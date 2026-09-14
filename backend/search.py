"""
search.py — searching the web without a quota that can run out.

THE PROBLEM THIS SOLVES
-----------------------
Search used to be one call to Gemini's grounded search. Great results, and
a free-tier limit small enough to hit in an afternoon of ordinary use. When
it ran out, Sid could not look anything up at all - so a capability you rely
on simply vanished for the rest of the day.

One provider means one failure takes everything with it. The fix is not a
better provider, it is **more than one**, tried in order.

THE LADDER
----------
Each rung is tried until one returns something usable:

    0. cache          free, instant      - asked this recently?
    1. duckduckgo     free, unlimited    - good results, blocks you sometimes
    2. bing rss       free, unlimited    - always answers, quality varies
    3. gemini         EXCELLENT, SCARCE  - the one with the small quota
    4. real browser   free, unlimited    - slow, but it cannot be refused

The ordering is the whole design, and it is deliberately NOT best-first.
The scarce rung sits near the bottom so the free ones spend it for you: if
DuckDuckGo answers, the quota is never touched and it is still there
tomorrow when nothing else will do.

The last rung matters more than it looks. Sid already drives a real Chrome
for Phase 8, and a real browser loading a real search page cannot be
rate-limited, because it is indistinguishable from you doing it. It is slow
- several seconds - which is exactly why it is last and not first.

WHY EACH RUNG HAD TO BE MEASURED FIRST
--------------------------------------
Every one of these was probed before being written in, and two candidates
that sounded obvious were dropped for failing:

    mojeek      captcha page
    searxng     public instances refuse JSON, or no longer resolve
    wikipedia   fine, but answers a different question than "search"

> **A fallback you have not tested is not a fallback.** It is a second way
> to fail, and you will find out on the day the first one breaks.
"""

import html
import re
import sqlite3
import time
from urllib.parse import urlparse

import httpx

from . import config, quota

DB_PATH = config.ROOT / "data" / "search_cache.db"

# A browser's user agent. Not a trick - these endpoints serve a different,
# emptier page to something that announces itself as a script.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# How long a cached answer stays good.
#
# Two values, because "what is a monad" does not change and "gold price"
# does. Getting this wrong in either direction is a real cost: too long and
# Sid confidently reports yesterday's weather, too short and the cache
# stops protecting the quota it exists to protect.
TTL_NORMAL = 6 * 60 * 60
TTL_FRESH = 15 * 60
FRESH_WORDS = re.compile(
    r"\b(now|today|tonight|current|currently|latest|live|score|scores|"
    r"weather|temperature|price|rate|stock|share|news|breaking|"
    r"right now|abhi|aaj)\b", re.I)

# When a backend says "you are rate limited", stop asking it. Retrying a
# backend that has already refused you wastes a whole round trip on every
# single search, and the answer will not have changed.
COOLDOWN = 60 * 60


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cache (
                query   TEXT PRIMARY KEY,
                answer  TEXT NOT NULL,
                backend TEXT NOT NULL,
                at      REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cooldown (
                backend TEXT PRIMARY KEY,
                until   REAL NOT NULL
            );
        """)


def _key(query: str) -> str:
    """Normalised, so 'Who Won?' and 'who won' are the same question."""
    return " ".join(query.lower().split()).strip(" ?.!")


def _ttl(query: str) -> int:
    return TTL_FRESH if FRESH_WORDS.search(query) else TTL_NORMAL


def cached(query: str) -> str | None:
    init()
    with _connect() as conn:
        row = conn.execute("SELECT answer, at FROM cache WHERE query=?",
                           (_key(query),)).fetchone()
    if row and time.time() - row["at"] < _ttl(query):
        return row["answer"]
    return None


def remember(query: str, answer: str, backend: str) -> None:
    init()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache (query, answer, backend, at) "
            "VALUES (?,?,?,?)", (_key(query), answer, backend, time.time()))


# Which backend is refusing right now is NOT tracked here. It lives in
# backend/quota.py, shared with the chat models, because it is the same
# problem wearing different clothes: a named thing, temporarily refusing,
# recovering on its own later. See that file for why it is on disk.

def _resting(backend: str) -> bool:
    return quota.resting("search:" + backend)


def _rest(backend: str, seconds: int = COOLDOWN) -> None:
    quota.rest("search:" + backend, seconds, reason="refused")


def _site(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def _clean(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _format(results: list[dict], query: str) -> str:
    """One shape for every backend, so the model sees a consistent answer."""
    lines = [f"Results for '{query}':", ""]
    for r in results[:6]:
        lines.append(f"- {r['title']}")
        if r.get("snippet"):
            lines.append(f"  {r['snippet'][:280]}")
        if r.get("url"):
            lines.append(f"  {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


# ==========================================================================
#  The rungs
# ==========================================================================

def _duckduckgo(query: str) -> list[dict]:
    """
    Free and unlimited, when it feels like it.

    DuckDuckGo serves HTTP 202 and a bot-detection page to some requests -
    intermittently, for the same query that worked a minute ago. A 202 with
    no results is NOT an empty internet, and treating it as one is how the
    old scraper came to answer 'no results found' to everything.

    So: try twice, and if there are no parsed results, say so and let the
    ladder move on rather than reporting emptiness as fact.
    """
    for attempt in range(2):
        response = httpx.post("https://html.duckduckgo.com/html/",
                              data={"q": query},
                              headers={"User-Agent": UA},
                              timeout=15, follow_redirects=True)
        if response.status_code == 200:
            break
        time.sleep(0.6)
    else:
        return []

    links = re.findall(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        response.text, re.S)
    snippets = re.findall(
        r'class="result__snippet"[^>]*>(.*?)</a>', response.text, re.S)

    out = []
    for i, (url, title) in enumerate(links[:6]):
        out.append({"title": _clean(title), "url": url,
                    "snippet": _clean(snippets[i]) if i < len(snippets) else ""})
    return out


def _bing_rss(query: str) -> list[dict]:
    """
    Bing will hand anyone an RSS feed of its results, with no key at all.

    It always answers, which makes it a dependable rung - but the quality
    wobbles (a question about a cricket final came back with lottery
    numbers), so it sits below DuckDuckGo rather than above it.
    """
    response = httpx.get("https://www.bing.com/search",
                         params={"q": query, "format": "rss"},
                         headers={"User-Agent": UA},
                         timeout=15, follow_redirects=True)
    if response.status_code != 200:
        return []

    out = []
    for item in re.findall(r"<item>(.*?)</item>", response.text, re.S)[:6]:
        title = re.search(r"<title>(.*?)</title>", item, re.S)
        desc = re.search(r"<description>(.*?)</description>", item, re.S)
        link = re.search(r"<link>(.*?)</link>", item, re.S)
        if title:
            out.append({"title": _clean(title.group(1)),
                        "snippet": _clean(desc.group(1)) if desc else "",
                        "url": link.group(1).strip() if link else ""})
    return out


async def _gemini(query: str) -> str | None:
    """
    The best answers, and the small quota this whole module exists because of.

    Reached only when the free rungs came back empty, which in practice is
    rare - so the limit stays unspent and is actually available on the day
    something genuinely needs it.
    """
    if not config.GEMINI_API_KEY:
        return None

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{config.GEMINI_MODEL}:generateContent")
    payload = {"contents": [{"role": "user", "parts": [{"text": query}]}],
               "tools": [{"google_search": {}}]}

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            url, headers={"x-goog-api-key": config.GEMINI_API_KEY},
            json=payload)

    if response.status_code == 429:
        _rest("gemini")
        return None
    if response.status_code != 200:
        return None

    try:
        candidate = response.json()["candidates"][0]
    except (KeyError, IndexError, ValueError):
        return None

    text = "".join(p.get("text", "")
                   for p in candidate.get("content", {}).get("parts", [])).strip()
    if not text:
        return None

    sources = []
    for chunk in candidate.get("groundingMetadata", {}).get(
            "groundingChunks", [])[:5]:
        web = chunk.get("web", {})
        if web.get("uri"):
            sources.append(f"  - {web.get('title', 'source')}: {web['uri']}")
    return text + ("\n\nSources:\n" + "\n".join(sources) if sources else "")


async def _real_browser(query: str) -> list[dict]:
    """
    The rung that cannot be refused.

    Sid already drives a real Chrome. A real browser loading a real search
    page is indistinguishable from you doing it, so there is no quota and
    no bot check to fail. It costs several seconds, which is the entire
    reason it is last rather than first.
    """
    from urllib.parse import quote_plus

    from .tools import browser

    text = await browser._in_browser_thread(
        browser._sync_open,
        f"https://duckduckgo.com/?q={quote_plus(query)}")
    text = await browser._in_browser_thread(browser._sync_read)
    if not text:
        return []

    lines = [ln.strip() for ln in str(text).splitlines() if len(ln.strip()) > 40]
    return [{"title": ln[:110], "snippet": ln[110:390], "url": ""}
            for ln in lines[:6]]


# ==========================================================================
#  The ladder
# ==========================================================================

async def search(query: str) -> str:
    """Try each rung until something answers. Never raises."""
    hit = cached(query)
    if hit:
        return hit + "\n\n(from a recent search)"

    tried = []

    for name, fn in (("duckduckgo", _duckduckgo), ("bing", _bing_rss)):
        if _resting(name):
            tried.append(f"{name}: resting")
            continue
        try:
            results = fn(query)
        except Exception as exc:
            tried.append(f"{name}: {type(exc).__name__}")
            continue
        if results:
            answer = _format(results, query)
            remember(query, answer, name)
            return answer
        tried.append(f"{name}: nothing")

    if not _resting("gemini"):
        try:
            answer = await _gemini(query)
        except Exception as exc:
            answer = None
            tried.append(f"gemini: {type(exc).__name__}")
        if answer:
            remember(query, answer, "gemini")
            return answer
        tried.append("gemini: nothing")
    else:
        tried.append("gemini: out of quota, resting")

    try:
        results = await _real_browser(query)
    except Exception as exc:
        results = []
        tried.append(f"browser: {type(exc).__name__}")
    if results:
        answer = _format(results, query)
        remember(query, answer, "browser")
        return answer

    # Everything failed. Say which, plainly - "no results" would be a lie,
    # and the model would report it to the user as fact.
    return ("Could not search just now. Tried: " + "; ".join(tried) +
            ". This is a search problem, not an empty internet - do not "
            "tell the user the thing does not exist, and do not retry.")

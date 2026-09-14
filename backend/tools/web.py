"""
tools/web.py — reaching outside the laptop.

Three tools here, and they show three different ways to get something done
when there's no convenient API:

  search_web        scrape DuckDuckGo's plain-HTML page
  play_on_youtube   scrape YouTube's search page for a video id
  open_url          just tell the operating system to open a browser

None of them need an API key. All of them are FRAGILE by nature — you are
reading someone else's HTML, and they can change it tomorrow without telling
you. That's not a flaw in your code, it's the deal. Phase 8 deals with this
properly using a real browser; this is the cheap version.
"""

import html
import json
import os
import re
import shutil
import subprocess
import time
import threading
import webbrowser

import httpx
from urllib.parse import quote_plus

from . import tool


# Chrome and Edge both support --app=<url>, which opens a URL in a clean
# window with no tabs, no address bar and its own taskbar icon. It looks and
# feels like a native application. That is how Sid opens anything visual.
_BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _find_browser() -> str | None:
    for path in _BROWSERS:
        if os.path.exists(path):
            return path
    for name in ("chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_app_window(url: str) -> str:
    """
    Open a URL as its own window, falling back sensibly.

    WHY NOT JUST webbrowser.open()?
    Two reasons, both learned the hard way:
      1. On Windows it often just FOCUSES an already-open browser instead of
         opening a new tab. Ask for three songs in a row and you keep staring
         at the first one, convinced the tool is broken.
      2. A tab buried among thirty others doesn't feel like an app.

    --app= gives a dedicated window every time, so each request visibly
    happens. If neither Chrome nor Edge exists we fall back to open_new_tab,
    which at least forces a NEW tab rather than reusing one.
    """
    browser = _find_browser()
    if browser:
        try:
            # Popen, not run() - we must not wait for the browser to close.
            subprocess.Popen([browser, f"--app={url}", "--new-window"])
            return "app window"
        except Exception:
            pass

    webbrowser.open_new_tab(url)
    return "browser tab"


# ==========================================================================
#  The media window — exactly one, replaced rather than stacked
# ==========================================================================
#
# THE BUG: asking for a second song opened a second player and left the first
# one playing. Two songs at once, and the only fix was hunting down windows
# by hand. Real transcript: two YouTube windows found open simultaneously.
#
# "Play X" does not mean "start X in addition to whatever is running". It
# means the same thing it means on any music player: play X *instead*.
#
# The handle is captured AFTER the window appears, by diffing the list of
# top-level windows before and after. That is deliberate: the process id
# from Popen is useless, because Chrome hands the request to an existing
# browser process and the process we launched exits immediately.
_media_hwnd: int | None = None


def close_media_window() -> bool:
    """Close the player Sid last opened. True if there was one."""
    global _media_hwnd
    from .. import windows

    if _media_hwnd is None:
        return False
    closed = windows.close_window(_media_hwnd)
    _media_hwnd = None
    return closed


def _capture_media_window(before: set[int]) -> None:
    """
    Wait for the new window to appear and remember it.

    Runs on a thread: the window takes a second or two to exist, and the
    person who asked for a song should not be kept waiting to be told it
    started.
    """
    global _media_hwnd
    from .. import windows

    for _ in range(24):                       # up to ~6 seconds
        time.sleep(0.25)
        fresh = [h for h, _t in windows.visible_windows() if h not in before]
        if fresh:
            _media_hwnd = fresh[-1]
            return


def open_media_window(url: str) -> str:
    """Open a player window, replacing the previous one."""
    from .. import windows

    close_media_window()
    # Give the old window a moment to actually go, so it isn't still in the
    # "before" snapshot and mistaken for the new one.
    time.sleep(0.4)

    before = {h for h, _t in windows.visible_windows()}
    where = open_app_window(url)
    threading.Thread(target=_capture_media_window, args=(before,),
                     daemon=True).start()
    return where

# Without a browser-ish User-Agent, most sites return a blocking page.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
TIMEOUT = 20.0


def _strip_tags(raw: str) -> str:
    """Turn a chunk of HTML into readable text."""
    return html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


@tool(tier="read")
async def search_web(query: str) -> str:
    """Search the web and summarise what's found, with source links.

    Use this for anything you don't know, anything recent, or anything the
    user asks you to look up. Your training data is frozen; this is not.

    Args:
        query: What to search for, e.g. "python asyncio tutorial"
    """
    # WHY THIS FUNCTION IS NOW FOUR LINES
    #
    # It used to be one call to Gemini's grounded search, and it had one
    # failure mode that mattered: a free-tier limit small enough to hit in
    # an afternoon. When it ran out, Sid could not look anything up at all.
    #
    # One provider means one failure takes the whole capability with it. The
    # fix lives in backend/search.py - a ladder of four independent
    # backends with a cache in front, arranged so the scarce one is spent
    # LAST rather than first. Read that file for why each rung is where it
    # is and which candidates were dropped for failing a probe.
    from .. import search as web_search

    return await web_search.search(query)


@tool(tier="act", speaks_for_itself=True)
async def play_on_youtube(query: str) -> str:
    """Find and play any song, music video or video on YouTube.

    Works for any song in any language. Pass exactly what the user asked for.

    Args:
        query: The exact song, artist or video the user named. Use their own
               words. Add the artist only if the user mentioned one.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(
                "https://www.youtube.com/results",
                params={"search_query": query},
                headers=HEADERS,
            )
            resp.raise_for_status()
    except Exception as exc:
        return f"Couldn't reach YouTube: {exc}"

    # YouTube's page embeds its search results as JSON inside a <script> tag.
    # Video ids are always exactly 11 characters, which makes them easy to
    # spot. The first one on the page is the top result.
    ids = re.findall(r'"videoId":"([\w-]{11})"', resp.text)
    titles = re.findall(r'"title":\{"runs":\[\{"text":"(.*?)"\}\]', resp.text)

    if not ids:
        # Graceful degradation: we couldn't find the exact video, but we can
        # still get the user to the right place. A tool that half-works beats
        # a tool that throws up its hands.
        url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        open_app_window(url)
        return f"Couldn't pick a video, so I opened the YouTube search for '{query}'."

    video_id = ids[0]

    # The title arrives with JSON escapes still in it, e.g. तुम
    # for Hindi. Wrapping it in quotes and letting json.loads decode is the
    # correct way. (An earlier version used .encode().decode("unicode_escape"),
    # which mangles every non-Latin script into mojibake like "à¤¤à¥à¤®".)
    title = query
    if titles:
        try:
            title = json.loads(f'"{titles[0]}"')
        except Exception:
            title = titles[0]

    url = f"https://www.youtube.com/watch?v={video_id}&autoplay=1"
    where = open_media_window(url)

    return f"Playing '{title}' on YouTube in a {where}. ({url})"


@tool(tier="act", speaks_for_itself=True)
def open_url(url: str) -> str:
    """Open a web page in the user's default browser.

    Args:
        url: The full address to open, e.g. "https://github.com"
    """
    # Only http/https. Without this check, a model could be talked into
    # opening file:// (reads your disk) or a custom scheme that launches
    # a program. Validate anything that reaches the operating system.
    if not re.match(r"^https?://", url):
        return f"Refused: '{url}' is not an http(s) address."

    where = open_app_window(url)
    return f"Opened {url} in a {where}."


@tool(tier="act", speaks_for_itself=True)
def stop_music() -> str:
    """Stop whatever Sid is playing and close the player window.

    Use this for "stop the music", "turn it off", "close the song".
    """
    # Its own tool rather than leaning on control_media, because media keys
    # only reach the window that currently has focus. "Stop the music" said
    # while you are typing somewhere else has to work regardless of focus,
    # and closing the window we opened always does.
    if close_media_window():
        return "Stopped."
    return "Nothing is playing that I opened."

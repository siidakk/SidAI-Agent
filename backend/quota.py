"""
quota.py — one place that knows what has run out.

THE IDEA
--------
Sid depends on several things that can be *temporarily* refused: two search
backends, a couple of Gemini models, a browser. Each has its own limit and
its own reset, and none of them tell you in advance.

The naive handling is to try, fail, and report the failure. That is how you
get "search quota expired" as an answer to a question. The next naive
handling is to try, fail, try the next one - better, but it pays the cost of
the failed attempt on *every single request*, forever.

So there is a third thing, and it is the whole point of this file:

    **When something refuses you, write down that it refused you, and stop
    asking it until it is likely to have recovered.**

That turns a repeated round trip into a lookup, and it lets everything else
skip straight to what still works. A backend is either available or
"resting"; nothing anywhere else has to know why.

WHY IT IS SHARED
----------------
Search backends and chat models have nothing to do with each other, but the
*shape* of the problem is identical: a named thing, temporarily refusing,
recovering on its own later. Writing that twice would mean two subtly
different implementations and two places to fix a bug.

WHY IT IS ON DISK
-----------------
Sid is several processes - the server, the overlay, a scheduled task. A
limit hit by one of them is hit for all of them. Keeping this in memory
would mean each process rediscovering the same refusal separately, which is
exactly the wasted round trip this file exists to remove.
"""

import sqlite3
import time

from . import config

DB_PATH = config.ROOT / "data" / "quota.db"

# How long to leave something alone after it refuses, by default.
#
# Measured, not guessed: Gemini's free tier refuses on a PER-MINUTE limit
# far more often than a daily one - both models answered normally seconds
# after a 429 during testing. So the default rest is short. Something that
# is really out for the day will simply refuse again and rest again.
DEFAULT_REST = 90

# Grounded search is the exception: its limit is daily, and retrying it
# every 90 seconds all afternoon would be pure waste.
LONG_REST = 60 * 60


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS resting (
                name    TEXT PRIMARY KEY,
                until   REAL NOT NULL,
                reason  TEXT,
                since   REAL NOT NULL
            )
        """)


def rest(name: str, seconds: int = DEFAULT_REST, reason: str = "") -> None:
    """Stop asking this one for a while."""
    init()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO resting (name, until, reason, since) "
            "VALUES (?,?,?,?)",
            (name, time.time() + seconds, reason[:200], time.time()))


def resting(name: str) -> bool:
    init()
    with _connect() as conn:
        row = conn.execute("SELECT until FROM resting WHERE name=?",
                           (name,)).fetchone()
    return bool(row and row["until"] > time.time())


def revive(name: str) -> None:
    """It worked, so forget any grudge - limits reset and we want to know."""
    init()
    with _connect() as conn:
        conn.execute("DELETE FROM resting WHERE name=?", (name,))


def first_available(names: list[str]) -> str | None:
    """
    The first one not currently resting.

    Returns None when everything is resting, and the caller should then try
    anyway rather than give up - a rest is a guess about when something
    recovers, not a fact, and being wrong about it must never be the reason
    Sid refuses to do something.
    """
    for name in names:
        if not resting(name):
            return name
    return None


def status() -> list[dict]:
    """What is currently resting, and for how much longer."""
    init()
    now = time.time()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM resting ORDER BY until").fetchall()
    return [{"name": r["name"],
             "seconds_left": max(0, int(r["until"] - now)),
             "reason": r["reason"] or ""}
            for r in rows if r["until"] > now]

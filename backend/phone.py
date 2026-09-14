"""
phone.py — the one way an iPhone will let you do this.

WHY A QUEUE, AND WHY THE PHONE PULLS
------------------------------------
On the PC, Sid pushes: it moves the mouse, types, clicks. On iOS none of
that exists. Apple blocks external tap injection, outside app automation,
and remotely-triggered background execution, and there is no ADB. A laptop
cannot reach into an iPhone and drive it.

What Apple *does* allow is the phone reaching out. So the direction is
inverted:

    you ask Sid        ->  a command goes in a queue here
    the iPhone asks    ->  "anything for me?"  ->  runs it  ->  reports back

Nothing is pushed into the phone. The phone volunteers, which is exactly
the shape iOS permits, and it needs no jailbreak, no MDM and no paid relay
app.

The cost is honest: **it is not instant unless you trigger it.** Back Tap,
the Action Button, "Hey Siri, ask Sid", or a time-based automation that
polls every few minutes. A queued command waits until the phone next asks.

WHY THE VOCABULARY IS SMALL
---------------------------
Every action here has to be hand-built as an `If` block inside the iOS
Shortcuts app, by a person, once. A hundred actions would be unmaintainable
and would never get finished, so this is deliberately a short list of things
worth doing from a laptop. Adding one is one `If` block plus one entry in
ACTIONS.

COMMANDS EXPIRE
---------------
A command the phone never collected is not a command any more, it is a
surprise. "Text mom I'm running late", collected six hours later, is worse
than nothing - so anything uncollected is dropped after STALE_AFTER.
"""

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from . import config

DB_PATH = config.ROOT / "data" / "phone.db"

# A command nobody collected within this long is stale and gets dropped.
# See the note above: a late action is worse than a missing one.
STALE_AFTER = 30 * 60          # seconds

# What the Shortcut on the phone knows how to do. Each of these is one
# `If` block over there, so keep the list short and the names stable.
ACTIONS = {
    "message":  "Send a text message.            args: to, body",
    "play":     "Play music.                     args: query",
    "open":     "Open an app on the phone.       args: name",
    "timer":    "Start a timer.                  args: minutes",
    "speak":    "Say something out loud.         args: text",
    "battery":  "Report battery level back.      args: none",
    "note":     "Add to Notes.                   args: text",
    "reminder": "Add a reminder.                 args: text",
}


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS commands (
                id         TEXT PRIMARY KEY,
                action     TEXT NOT NULL,
                args       TEXT NOT NULL,      -- JSON
                status     TEXT NOT NULL,      -- queued | taken | done | stale
                result     TEXT,
                created_at TEXT NOT NULL,
                taken_at   TEXT,
                done_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_phone_status ON commands(status);
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def queue(action: str, args: dict | None = None) -> dict:
    """Put one command in the queue for the phone to collect."""
    init()
    if action not in ACTIONS:
        raise ValueError(
            f"'{action}' isn't something the phone Shortcut knows. "
            f"Options: {', '.join(sorted(ACTIONS))}")

    command_id = uuid.uuid4().hex[:10]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO commands (id, action, args, status, created_at) "
            "VALUES (?,?,?, 'queued', ?)",
            (command_id, action, json.dumps(args or {}), _now()))
    return {"id": command_id, "action": action, "args": args or {}}


def take_next() -> dict | None:
    """
    Hand the phone the oldest waiting command, and mark it taken.

    One at a time, on purpose. The Shortcut over there is built by hand out
    of If blocks; making it loop over a list would roughly double how fussy
    it is to build, for a case (several commands at once) that is rare.
    """
    init()
    _expire()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM commands WHERE status='queued' "
            "ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE commands SET status='taken', taken_at=? WHERE id=?",
                     (_now(), row["id"]))

    return {"id": row["id"], "action": row["action"],
            **json.loads(row["args"] or "{}")}


def complete(command_id: str, result: str = "") -> bool:
    """The phone reporting what happened."""
    init()
    with _connect() as conn:
        return conn.execute(
            "UPDATE commands SET status='done', result=?, done_at=? WHERE id=?",
            (str(result)[:2000], _now(), command_id)).rowcount > 0


def _expire() -> None:
    """Drop anything the phone never came to collect."""
    cutoff = datetime.fromtimestamp(time.time() - STALE_AFTER,
                                    tz=timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE commands SET status='stale' "
            "WHERE status='queued' AND created_at < ?", (cutoff,))


def pending() -> list[dict]:
    init()
    _expire()
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM commands WHERE status IN ('queued','taken') "
            "ORDER BY created_at")]


def recent(limit: int = 10) -> list[dict]:
    init()
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM commands ORDER BY created_at DESC LIMIT ?", (limit,))]


def last_seen() -> str | None:
    """When the phone last asked for work. Tells you if the bridge is alive."""
    init()
    with _connect() as conn:
        row = conn.execute(
            "SELECT MAX(taken_at) AS t FROM commands WHERE taken_at IS NOT NULL"
        ).fetchone()
    return row["t"] if row and row["t"] else None

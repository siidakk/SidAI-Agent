"""
audit.py — an append-only record of everything Sid actually did.

WHY THIS MATTERS MORE FROM NOW ON
---------------------------------
Until Phase 6 you watched every action happen. From now on, jobs run in the
background while you're asleep or in class. "What did it do at 3am?" needs an
answer, and "check the chat scrollback" is not one — the chat shows what it
SAID, not what it DID.

APPEND-ONLY, ON PURPOSE
-----------------------
There is no update and no delete in this module. Not because deleting is
technically hard, but because a log you can edit is not evidence. The moment
something can quietly rewrite its own history, the log stops answering the
only question it exists for.

That's also why the audit log is separate from `memory.py`. Memory is *for*
the agent and it's allowed to forget — `forget()` is a normal tool. This is
*about* the agent, and it never forgets.

WHAT GOES IN
------------
Every tool call: what was asked, what came back, whether it was approved,
how long it took, and which task it belonged to. Enough to reconstruct an
afternoon without guessing.
"""

import json
import sqlite3
from datetime import datetime, timezone

from . import config

DB_PATH = config.ROOT / "data" / "audit.db"

# Results are truncated. A log entry is a record that something happened, not
# a copy of the data - storing entire email bodies here would duplicate your
# inbox into a second file with different security properties.
MAX_RESULT = 2000


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS entries (
                id         INTEGER PRIMARY KEY,
                at         TEXT NOT NULL,
                tool       TEXT NOT NULL,
                tier       TEXT NOT NULL,
                args       TEXT NOT NULL,
                result     TEXT,
                ok         INTEGER NOT NULL DEFAULT 1,
                approved   TEXT,               -- granted / denied / n/a
                task_id    TEXT,               -- set when run by a background job
                ms         INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_entries_at   ON entries(at DESC);
            CREATE INDEX IF NOT EXISTS idx_entries_task ON entries(task_id);
            CREATE INDEX IF NOT EXISTS idx_entries_tool ON entries(tool);
        """)


def record(
    tool: str,
    tier: str,
    args: dict,
    result: str,
    ok: bool = True,
    approved: str | None = None,
    task_id: str | None = None,
    ms: int | None = None,
) -> int:
    """
    Write one entry. Never raises.

    A failure to log must not break the thing being logged - an agent that
    crashes because its audit database is locked is worse than one with a
    gap in its records. The gap is visible; the crash loses the work.
    """
    try:
        with _connect() as conn:
            cursor = conn.execute(
                "INSERT INTO entries "
                "(at, tool, tier, args, result, ok, approved, task_id, ms) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    tool, tier,
                    json.dumps(args, default=str)[:MAX_RESULT],
                    (result or "")[:MAX_RESULT],
                    1 if ok else 0,
                    approved, task_id, ms,
                ),
            )
            return cursor.lastrowid
    except Exception:
        return 0


def recent(limit: int = 100, tool: str | None = None,
           task_id: str | None = None) -> list[dict]:
    """Most recent entries, newest first."""
    query = "SELECT * FROM entries"
    where, params = [], []

    if tool:
        where.append("tool = ?"); params.append(tool)
    if task_id:
        where.append("task_id = ?"); params.append(task_id)
    if where:
        query += " WHERE " + " AND ".join(where)

    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    try:
        with _connect() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]
    except Exception:
        return []


def summary() -> dict:
    """Counts for the UI: how much has happened, and how much of it was risky."""
    try:
        with _connect() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
            failed = conn.execute(
                "SELECT COUNT(*) c FROM entries WHERE ok = 0").fetchone()["c"]
            denied = conn.execute(
                "SELECT COUNT(*) c FROM entries WHERE approved = 'denied'"
            ).fetchone()["c"]
            top = conn.execute(
                "SELECT tool, COUNT(*) n FROM entries "
                "GROUP BY tool ORDER BY n DESC LIMIT 5"
            ).fetchall()
        return {
            "total": total, "failed": failed, "denied": denied,
            "top_tools": [dict(r) for r in top],
        }
    except Exception:
        return {"total": 0, "failed": 0, "denied": 0, "top_tools": []}

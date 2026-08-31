"""
jobs.py — work that keeps going after you close the window.

THE PROBLEM
-----------
Everything so far runs inside an HTTP request. Ask something, watch it
stream, get an answer. Close the tab halfway and the work dies with the
connection.

That's fine for "what's the time". It is not fine for "search these five
things and summarise", and it makes "do this while I'm in class" impossible.

THE SHAPE OF THE FIX
--------------------
    POST /api/task   ->  returns a run_id IMMEDIATELY
    (worker runs it in the background)
    GET  /api/task/{id}  ->  status, events so far, final answer

The request that starts the work is no longer the request that waits for it.
That one idea is what separates a script from a service.

WHY SQLITE AND NOT A REAL QUEUE
-------------------------------
Celery, RQ and friends assume multiple machines and a broker to coordinate
them. This is one laptop with one user. A table plus an asyncio task does
the job, and it survives a restart because the rows are on disk — which is
the only property of a "real" queue that actually matters here.

Same reasoning as Phase 4 choosing SQLite over Postgres: reach for the
smaller tool until the bigger one is justified.

APPROVALS, WHICH IS THE INTERESTING PART
----------------------------------------
A background job hitting a `danger` tool cannot block on a UI that isn't
open. Two bad options and one good one:

  - Auto-deny        safe, but background jobs can never do anything real
  - Auto-approve     absolutely not
  - PAUSE and wait   the job parks itself, the task list shows "needs you",
                     and it resumes when you tap approve - from your phone,
                     an hour later, wherever you are

The third is what "do it while I'm in class" actually requires, so the
approval timeout for jobs is an hour rather than three minutes.
"""

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from . import approvals, audit, config

DB_PATH = config.ROOT / "data" / "jobs.db"

# Background approvals wait far longer than interactive ones. You started
# this to walk away from it; three minutes would defeat the point.
APPROVAL_TIMEOUT = 3600

# One job at a time. This laptop has 8 GB and a CPU already sharing itself
# with a browser; running four plans at once would make all four slow.
MAX_CONCURRENT = 1

# Live handles for running jobs, so they can be cancelled. Not persisted —
# a restart kills the asyncio tasks anyway, and the DB records that.
_running: dict[str, asyncio.Task] = {}
_gate = asyncio.Semaphore(MAX_CONCURRENT)


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id          TEXT PRIMARY KEY,
                request     TEXT NOT NULL,
                status      TEXT NOT NULL,   -- queued running needs_approval
                                             -- done failed cancelled
                created_at  TEXT NOT NULL,
                finished_at TEXT,
                answer      TEXT,
                events      TEXT NOT NULL DEFAULT '[]',
                error       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        """)

    # A job marked "running" at startup is a lie: the process that was
    # running it is gone. Mark them failed rather than leaving rows that
    # claim to be in progress forever.
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET status='failed', "
            "error='Sid restarted while this was running' "
            "WHERE status IN ('running','queued','needs_approval')"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(request: str) -> str:
    """Record a new job and return its id. Does not start it."""
    job_id = uuid.uuid4().hex[:12]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id, request, status, created_at) VALUES (?,?,?,?)",
            (job_id, request, "queued", _now()),
        )
    return job_id


def get(job_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    job = dict(row)
    job["events"] = json.loads(job["events"] or "[]")
    return job


def recent(limit: int = 25) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, request, status, created_at, finished_at, answer, error "
            "FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _update(job_id: str, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE jobs SET {sets} WHERE id=?",
                     (*fields.values(), job_id))


def _append_event(job_id: str, event: dict) -> None:
    """
    Store an event so the task page can replay what happened.

    Read-modify-write on a JSON column. Fine here because MAX_CONCURRENT is
    1, so only one job ever writes at a time. If that ever changes this
    becomes a separate table with one row per event - noting it now, because
    "it was safe when I wrote it" is how races get born.
    """
    job = get(job_id)
    if job is None:
        return
    events = job["events"]
    events.append(event)
    _update(job_id, events=json.dumps(events[-200:]))


async def _approve_in_background(job_id: str):
    """
    Approval callback for a job nobody is watching.

    Parks the job as `needs_approval` and waits up to an hour. The task list
    surfaces it, and approving from anywhere resumes the job exactly where
    it stopped.
    """
    async def approve(tool_name: str, arguments: dict) -> bool:
        request = approvals.create(tool_name, arguments)

        _update(job_id, status="needs_approval")
        _append_event(job_id, {
            "type": "approval_request", "id": request.id,
            "tool": tool_name,
            "summary": approvals.describe(tool_name, arguments),
        })

        granted = await approvals.wait_for(request, timeout=APPROVAL_TIMEOUT)

        _update(job_id, status="running")
        _append_event(job_id, {"type": "approval_result",
                               "id": request.id, "granted": granted})
        return granted

    return approve


async def _run(job_id: str, request_text: str) -> None:
    """Execute one job. Never raises — a crash must land in the DB, not the void."""
    from . import llm, traces

    async with _gate:
        _update(job_id, status="running")
        answer_parts: list[str] = []

        # Background turns are the ones you most need a trace of: nobody
        # watched them happen, so the trace is the only account of what did.
        trace = traces.Trace(request_text, conversation=f"job:{job_id}")

        try:
            approve = await _approve_in_background(job_id)

            async for event in llm.stream_reply(
                [{"role": "user", "content": request_text}],
                approve=approve,
                task_id=job_id,
            ):
                trace.observe(event)
                if event["type"] == "text":
                    answer_parts.append(event["text"])
                else:
                    _append_event(job_id, event)

            _update(job_id, status="done", finished_at=_now(),
                    answer="".join(answer_parts).strip())

        except asyncio.CancelledError:
            _update(job_id, status="cancelled", finished_at=_now())
            raise
        except Exception as exc:
            trace.observe({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            _update(job_id, status="failed", finished_at=_now(),
                    error=f"{type(exc).__name__}: {exc}")
        finally:
            trace.save()
            _running.pop(job_id, None)


def start(request_text: str) -> str:
    """Queue a job and start it in the background. Returns immediately."""
    job_id = create(request_text)
    task = asyncio.create_task(_run(job_id, request_text))

    # Keep a reference. Without one, asyncio can garbage-collect a running
    # task mid-flight - a genuinely baffling bug where work simply stops
    # with no error anywhere.
    _running[job_id] = task
    return job_id


def cancel(job_id: str) -> bool:
    task = _running.get(job_id)
    if task is None:
        return False
    task.cancel()
    return True

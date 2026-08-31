"""
triggers.py — Sid starting things on its own.

THE LAST INVERSION
------------------
Phases 1-7 were all "you ask, Sid answers". This is the one where Sid acts
without being asked: every weekday at 8am, or every 30 minutes, a trigger
fires, a job runs, and you find out only if the answer is worth your
attention.

That's the difference between a very good chatbot and something that feels
like it works for you.

BUILT ON PHASE 6, NOT BESIDE IT
-------------------------------
A trigger does exactly one thing: call `jobs.start()`. It has no execution
logic of its own.

That's deliberate and it's why this file is short. Background execution,
approvals, the audit trail, restart recovery - all of that already exists
and works. A trigger that re-implemented any of it would be a second code
path that drifts out of step with the first.

**When you add a scheduler, schedule the thing you already have.**

WHY NOT CRON SYNTAX
-------------------
`0 8 * * 1-5` is precise, standard, and nobody can read it. This takes
"every day at 08:00" and "every 30 minutes", which covers what a personal
assistant actually needs. If you ever want the full grammar, the field is
already there to hold it.

THE HARD PART IS NOT SCHEDULING
-------------------------------
It's deciding when to stay quiet. A trigger that fires every 30 minutes and
notifies every time has taught you to ignore it within a day. So a job's
notification is opt-in per trigger, and the prompt itself is expected to say
"only mention this if something changed".
"""

import asyncio
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from . import config

DB_PATH = config.ROOT / "data" / "triggers.db"

# How often the scheduler wakes up to look for due triggers. 30 seconds is
# far more precision than "every morning at 8" needs, and it costs one
# cheap SQL query - no reason to be cleverer.
TICK_SECONDS = 30

_task: asyncio.Task | None = None

# Live "wait for this job, then notify" tasks. See the comment in _fire()
# for why holding these matters.
_watchers: set[asyncio.Task] = set()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS triggers (
                id        TEXT PRIMARY KEY,
                name      TEXT NOT NULL,
                kind      TEXT NOT NULL,     -- daily | interval | once
                spec      TEXT NOT NULL,     -- "08:00" | "30" (minutes) | ISO time
                prompt    TEXT NOT NULL,
                notify    INTEGER NOT NULL DEFAULT 1,
                enabled   INTEGER NOT NULL DEFAULT 1,
                next_run  TEXT,
                last_run  TEXT,
                last_job  TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_triggers_next ON triggers(next_run);
        """)


def _now() -> datetime:
    return datetime.now().astimezone()


def _compute_next(kind: str, spec: str, after: datetime | None = None) -> datetime | None:
    """
    When should this fire next?

    Local time throughout, on purpose. "8am" means 8am where you are, and a
    personal assistant that fires at 8am UTC would be quietly useless.
    """
    after = after or _now()

    if kind == "daily":
        try:
            hour, minute = (int(x) for x in spec.split(":"))
        except (ValueError, AttributeError):
            return None
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Already past today? Then tomorrow.
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate

    if kind == "interval":
        try:
            minutes = max(1, int(spec))
        except (ValueError, TypeError):
            return None
        return after + timedelta(minutes=minutes)

    if kind == "once":
        try:
            when = datetime.fromisoformat(spec)
            if when.tzinfo is None:
                when = when.astimezone()
            return when if when > after else None
        except ValueError:
            return None

    return None


def create(name: str, kind: str, spec: str, prompt: str, notify: bool = True) -> dict:
    next_run = _compute_next(kind, spec)
    if next_run is None:
        raise ValueError(
            f"Couldn't work out when '{kind}' with '{spec}' should run. "
            f"Use daily+'08:00', interval+'30', or once+an ISO timestamp."
        )

    trigger_id = uuid.uuid4().hex[:10]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO triggers "
            "(id, name, kind, spec, prompt, notify, enabled, next_run, created_at) "
            "VALUES (?,?,?,?,?,?,1,?,?)",
            (trigger_id, name, kind, spec, prompt, 1 if notify else 0,
             next_run.isoformat(), _now().isoformat()),
        )
    return get(trigger_id)


def get(trigger_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM triggers WHERE id=?", (trigger_id,)).fetchone()
    return dict(row) if row else None


def all_triggers() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM triggers ORDER BY enabled DESC, next_run"
        ).fetchall()
    return [dict(r) for r in rows]


def set_enabled(trigger_id: str, enabled: bool) -> bool:
    with _connect() as conn:
        return conn.execute(
            "UPDATE triggers SET enabled=? WHERE id=?",
            (1 if enabled else 0, trigger_id),
        ).rowcount > 0


def delete(trigger_id: str) -> bool:
    with _connect() as conn:
        return conn.execute("DELETE FROM triggers WHERE id=?", (trigger_id,)).rowcount > 0


def due(at: datetime | None = None) -> list[dict]:
    at = at or _now()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM triggers WHERE enabled=1 AND next_run IS NOT NULL "
            "AND next_run <= ?", (at.isoformat(),),
        ).fetchall()
    return [dict(r) for r in rows]


async def _fire(trigger: dict) -> None:
    """Run one trigger: start a job, then reschedule."""
    from . import jobs

    job_id = jobs.start(trigger["prompt"])

    following = _compute_next(trigger["kind"], trigger["spec"])
    with _connect() as conn:
        conn.execute(
            "UPDATE triggers SET last_run=?, last_job=?, next_run=?, "
            "enabled=? WHERE id=?",
            (
                _now().isoformat(), job_id,
                following.isoformat() if following else None,
                # A "once" trigger disables itself. Leaving it enabled with
                # no next_run would be a row that looks live and never fires.
                0 if trigger["kind"] == "once" else trigger["enabled"],
                trigger["id"],
            ),
        )

    if trigger["notify"]:
        # Hold the reference. asyncio only keeps a WEAK reference to a task,
        # so one with no owner can be garbage-collected mid-await and simply
        # stop - no error, no notification, nothing to debug. Exactly the
        # bug `_running[job_id] = task` prevents in jobs.py; it took one
        # silent missing notification here to notice it was the same bug.
        watcher = asyncio.create_task(_notify_when_done(trigger, job_id))
        _watchers.add(watcher)
        watcher.add_done_callback(_watchers.discard)


async def _notify_when_done(trigger: dict, job_id: str, timeout: float = 900) -> None:
    """
    Wait for the job, then decide whether it's worth interrupting for.

    The judgement lives here rather than in the job: a job doesn't know
    whether it was started by you (you're already looking) or by a trigger
    at 8am (you are not).
    """
    from . import jobs, notify

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(3)

        job = jobs.get(job_id)
        if job is None:
            return

        if job["status"] == "needs_approval":
            notify.send(
                f"{trigger['name']} needs you",
                "A scheduled task is waiting for your approval.",
                kind="approval", task_id=job_id,
            )
            continue

        if job["status"] in ("done", "failed", "cancelled"):
            if job["status"] == "done" and job.get("answer"):
                notify.send(trigger["name"], job["answer"][:200],
                            kind="result", task_id=job_id)
            elif job["status"] == "failed":
                notify.send(f"{trigger['name']} failed",
                            (job.get("error") or "")[:200],
                            kind="error", task_id=job_id)
            return


async def _loop() -> None:
    """The scheduler. Wakes, fires anything due, sleeps again."""
    while True:
        try:
            for trigger in due():
                await _fire(trigger)
        except Exception:
            # A scheduler that dies on one bad trigger stops every other
            # trigger too. Swallow, and try again on the next tick.
            pass
        await asyncio.sleep(TICK_SECONDS)


def start_scheduler() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


def stop_scheduler() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None

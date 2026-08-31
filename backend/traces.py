"""
traces.py — what actually happened, for one whole turn.

WHY THIS IS NOT THE AUDIT LOG
-----------------------------
`audit.py` answers *"what did Sid DO?"* - one row per tool call, append-only,
because it is evidence. Keep it forever.

This answers a different question: *"why did Sid do that?"* One row per TURN,
holding the shape of the whole thing - what you asked, what it planned, which
steps ran and how long each took, what came back, how many tokens it cost.

The distinction matters when something goes wrong. The audit log tells you
`search_web` was called six times. Only a trace tells you the plan had one
search step and the loop guard never fired, which is a completely different
bug from the one you'd have guessed.

> **Audit is for accountability. Traces are for understanding.**

And unlike the audit log, traces are allowed to be deleted. They are
diagnostics, not a record, so they roll off after a while rather than growing
without limit.

WHAT MAKES A TRACE USEFUL
-------------------------
Timings per step, not just for the turn. A turn that took 9 seconds tells you
nothing; a turn where one step took 8.4 of those 9 seconds tells you exactly
where to look.

That is also what makes Phase 11's evals possible. An eval asserts things
like "this request should call schedule_task" or "this should finish in under
one model call" - and it can only assert them because the trace recorded them.

**You cannot improve what you do not measure, and you cannot measure what you
did not record.**
"""

import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import config

DB_PATH = config.ROOT / "data" / "traces.db"

# Traces are diagnostics, not evidence. Keeping a month of them would make
# the database large and the useful ones hard to find.
KEEP_DAYS = 7


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS traces (
                id           TEXT PRIMARY KEY,
                conversation TEXT,
                request      TEXT NOT NULL,
                mode         TEXT,            -- direct | plan | react
                steps        TEXT,            -- JSON: [{tool, ms, ok, output}]
                answer       TEXT,
                error        TEXT,
                ms           INTEGER,
                tokens_in    INTEGER,
                tokens_out   INTEGER,
                provider     TEXT,
                model        TEXT,
                started_at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_traces_time ON traces(started_at DESC);
        """)


class Trace:
    """
    One turn, recorded as it happens.

    Deliberately a plain object that writes ONCE at the end, rather than
    updating a row as it goes. A turn is either a complete story or it
    isn't - half-written traces would be the confusing kind of data, and the
    write costs nothing at the end of something that took seconds.

    Nothing here may raise. A tracing bug that breaks a working turn would be
    worse than no tracing at all - the same rule as audit.record().
    """

    def __init__(self, request: str, conversation: str = "default"):
        self.id = uuid.uuid4().hex[:12]
        self.request = request
        self.conversation = conversation
        self.started = time.time()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.steps: list[dict] = []
        self.mode = ""
        self.answer = ""
        self.error = ""
        self.tokens_in = 0
        self.tokens_out = 0
        self._step_started: dict[str, float] = {}

    def step_start(self, step_id: str, tool: str, args: dict | None = None) -> None:
        self._step_started[step_id] = time.time()
        self.steps.append({"id": step_id, "tool": tool,
                           "args": _shorten(args or {}), "ms": None, "ok": None})

    def step_done(self, step_id: str, output: str = "", ok: bool = True) -> None:
        started = self._step_started.pop(step_id, None)
        for step in reversed(self.steps):
            if step["id"] == step_id:
                step["ms"] = int((time.time() - started) * 1000) if started else None
                step["ok"] = ok
                step["output"] = str(output)[:300]
                break

    def observe(self, event: dict) -> None:
        """
        Feed it the same events the browser gets.

        Reading the existing event stream rather than adding trace calls
        throughout llm.py means the trace cannot drift out of step with what
        actually ran - it is watching the real thing, not a parallel
        description of it.
        """
        try:
            kind = event.get("type")
            if kind == "step_start":
                self.step_start(event.get("id", "?"), event.get("tool", "?"),
                                event.get("args"))
            elif kind == "step_done":
                self.step_done(event.get("id", "?"), event.get("output", ""),
                               event.get("ok", True))
            elif kind == "tool_call":
                # The reactive loop has no step ids, so synthesise one.
                self.step_start(f"r{len(self.steps)}", event.get("tool", "?"),
                                event.get("input"))
            elif kind == "tool_result":
                if self.steps and self.steps[-1]["ms"] is None:
                    self.step_done(self.steps[-1]["id"],
                                   event.get("output", ""), event.get("ok", True))
            elif kind == "text":
                self.answer += event.get("text", "")
            elif kind == "done":
                self.mode = event.get("mode", "") or self.mode
                usage = event.get("usage") or {}
                self.tokens_in += usage.get("input_tokens", 0) or 0
                self.tokens_out += usage.get("output_tokens", 0) or 0
            elif kind == "error":
                self.error = str(event.get("message") or event.get("text") or "")[:500]
        except Exception:
            pass

    def save(self) -> None:
        try:
            init()
            with _connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO traces (id, conversation, request, "
                    "mode, steps, answer, error, ms, tokens_in, tokens_out, "
                    "provider, model, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.id, self.conversation, self.request[:2000], self.mode,
                     json.dumps(self.steps), self.answer[:4000], self.error,
                     int((time.time() - self.started) * 1000),
                     self.tokens_in, self.tokens_out,
                     config.PROVIDER, _model_name(), self.started_at),
                )
            _prune()
        except Exception:
            pass


def _model_name() -> str:
    return {
        "gemini": getattr(config, "GEMINI_MODEL", ""),
        "ollama": getattr(config, "OLLAMA_MODEL", ""),
        "claude": getattr(config, "CLAUDE_MODEL", ""),
    }.get(config.PROVIDER, "")


def _shorten(args: dict) -> dict:
    """Arguments can be whole pages of text. Keep the shape, drop the bulk."""
    out = {}
    for key, value in list(args.items())[:8]:
        text = str(value)
        out[key] = text[:120] + ("…" if len(text) > 120 else "")
    return out


def _prune() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    with _connect() as conn:
        conn.execute("DELETE FROM traces WHERE started_at < ?", (cutoff,))


# ==========================================================================
#  Reading them back
# ==========================================================================

def recent(limit: int = 50) -> list[dict]:
    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, conversation, request, mode, answer, error, ms, "
            "tokens_in, tokens_out, provider, model, started_at, steps "
            "FROM traces ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()

    out = []
    for row in rows:
        item = dict(row)
        try:
            item["steps"] = json.loads(item["steps"] or "[]")
        except Exception:
            item["steps"] = []
        out.append(item)
    return out


def get(trace_id: str) -> dict | None:
    init()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM traces WHERE id=?", (trace_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["steps"] = json.loads(item["steps"] or "[]")
    except Exception:
        item["steps"] = []
    return item


def summary() -> dict:
    """
    Headline numbers, for the top of the panel.

    The median, not the mean. One 60-second cold start on the local model
    drags an average far enough to make a fast system look slow, and you
    would go optimising the wrong thing.
    """
    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ms, mode, error FROM traces ORDER BY started_at DESC LIMIT 200"
        ).fetchall()

    if not rows:
        return {"turns": 0}

    times = sorted(r["ms"] or 0 for r in rows)
    modes: dict[str, int] = {}
    for r in rows:
        modes[r["mode"] or "?"] = modes.get(r["mode"] or "?", 0) + 1

    return {
        "turns": len(rows),
        "median_ms": times[len(times) // 2],
        "slowest_ms": times[-1],
        "failed": sum(1 for r in rows if r["error"]),
        "modes": modes,
    }

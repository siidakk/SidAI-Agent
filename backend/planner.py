"""
planner.py — think first, then act. And act on several things at once.

WHAT WAS WRONG WITH THE REACTIVE LOOP
-------------------------------------
Phase 2's loop improvises: ask the model, run whatever tool it names, feed
the result back, ask again. It works, and it's the right first thing to
build. But it has two structural problems that no amount of tuning fixes:

  1. EVERYTHING IS SEQUENTIAL. "Check my exam date, the weather, and flight
     prices" is three independent lookups. The reactive loop does them one
     after another, waiting for each. Three round trips for work that could
     take one.

  2. NOTHING IS INSPECTABLE. You find out what it's going to do by watching
     it do it. There's no moment where a plan exists that you could read,
     check, or reject before anything happens.

THE FIX: ASK FOR A PLAN
-----------------------
One model call returns EITHER a direct answer or a step graph:

    {"answer": "It's 4pm."}                     <- nothing to do

    {"steps": [                                  <- a plan
      {"id": "s1", "tool": "get_time",    "args": {},          "needs": []},
      {"id": "s2", "tool": "list_events", "args": {"days": 7}, "needs": ["s1"]},
      {"id": "s3", "tool": "search_web",  "args": {...},       "needs": []}
    ]}

`needs` is the whole point. s2 waits for s1; s3 waits for nothing. So s1 and
s3 start together. That structure - nodes with dependencies, no cycles - is
a **directed acyclic graph**, and it's how every build system, spreadsheet
and CI pipeline decides what can run in parallel.

WHY THIS ISN'T SLOWER
---------------------
It sounds like planning adds a call. It doesn't: the planner call REPLACES
the reactive loop's first call. A question needing no tools now costs ONE
model call instead of two.

  simple question    reactive: 2 calls   planned: 1 call
  3 independent      reactive: 4 calls,  planned: 2 calls,
  lookups                      4 waits            1 wait

WHAT THIS REPLACES
------------------
MAX_STEPS and the loop guard from Phase 2/4 were damage control for a model
improvising without a map. A plan is bounded and inspectable *before* it
runs, which is a better answer than catching runaway behaviour after.
"""

import asyncio
import json
import re
from typing import Any, AsyncIterator

from . import approvals, config, providers, tools

# A plan bigger than this is almost certainly the model confusing itself.
# Real requests decompose into a handful of steps; twenty means it has
# started planning the plan.
MAX_STEPS = 12

# How many times a failed plan may be rewritten. Once is usually enough -
# the second failure is rarely a different failure.
MAX_REPLANS = 1

# Steps run concurrently, but not unboundedly. Ten simultaneous PowerShell
# processes would be worse than useless on an 8 GB laptop.
MAX_PARALLEL = 4


PLANNER_INSTRUCTIONS = """
You are the planning stage of an assistant. Read the user's request and reply
with ONE JSON object and nothing else. No markdown, no code fences, no prose.

If you can answer without using any tools, reply:
  {"answer": "your reply here"}

"answer" is ONLY for information you already know, like a greeting or a fact.
NOTHING HAS RUN YET when you write this - you are choosing a plan, not
carrying it out. So "answer" must never report a completed action. Every one
of these is a lie at this point, and they have all actually happened:
  {"answer": "I've scheduled that for you."}
  {"answer": "Done, volume set to 30."}
  {"answer": "Aapka schedule set kar diya hai."}
If the request asks you to DO something, it needs "steps".

If tools are needed, reply with a plan:
  {"steps": [
     {"id": "s1", "tool": "tool_name", "args": {...}, "needs": []},
     {"id": "s2", "tool": "tool_name", "args": {...}, "needs": ["s1"]}
  ]}

Rules for plans:
- "needs" lists the ids this step must wait for. Steps that need nothing run
  AT THE SAME TIME, so leave "needs" empty unless a step genuinely requires
  another's output. This is the main thing that makes plans fast.
- To use an earlier step's result inside args, write {{s1}} and it will be
  replaced with that step's output. {{s1}} works ONLY inside a step's args.
  Never put it in "answer" - "answer" means you already know the reply. If
  you need a step's output, you need a plan, not an answer.
- Use the exact tool names and argument names from the list below.
- If you need to know the CURRENT date or time, call get_time first - you do
  not know it. But a time the user gave you ("at 07:30", "every morning",
  "Friday") is already known: use it directly, do NOT call get_time for it.
- Never reply with "I can't do that" because a step seems hard to order. If a
  tool in the list below does the job, put it in the plan.
- Keep plans small. Prefer three good steps to eight speculative ones.
"""


# ==========================================================================
#  Getting a plan out of the model
# ==========================================================================
def _extract_json(text: str) -> dict | None:
    """
    Pull a JSON object out of whatever the model actually sent.

    Models wrap JSON in ```json fences, add "Here's the plan:", or trail a
    sentence afterwards - despite being told not to. Insisting on clean
    output and failing otherwise would make this brittle for no reason, so
    we go and find the object.
    """
    text = text.strip()
    if not text:
        return None

    # Strip code fences if present.
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Last resort: the outermost {...} in the string.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def validate(plan: dict) -> tuple[list[dict], str | None]:
    """
    Check a plan is actually runnable. Returns (steps, error).

    Every one of these checks exists because a model will eventually produce
    that exact mistake. Validating here means a bad plan becomes a clear
    message the planner can fix, rather than a crash halfway through
    execution with some steps already done.
    """
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return [], "Plan has no steps."

    if len(steps) > MAX_STEPS:
        return [], f"Plan has {len(steps)} steps; the maximum is {MAX_STEPS}."

    seen_ids: set[str] = set()
    cleaned: list[dict] = []

    for raw in steps:
        if not isinstance(raw, dict):
            return [], "Every step must be an object."

        step_id = str(raw.get("id", "")).strip()
        name = str(raw.get("tool", "")).strip()

        if not step_id:
            return [], "A step is missing its id."
        if step_id in seen_ids:
            return [], f"Duplicate step id '{step_id}'."
        if name not in tools.REGISTRY:
            return [], (f"Step '{step_id}' uses unknown tool '{name}'. "
                        f"Available: {', '.join(sorted(tools.REGISTRY))}")

        args = raw.get("args") or {}
        if not isinstance(args, dict):
            return [], f"Step '{step_id}' has args that aren't an object."

        needs = raw.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]                  # a lone id instead of a list
        if not isinstance(needs, list):
            return [], f"Step '{step_id}' has a bad 'needs'."

        seen_ids.add(step_id)
        cleaned.append({
            "id": step_id, "tool": name,
            "args": args, "needs": [str(n) for n in needs],
        })

    # Dependencies must exist...
    for step in cleaned:
        for need in step["needs"]:
            if need not in seen_ids:
                return [], f"Step '{step['id']}' needs '{need}', which doesn't exist."

    # ...and must not form a cycle. Without this check, s1 needing s2 and s2
    # needing s1 would simply hang forever: neither is ever ready to run.
    if _has_cycle(cleaned):
        return [], "The plan has a circular dependency."

    return cleaned, None


def _has_cycle(steps: list[dict]) -> bool:
    """
    Kahn's algorithm: repeatedly remove steps whose dependencies are all
    satisfied. If any remain when nothing more can be removed, they depend
    on each other and the graph has a cycle.
    """
    remaining = {s["id"]: set(s["needs"]) for s in steps}
    done: set[str] = set()

    while remaining:
        ready = [sid for sid, needs in remaining.items() if needs <= done]
        if not ready:
            return True                       # stuck: everything left is circular
        for sid in ready:
            done.add(sid)
            del remaining[sid]
    return False


def levels(steps: list[dict]) -> list[list[dict]]:
    """
    Group steps into waves that can each run entirely in parallel.

    Level 0 is everything with no dependencies. Level 1 is everything whose
    dependencies are all in level 0. And so on.

    This is the payoff of the whole phase: the number of LEVELS is how many
    round trips you actually wait for, not the number of steps.
    """
    by_id = {s["id"]: s for s in steps}
    remaining = {s["id"]: set(s["needs"]) for s in steps}
    done: set[str] = set()
    out: list[list[dict]] = []

    while remaining:
        wave = [sid for sid, needs in remaining.items() if needs <= done]
        if not wave:
            break                             # cycle; validate() catches this
        out.append([by_id[sid] for sid in wave])
        for sid in wave:
            done.add(sid)
            del remaining[sid]
    return out


# ==========================================================================
#  Substituting earlier results into later arguments
# ==========================================================================
def substitute(args: dict, results: dict[str, str]) -> dict:
    """
    Replace {{s1}} in a step's arguments with step s1's output.

    This is what lets a plan be written before any of it has run. The
    planner doesn't know today's date when it writes step 2 - it just says
    "use whatever step 1 returns".
    """
    def fill(value: Any) -> Any:
        if isinstance(value, str):
            def replace(match: re.Match) -> str:
                key = match.group(1).strip()
                # Unknown reference: leave the placeholder visible rather
                # than silently substituting "". A visible {{s9}} in the
                # trace tells you what went wrong; an empty string doesn't.
                return results.get(key, match.group(0))
            return re.sub(r"\{\{\s*(\w+)\s*\}\}", replace, value)
        if isinstance(value, dict):
            return {k: fill(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fill(v) for v in value]
        return value

    return {k: fill(v) for k, v in args.items()}


# ==========================================================================
#  Execution
# ==========================================================================
async def make_plan(
    messages: list[dict],
    system: str,
    schemas: list[dict],
) -> tuple[dict | None, str]:
    """
    Ask the model for a plan (or a direct answer). Returns (parsed, raw_text).

    Note we pass the tools as TEXT in the prompt rather than as real tool
    definitions. We don't want the model to CALL anything here - we want it
    to describe what should be called. Handing it working tools at this
    point invites it to just start doing things, which is the behaviour
    we're replacing.
    """
    menu = "\n".join(
        f"- {s['name']}({', '.join(s['parameters']['properties'])}): "
        f"{s['description'].splitlines()[0]}"
        for s in schemas
    )

    provider = providers.get(config.PROVIDER)
    planning_system = (
        f"{system}\n\n{PLANNER_INSTRUCTIONS}\n\nTOOLS YOU MAY USE:\n{menu}"
    )

    raw = ""
    # LOW TEMPERATURE, on purpose.
    #
    # Planning is a structured task: given the same request and the same
    # tools, the plan should be the same plan. At Gemini's default (~1.0) it
    # was not - the identical request produced a correct schedule_task plan
    # twice and "Sorry, I can't do that yet" the third time. The eval suite
    # caught that; three manual tries would probably not have.
    #
    # Creativity belongs in the ANSWER, not in deciding which tools exist.
    async for event in provider.stream_reply(
            messages, system=planning_system, temperature=0.1):
        if event["type"] == "text":
            raw += event["text"]

    return _extract_json(raw), raw


async def execute(
    steps: list[dict],
    approve=None,
    task_id: str | None = None,
) -> AsyncIterator[dict]:
    """
    Run a validated plan, level by level, with each level in parallel.

    Yields the same event vocabulary the reactive loop uses, plus:
        {"type": "step_start",  "id", "tool", "args"}
        {"type": "step_done",   "id", "output", "ok"}

    On the last event a "results" dict is attached so the caller can build
    the final answer from every step's output.
    """
    results: dict[str, str] = {}
    failures: list[str] = []

    # Cap concurrency. Four PowerShell processes at once is plenty; ten
    # would thrash an 8 GB laptop harder than doing them one at a time.
    gate = asyncio.Semaphore(MAX_PARALLEL)

    for wave in levels(steps):
        # Substitute NOW, not when the plan was written - the values these
        # placeholders refer to only exist once earlier levels have run.
        prepared = [
            {**step, "args": substitute(step["args"], results)} for step in wave
        ]

        # Danger steps are approved BEFORE the wave starts, one at a time.
        # Firing four approval cards simultaneously would be chaos, and
        # approving something while other steps are already running means
        # you're consenting to a situation that has already changed.
        for step in prepared:
            spec = tools.REGISTRY.get(step["tool"])

            # A background job brings its own `approve`. Emitting approval
            # cards into a stream nobody is reading would strand the job
            # forever, so hand the asking over to tools.run() instead.
            if approve is not None:
                step["_delegate"] = True
                continue

            if spec and spec.tier == "danger":
                request = approvals.create(step["tool"], step["args"])
                yield {
                    "type": "approval_request",
                    "id": request.id,
                    "tool": step["tool"],
                    "summary": approvals.describe(step["tool"], step["args"]),
                }
                granted = await approvals.wait_for(request)
                yield {"type": "approval_result", "id": request.id, "granted": granted}
                step["_granted"] = granted

        for step in prepared:
            yield {"type": "step_start", "id": step["id"],
                   "tool": step["tool"], "args": step["args"]}

        async def run_one(step: dict) -> tuple[str, str]:
            async with gate:
                # Delegated: tools.run() does the asking, through the
                # caller's callback. That's how a background job pauses for
                # approval without a live connection to ask down.
                if step.get("_delegate"):
                    return step["id"], await tools.run(
                        step["tool"], step["args"],
                        approve=approve, task_id=task_id,
                    )

                if step["tool"] in tools.REGISTRY and \
                   tools.REGISTRY[step["tool"]].tier == "danger":
                    if not step.get("_granted"):
                        return step["id"], "The user did not approve this step."
                    return step["id"], await tools.run(
                        step["tool"], step["args"],
                        approve=_granted, task_id=task_id,
                    )
                return step["id"], await tools.run(
                    step["tool"], step["args"], task_id=task_id
                )

        # THE PARALLEL BIT. gather starts every coroutine in this wave at
        # once and waits for all of them. Three 2-second lookups take two
        # seconds, not six.
        done = await asyncio.gather(*(run_one(s) for s in prepared))

        for step_id, output in done:
            results[step_id] = output
            failed = output.lstrip().lower().startswith(("error", "refused", "could not"))
            if failed:
                failures.append(f"{step_id}: {output[:200]}")
            yield {"type": "step_done", "id": step_id,
                   "output": output[:500], "ok": not failed}

    yield {"type": "plan_complete", "results": results, "failures": failures}


async def _granted(tool_name: str, arguments: dict) -> bool:
    """Approval callback for a step the user already approved."""
    return True

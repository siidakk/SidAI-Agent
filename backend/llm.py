"""
llm.py — THE AGENT LOOP.

This is the most important file in the project. Everything before it was
plumbing; everything after it is more tools. The ~40 lines in `stream_reply`
below are the entire difference between a chatbot and an agent.

THE LOOP, IN ENGLISH
--------------------
    1. Send the conversation to the model, along with a menu of tools.
    2. The model replies. It either:
         (a) writes an answer          -> we're done, stop.
         (b) asks to call tool(s)      -> keep going.
    3. Run the tools it asked for. Get real results.
    4. Append the model's request AND the results to the conversation.
    5. Go back to step 1.

That's it. No framework, no magic. The model can't run code — it can only ask.
We are the hands.

WHY IT LOOPS INSTEAD OF DOING ONE ROUND
---------------------------------------
Because step 3's results often reveal that more work is needed. Ask "what's
in my project folder and how big is the biggest file?" and the model must
first call `list_files`, READ the answer, and only then know which file to
ask about. It cannot plan that in one shot — it doesn't know the folder
contents until we tell it.

That's also the seed of Phase 5. Right now the model improvises one step at a
time. Later we'll have it write the whole plan up front, so independent steps
can run in parallel.
"""

import json
import re
from typing import AsyncIterator

from . import approvals, config, memory, planner, providers, tools

# Safety valve. If the model gets stuck calling tools forever — and small
# models absolutely do — we stop after this many rounds. Without this, a
# confused model can burn your entire quota in one message.
MAX_STEPS = 6


async def _already_granted(tool_name: str, arguments: dict) -> bool:
    """
    The approval callback used AFTER the user has already said yes.

    tools.run() checks the tier again and asks its callback. We've done the
    asking, so this just says yes. It exists so that tools.run() can keep its
    fail-closed rule - no callback means refuse - without the loop needing a
    special "skip the check" flag, which is exactly the kind of flag that
    later gets set by accident.
    """
    return True


async def check() -> dict:
    """Ask the active provider whether it's ready. Used by /api/health."""
    p = providers.get(config.PROVIDER)
    result = await p.check()
    return {
        "provider": config.PROVIDER,
        "tools": len(tools.REGISTRY),
        **result,
    }


async def react(
    messages: list[dict],
    system: str,
    approve=None,
    task_id: str | None = None,
) -> AsyncIterator[dict]:
    """
    Run the agent loop, streaming events out as they happen.

    `messages` uses OUR format, which every provider translates from:

        {"role": "user",      "content": "..."}
        {"role": "assistant", "content": "...", "tool_calls": [{id, name, input}]}
        {"role": "tool",      "tool_call_id": "...", "name": "...", "content": "..."}

    Events yielded (these become SSE messages to the browser):

        {"type": "text",        "text": "..."}          words of the reply
        {"type": "tool_call",   "name", "input"}        about to run a tool
        {"type": "tool_result", "name", "output"}       what it returned
        {"type": "done",        "usage": {...}}         finished
    """
    provider = providers.get(config.PROVIDER)
    schemas = tools.schemas()

    # We work on a copy. The caller's list — and the browser's history —
    # should only ever contain the user-visible conversation, not the
    # tool chatter we generate along the way.
    working = list(messages)

    total_in = total_out = 0

    # ---- LOOP GUARD ---------------------------------------------------
    # Remember every (tool, arguments) pair we've already run this turn.
    #
    # MAX_STEPS alone is not enough. It caught a real failure: search_web
    # started returning "no results" for everything (DuckDuckGo had begun
    # blocking us), and the model called it SIX TIMES with the same query
    # before hitting the ceiling. Six identical calls, six identical
    # failures, one wasted quota and a useless answer.
    #
    # A model that gets an unsatisfying result naturally tries again. It has
    # no memory of "I already did that" beyond what's in the transcript, and
    # a failure message is easy to skim past. So the loop enforces it.
    seen_calls: dict[str, str] = {}

    for step in range(MAX_STEPS):
        text_so_far = ""
        calls: list[dict] = []

        # ---- 1. ask the model -------------------------------------------
        async for event in provider.stream_reply(
            working, tools=schemas, system=system
        ):

            if event["type"] == "text":
                text_so_far += event["text"]
                yield event                       # stream words to the browser

            elif event["type"] == "tool_call":
                calls.append(event)               # collect, don't run yet

            elif event["type"] == "done":
                total_in += event["usage"]["input_tokens"]
                total_out += event["usage"]["output_tokens"]

        # ---- 2. no tools requested? then it answered. stop. --------------
        if not calls:
            yield {
                "type": "done",
                "usage": {"input_tokens": total_in, "output_tokens": total_out},
                "steps": step + 1,
            }
            return

        # ---- 3. record what the model asked for --------------------------
        # This MUST go into the conversation before the results, or the model
        # sees answers to questions it doesn't remember asking, and gets
        # thoroughly confused.
        working.append({
            "role": "assistant",
            "content": text_so_far,
            "tool_calls": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "input": c["input"],
                    # Opaque provider baggage (e.g. Gemini's thoughtSignature).
                    # The loop never inspects it, just hands it back.
                    "meta": c.get("meta"),
                }
                for c in calls
            ],
        })

        # ---- 4. actually run them ----------------------------------------
        for call in calls:
            yield {
                "type": "tool_call",
                "name": call["name"],
                "input": call["input"],
            }

            spec = tools.REGISTRY.get(call["name"])

            # Already run this exact call? Don't run it again - hand back
            # what it returned last time, plus an explicit instruction to
            # stop. Repeating a side-effecting tool would be worse still:
            # imagine two identical calendar events.
            signature = f"{call['name']}::{json.dumps(call['input'], sort_keys=True)}"
            if signature in seen_calls:
                output = (
                    f"You already called {call['name']} with exactly these "
                    f"arguments this turn, and it returned:\n\n"
                    f"{seen_calls[signature][:400]}\n\n"
                    f"Do NOT call it again. Use that result, try something "
                    f"genuinely different, or tell the user it didn't work."
                )
                yield {"type": "tool_result", "name": call["name"],
                       "output": "(repeat call blocked)"}
                working.append({
                    "role": "tool", "tool_call_id": call["id"],
                    "name": call["name"], "content": output,
                })
                continue

            if spec and spec.tier == "danger" and approve is not None:
                # A caller supplied its own way of asking - a background job,
                # which parks itself instead of blocking a live stream. Let
                # tools.run() drive it rather than doing our own thing here.
                output = await tools.run(
                    call["name"], call["input"], approve=approve, task_id=task_id
                )

            elif spec and spec.tier == "danger":
                # ---- ask the human -------------------------------------
                # The request has to REACH the browser before we block, so it
                # is emitted here rather than from inside a callback: a
                # generator cannot yield from within a nested function.
                request = approvals.create(call["name"], call["input"])
                yield {
                    "type": "approval_request",
                    "id": request.id,
                    "tool": call["name"],
                    "summary": approvals.describe(call["name"], call["input"]),
                }

                # This awaits the user's tap. The whole request hangs here,
                # which is fine — the server is async and other requests
                # carry on being served meanwhile.
                granted = await approvals.wait_for(request)
                yield {"type": "approval_result", "id": request.id, "granted": granted}

                if granted:
                    output = await tools.run(
                        call["name"], call["input"],
                        approve=_already_granted, task_id=task_id
                    )
                else:
                    output = (
                        f"The user did not approve '{call['name']}', so it was "
                        f"not run. Do not retry unless they ask. Offer an "
                        f"alternative if there is a safe one."
                    )
            else:
                # tools.run() never raises — errors come back as text so the
                # model can read them and correct itself.
                output = await tools.run(
                    call["name"], call["input"], approve=approve, task_id=task_id
                )

            seen_calls[signature] = output

            yield {
                "type": "tool_result",
                "name": call["name"],
                "output": output[:500],           # UI preview; model gets it all
            }

            working.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": output,
            })

        # ---- 5. loop back round ------------------------------------------

    # Fell out of the for loop: MAX_STEPS reached without a final answer.
    yield {
        "type": "text",
        "text": (
            f"\n\n[Stopped after {MAX_STEPS} tool rounds without finishing. "
            f"Try asking for something simpler, or one thing at a time.]"
        ),
    }
    yield {
        "type": "done",
        "usage": {"input_tokens": total_in, "output_tokens": total_out},
        "steps": MAX_STEPS,
        "truncated": True,
    }


# ==========================================================================
#  THE ENTRY POINT — plan first, fall back to reacting
# ==========================================================================
async def stream_reply(
    messages: list[dict],
    approve=None,
    task_id: str | None = None,
) -> AsyncIterator[dict]:
    """
    Answer a message. Plans when it can, improvises when it must.

    THE SHAPE OF IT
    ---------------
        1. Build the system prompt, including relevant memories.
        2. Ask the planner for either a direct answer or a step graph.
        3. Direct answer  -> stream it. ONE model call total.
        4. Valid plan     -> run it, levels in parallel, then summarise.
        5. Unusable plan  -> fall back to react(), the Phase 2 loop.

    STEP 5 IS THE IMPORTANT ONE. Planning depends on the model producing
    valid JSON describing a graph, and a 3B local model frequently cannot.
    Rather than fail, we drop back to the loop that only needs tool calling.
    Sid gets slower, not broken.

    A new capability should never remove an old one. Degrade, don't collapse.
    """
    # ---- memory: what's worth knowing for THIS message ------------------
    # Lives here rather than in react() because this function now owns the
    # system prompt, and both paths need the same one.
    last_user = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
    )
    context = await memory.build_context(last_user) if last_user else ""
    system = config.SYSTEM_PROMPT + context

    # DRY RUN has to be stated in the prompt, not just enforced in the tools.
    #
    # Without this the model reads "[dry run] Would have called set_volume"
    # and still replies "I've set the volume to 90" - it treats the tool
    # result as success because that's the usual shape. A dry run that
    # reports success is worse than no dry run at all: you'd trust it.
    from . import settings

    if settings.get("dry_run"):
        system += (
            "\n\nDRY RUN IS ON. Tools that would change anything are NOT "
            "running - they only report what they would have done. You MUST "
            "say clearly that nothing actually happened. Never claim to have "
            "done something. Say 'I would...', not 'I have...'."
        )

    schemas = tools.schemas()

    # ---- 1. ask for a plan ---------------------------------------------
    try:
        parsed, raw = await planner.make_plan(messages, system, schemas)
    except Exception as exc:
        # The planner itself failed (network, quota). Don't lose the turn.
        yield {"type": "notice", "text": f"planner unavailable, improvising"}
        async for event in react(messages, system, approve, task_id):
            yield event
        return

    # ---- 2. it answered directly. one call, done. -----------------------
    #
    # Two guards here, both learned the hard way. A scheduled job once
    # finished with the answer "{{s1}}" - the model had replied
    # {"answer": "{{s1}}"}, referring to a step it never asked us to run.
    # A placeholder is a promise about work; if the work isn't in this
    # object, the "answer" is not an answer.
    #
    # And when the model sends BOTH an answer and steps, the steps win:
    # asking for tools means it knew it couldn't answer from memory.
    if (parsed
            and isinstance(parsed.get("answer"), str)
            and parsed["answer"].strip()
            and not parsed.get("steps")
            and not re.search(r"\{\{\s*\w+\s*\}\}", parsed["answer"])):
        yield {"type": "text", "text": parsed["answer"].strip()}
        yield {"type": "done", "usage": {"input_tokens": 0, "output_tokens": 0},
               "mode": "direct", "steps": 0}
        return

    # ---- 3. no usable JSON at all: fall back ----------------------------
    if not parsed or "steps" not in parsed:
        async for event in react(messages, system, approve, task_id):
            yield event
        return

    # ---- 4. validate, and give the planner one chance to fix it ---------
    steps, error = planner.validate(parsed)

    if error:
        retry_messages = messages + [{
            "role": "user",
            "content": (f"That plan was rejected: {error}\n"
                        f"Reply with a corrected JSON plan, nothing else."),
        }]
        try:
            parsed, _ = await planner.make_plan(retry_messages, system, schemas)
            steps, error = planner.validate(parsed or {})
        except Exception:
            error = error or "planner failed"

    if error:
        # Still broken after a retry. The reactive loop needs no JSON at all,
        # so it can still do the job.
        async for event in react(messages, system, approve, task_id):
            yield event
        return

    # ---- 5. run it ------------------------------------------------------
    yield {"type": "plan", "steps": [
        {"id": s["id"], "tool": s["tool"], "needs": s["needs"]} for s in steps
    ], "levels": len(planner.levels(steps))}

    results: dict[str, str] = {}
    failures: list[str] = []

    async for event in planner.execute(steps, approve=approve, task_id=task_id):
        if event["type"] == "plan_complete":
            results = event["results"]
            failures = event["failures"]
        else:
            yield event

    # ---- 6. turn the results into an actual answer ----------------------
    # The plan produced raw tool output. Someone still has to read it and
    # reply in English, so this is the second (and final) model call.
    transcript = "\n\n".join(
        f"[{s['id']}] {s['tool']} -> {results.get(s['id'], '(no result)')[:1500]}"
        for s in steps
    )
    summary_messages = messages + [{
        "role": "user",
        "content": (
            f"You ran this plan and got these results:\n\n{transcript}\n\n"
            f"Answer the original request using them. Be direct. Do not "
            f"describe the plan or mention step ids."
            + (f"\n\nSome steps failed: {'; '.join(failures)}. "
               f"Say plainly what didn't work." if failures else "")
        ),
    }]

    provider = providers.get(config.PROVIDER)
    async for event in provider.stream_reply(summary_messages, system=system):
        if event["type"] == "text":
            yield event
        elif event["type"] == "done":
            yield {**event, "mode": "planned", "steps": len(steps),
                   "levels": len(planner.levels(steps))}

# Phase 5 — It Plans

> **Aim:** stop improvising one step at a time. Write the whole plan first,
> as a graph with dependencies, and run independent steps at the same time.

This is the phase you called the differentiator — the orchestration part.

---

## 1. What you built

| File | What it is |
|---|---|
| `backend/planner.py` | Plan generation, validation, DAG levels, parallel executor |
| `backend/llm.py` | `stream_reply` now plans first; the old loop became `react()` |
| `frontend/app.js` | Renders the plan as waves before anything runs |

---

## 2. What was wrong with the reactive loop

Phase 2's loop works, and it was the right first thing to build. But it has
two problems that no amount of tuning fixes:

**Everything is sequential.** *"Check my exam date, the weather, and flight
prices"* is three independent lookups. The loop does them one after another.

**Nothing is inspectable.** You find out what it's going to do by watching it
do it. There is no moment where a plan exists that you could read before
anything happens.

---

## 3. The fix: ask for a plan, not an action

One model call returns **either** a direct answer **or** a step graph:

```json
{"answer": "It's 4pm."}
```
```json
{"steps": [
  {"id": "s1", "tool": "get_time",    "args": {},          "needs": []},
  {"id": "s2", "tool": "list_events", "args": {"days": 7}, "needs": ["s1"]},
  {"id": "s3", "tool": "search_web",  "args": {...},       "needs": []}
]}
```

**`needs` is the entire point.** s2 waits for s1; s3 waits for nothing — so
s1 and s3 start together.

Nodes with dependencies and no cycles is a **directed acyclic graph (DAG)**.
It's how every build system, spreadsheet and CI pipeline decides what can run
at once. You've now written one.

### Planning does not cost an extra call

This surprises people. The planner call **replaces** the reactive loop's
first call:

| | reactive | planned |
|---|---|---|
| Simple question | 2 calls | **1 call** |
| 3 independent lookups | 4 calls, 4 waits | 2 calls, **1 wait** |

Verified — *"hello, who are you?"* → `mode=direct`, 1.9s, no plan generated.

---

## 4. Levels: the idea that makes it parallel

```python
def levels(steps):
    # wave 0 = everything with no dependencies
    # wave 1 = everything whose dependencies are all in wave 0
    # ...
```

**The number of waves is how many round trips you actually wait for — not
the number of steps.** Verified:

| Plan | Steps | Waves |
|---|---|---|
| 3 independent | 3 | **1** |
| a chain | 3 | 3 |
| diamond (a → b,c → d) | 4 | 3, with `b+c` together |

`asyncio.gather` starts every step in a wave at once. Concurrency is capped
at 4 — ten simultaneous PowerShell processes would thrash an 8 GB laptop
harder than doing them one at a time.

---

## 5. ⚠️ The honest measurement

First real test, three tools, both paths:

```
REACTIVE (Phase 2)   2.5s   3 tools
PLANNED  (Phase 5)   2.6s   3 tools     <- 0.2s SLOWER
```

**No speedup.** Two reasons, both worth understanding:

1. `get_time`, `system_info` and `get_volume` are local and **instant**.
   Running three zero-millisecond operations in parallel saves zero
   milliseconds.
2. Gemini already supports **parallel tool calling** — it issued all three
   in one assistant turn, so the reactive loop wasn't actually serial here.

Isolating the executor with three deliberately slow (2s) steps:

```
SEQUENTIAL   6.0s
PARALLEL     2.0s   in 1 wave      <- 3x
CHAINED      6.0s   in 3 waves     <- correctly NOT parallel
```

> **Parallelism only pays when the steps are slow.** Network calls, shell
> commands, API lookups — yes. Local instant functions — no.

That last row matters too: when steps genuinely depend on each other the
planner does *not* parallelise them, and that's correct behaviour, not a
missed optimisation.

**Don't report a speedup you haven't measured.** The honest version of this
phase is "3× on slow independent work, nothing on fast work, and a plan you
can read either way."

---

## 6. Validation: every check exists because a model made that mistake

`validate()` rejects, with a message the planner can act on:

| Rejected | Why it matters |
|---|---|
| unknown tool | `make_coffee` isn't real |
| missing dependency | `needs: ["s9"]` when there's no s9 |
| duplicate ids | two steps called `s1` |
| **circular dependency** | s1 needs s2, s2 needs s1 |
| too many steps | >12 means it's planning the plan |
| empty plan | nothing to run |

The cycle check is the one that would otherwise be a *hang*, not an error:
neither step is ever ready, so the executor waits forever. It uses **Kahn's
algorithm** — repeatedly remove steps whose dependencies are satisfied; if
any remain when nothing more can be removed, they depend on each other.

Validating up front means a bad plan becomes a clear message, rather than a
crash halfway through with some steps already executed.

---

## 7. Passing results between steps

A plan is written before any of it has run, so step 2 can't know what step 1
returned. Placeholders bridge that:

```json
{"id": "s2", "tool": "search_web", "args": {"query": "weather on {{s1}}"}}
```

Substitution happens **when the wave starts**, not when the plan was written.

One deliberate choice: an unknown placeholder is **left visible** rather than
replaced with an empty string. `{{s9}}` in the trace tells you exactly what
went wrong; a silent empty string tells you nothing.

---

## 8. Getting JSON out of a model that was told not to add prose

`_extract_json` tries three things: parse it, strip ``` fences and parse,
then find the outermost `{...}`. Verified:

```
clean     {"answer":"hi"}                      -> parsed
fenced    ```json {"answer":"hi"} ```          -> parsed
chatty    Sure! Here is the plan: {...} Hope!  -> parsed
garbage   I cannot do that                     -> None
```

Models add fences and preambles despite explicit instructions. Being strict
and failing would be brittle for no benefit — so we go and find the object.

---

## 9. The fallback is the most important part

Planning requires the model to emit valid JSON describing a graph. **A 3B
local model frequently cannot.** So when planning fails, `stream_reply`
drops back to `react()` — the Phase 2 loop, which only needs tool calling.

Three ways it falls back, all verified:

```
planner returned garbage  -> react: tools=['get_time'] -> "4:40 AM."
planner raised an error   -> react: tools=['get_time'] -> "4:40 AM."
plan failed validation    -> one retry, then react
```

> **A new capability must never remove an old one.** Sid gets slower, not
> broken.

The retry is worth noting: a rejected plan goes back to the planner *with
the specific error*, once. Once, not forever — the second failure is rarely
a different failure.

---

## 10. Seeing the plan

The UI draws the plan **before anything runs**, grouped into waves:

```
3 STEPS IN 1 WAVE
│ get_time   system_info   get_volume
```

Steps on the same row start together. A flat list would hide the one thing
that makes a plan different from the old loop. Each chip goes cyan while
running, ✓ green on success, ✗ red on failure, with the output on hover.

That moment — a plan exists, nothing has happened yet — is the whole point.
Phase 7's approvals gate individual dangerous steps; this gates your
*understanding* of the whole thing.

---

## 11. Approvals inside a plan

Danger steps are approved **before their wave starts**, one at a time.

Two reasons. Four approval cards appearing at once would be chaos. And
approving something while other steps are already running means consenting
to a situation that has already changed underneath you.

---

## 12. Try it

- *"hello"* → direct answer, no plan, one call
- *"Tell me the time, my disk space, and my volume"* → 3 steps, 1 wave
- *"What's on my calendar tomorrow?"* → get_time → list_events, 2 waves
- *"Check my email and my calendar and tell me if I'm busy"* → parallel
- Switch to `ollama` and try the same thing — watch it fall back to `react`

---

## 13. Exercises

1. **Watch the waves.** Ask for three unrelated things. Count the rows.
2. **Force a chain.** Ask for something where step 2 genuinely needs step 1.
   Two rows, not one — and that's correct.
3. **Break the planner.** Make `make_plan` return `None`. Everything still
   works, just reactively. That's the fallback earning its place.
4. **Feel the model gap.** Same question on `gemini` and on `ollama`. This
   is the phase where a 3B model visibly falls apart — exactly the
   diagnostic the provider switcher was built for.
5. **Add a slow tool** (`await asyncio.sleep(2)`) and plan three of them.
   6s → 2s. That's where parallelism actually lives.

---

## 14. Glossary

| Term | Plain meaning |
|---|---|
| **DAG** | Steps with dependencies and no cycles |
| **Topological order** | An order where every step comes after its dependencies |
| **Wave / level** | Steps that can all run at the same time |
| **Kahn's algorithm** | Peel off ready steps repeatedly; leftovers mean a cycle |
| **`asyncio.gather`** | Start many async things at once, wait for all |
| **Semaphore** | A cap on how many run concurrently |
| **Replanning** | Rewriting a plan after it failed, given the error |
| **Graceful degradation** | Falling back to something worse rather than failing |

---

## 15. What Phase 6 changes

Plans run, but you have to sit and watch them. Close the window mid-plan and
it's gone.

Phase 6 moves execution to a **background job queue**: `POST /task` returns a
`run_id` immediately, a worker executes the plan, and you can close the
laptop and check the result from your phone. That's what makes *"book this
while I'm in class"* possible.

**Check you can answer:**

- Why doesn't planning cost an extra model call?
- Why is the number of *waves* what matters, not the number of steps?
- Why did the first benchmark show no speedup, and when does parallelism help?
- What would happen without the cycle check?
- Why is an unknown `{{s9}}` left visible instead of blanked?
- Why does the fallback to `react()` matter more than the planner itself?

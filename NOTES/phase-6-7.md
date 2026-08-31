# Phase 6 + 7 — It Works While You're Away, and Keeps Receipts

> **Phase 6:** start something, close the laptop, check the result from your
> phone.
> **Phase 7:** an append-only record of everything it did, a policy you can
> tighten, and a dry-run mode.

They're one chapter, not two. The moment work happens unattended, a log stops
being nice-to-have and becomes the only way to answer *"what did it do at
3am?"*

---

## 1. What you built

| File | What it is |
|---|---|
| `backend/jobs.py` | The job queue, worker and status tracking |
| `backend/audit.py` | Append-only record of every tool call |
| `backend/settings.py` | `dry_run`, `confirm_act` |
| `backend/tools/__init__.py` | Policy + audit, at the one choke point |
| `backend/main.py` | `/api/task`, `/api/tasks`, `/api/audit` |
| `frontend/` | Background-send button, Activity panel |

Two new databases: `data/jobs.db`, `data/audit.db`.

---

## 2. The one idea in Phase 6

> **The request that STARTS the work is no longer the request that WAITS for
> it.**

Everything until now ran inside an HTTP request. Ask, watch it stream, get an
answer. Close the tab halfway and the work dies with the connection.

```
POST /api/task   ->  returns a run_id in 0.09s, connection closes
        (worker keeps going)
GET  /api/task/{id}  ->  status, events, answer
```

Verified: the POST returned in **0.09 seconds**, the task finished ~3s later,
and the answer was there when polled. That separation is what makes a service
different from a script.

### Why SQLite and not Celery

Celery and RQ assume many machines and a broker to coordinate them. This is
one laptop and one user. A table plus an `asyncio` task does the job, and it
**survives a restart** because the rows are on disk — the only property of a
"real" queue that actually matters here.

Same call as Phase 4 choosing SQLite over Postgres. Reach for the smaller
tool until the bigger one is justified.

### The restart problem

A job marked `running` when the process starts is a lie — whatever was
running it is gone. So `init()` marks those `failed` with
*"Sid restarted while this was running"*. Leaving rows that claim to be in
progress forever is worse than admitting the work was lost.

### One line that prevents a baffling bug

```python
_running[job_id] = task
```

`asyncio.create_task` returns a handle, and if nothing holds a reference the
task can be **garbage-collected mid-flight**. Work simply stops, with no
error anywhere. Keeping the reference is the fix.

---

## 3. Approvals when nobody is watching

This is the interesting part of combining the two phases.

A background job hitting a `danger` tool can't block on a UI that isn't open.
Three options:

| | |
|---|---|
| Auto-deny | Safe, but background jobs can never do anything real |
| Auto-approve | Absolutely not |
| **Pause and wait** | The job parks, the task list shows "needs you", it resumes when you tap |

The third is what *"do it while I'm in class"* actually requires. So a job's
approval timeout is **an hour**, not three minutes — you started it in order
to walk away.

Verified:

```
status = needs_approval        <- job parked itself
it wants: Get-Date
...approving from a separate connection, as your phone would...
resumed -> done: 30 August 2026 03:47:58
```

The mechanism: `jobs.py` supplies its own `approve` callback, which sets the
job status and waits. `planner.execute` and `react` both check whether a
caller brought one — if so they **delegate** rather than emitting approval
cards into a stream nobody is reading. That would strand the job forever.

---

## 4. The audit log, and why it's append-only

There is no update and no delete in `audit.py`. Not because deleting is hard,
but because **a log you can edit is not evidence.** The moment something can
quietly rewrite its own history, the log stops answering the only question it
exists for.

That's also why it's separate from `memory.py`. Memory is *for* the agent and
is allowed to forget — `forget()` is a normal tool. This is *about* the
agent, and it never forgets.

Every call is recorded: tool, tier, arguments, result, whether it was
approved, how long it took, and which task it belonged to.

```
03:51 AM  get_volume  95ms
03:51 AM  get_time  0ms
03:49 AM  set_volume  [dry-run]

10 actions logged · 0 failed · 0 denied
```

**Failures are logged too.** A log that records only successes is worse than
no log: it tells you a comforting story instead of what happened.

### `record()` never raises

A failure to log must not break the thing being logged. An agent that crashes
because its audit database is locked is worse than one with a gap in its
records — the gap is visible, the crash loses the work.

---

## 5. Dry run, and the bug that made it dangerous

`dry_run` stops anything that would change something. `act` and `danger`
tools report what they *would* have done; `read` tools still run, because
seeing real data is how you judge whether a plan is sensible — and looking at
things changes nothing.

**First attempt was actively dangerous.** The tool correctly returned
`[dry run] Would have called set_volume(...). Nothing ran.` and the model
replied:

> *"Volume 90 par set kar diya hai"* — **"I've set the volume to 90."**

It hadn't. The model saw a tool result and pattern-matched it to success.

> **A dry run that reports success is worse than no dry run, because you'd
> trust it.**

The fix is that dry run now has to be stated **in the prompt**, not only
enforced in the tools:

```
DRY RUN IS ON. Tools that would change anything are NOT running...
Say 'I would...', not 'I have...'.
```

After:

> *"[Dry Run] I would have set your volume to 90, but nothing actually
> happened."*

And the volume was genuinely unchanged at 58%.

The general lesson: **enforcing a rule in code isn't enough if the model
narrates the outcome.** Anything that changes what an action *means* has to
reach the model too.

---

## 6. One choke point

Every policy decision lives in `tools.run()` — dry run, approval, audit —
because it is the single function every tool call passes through, whether it
came from the reactive loop, a plan, or a background job.

```python
if spec.tier != "read" and settings.get("dry_run"):     # describe, don't do
needs_approval = spec.tier == "danger" or (             # policy
    spec.tier == "act" and settings.get("confirm_act"))
audit.record(...)                                       # always
```

Spreading these across three call sites would guarantee that one of them
eventually forgets. **Enforce at the narrowest waist.**

`confirm_act` is off by default — needing permission to change the volume
would make Sid unusable — but it's there for a tighter leash.

---

## 7. The UI

**Clock button** next to send: run in the background. You get a task id and
can close the window.

**Activity panel** (list icon):
- **Tasks** — status, request, answer. A parked job shows *Review and
  approve* as the loudest thing on the row.
- **Log** — the audit trail, dense on purpose because you scan it rather than
  read it. Colour-coded by tier, hover for the full result.
- **Toggles** for dry run and confirm-every-action.

The button carries a dot: cyan when something is running, **amber and
blinking when a task needs you**. A job waiting silently forever is exactly
the failure this prevents.

---

## 8. Try it

- Type something, hit the **clock** instead of send. Close the window. Reopen
  and check Activity.
- *"Run the command Get-Date"* in the background → parks → approve from the
  panel → resumes.
- Turn on **dry run**, then *"set my volume to 90"*. It says what it would
  have done. Check the volume — unchanged.
- Open **Log** after a busy session. That's the receipts.

---

## 9. Exercises

1. **Prove the separation.** Start a background task, then kill the browser
   entirely. Reopen. The answer is waiting.
2. **Kill the server mid-job.** Restart. The job shows `failed` with
   *"Sid restarted"* — not a phantom `running` row.
3. **Park a job overnight.** Start one needing approval, leave it. It waits
   an hour, then denies by default.
4. **Read the log after an hour of use.** Anything you don't recognise?
5. **Turn on `confirm_act`.** Notice how quickly it becomes annoying — that's
   why it's off by default, and why `read`/`act`/`danger` tiers exist at all.
6. **Try to make dry run lie.** Ask for something complicated with it on. If
   the model ever claims it did something, the prompt needs strengthening.

---

## 10. Glossary

| Term | Plain meaning |
|---|---|
| **Job queue** | Work recorded now, executed later, by something else |
| **Worker** | The thing that picks jobs up and runs them |
| **Append-only** | You may add records, never change or remove them |
| **Audit trail** | The record of what actually happened |
| **Dry run** | Do everything except the part that changes something |
| **Fail closed** | When unsure, refuse |
| **Choke point** | The single place every call must pass through |
| **Semaphore** | A cap on how many things run at once |

---

## 11. What Phase 8 changes

Sid can work unattended — but only ever when you ask. It never starts
anything itself.

Phase 8 is the browser agent: sites with no API (your attendance portal,
Swiggy, price comparison). Phase 9 is proactivity — triggers, schedules, and
*"your exam ends Friday, flights are cheap"* arriving without you asking.

The job queue you just built is what makes Phase 9 possible: a trigger fires,
a job runs, a notification arrives.

**Check you can answer:**

- Why is the audit log separate from memory?
- Why does `record()` swallow its own exceptions?
- Why can't a background job use the normal approval flow?
- Why did dry run need a prompt change and not just a code change?
- Why do dry run, policy and audit all live in `tools.run()`?
- What does `_running[job_id] = task` prevent?

# Phase 8 + 9 — It Browses the Real Web, and It Starts Things Itself

> **Phase 8:** the 95% of the internet that will never give you an API.
> **Phase 9:** Sid stops waiting to be asked.

Phase 9 is the bigger change by far. Everything up to now was *reactive* — you
ask, Sid answers. This is the phase where Sid does something at 7:30am while
you're asleep and tells you about it.

---

## 1. What you built

| File | What it is |
|---|---|
| `backend/tools/browser.py` | Real browser: open, read, click, type |
| `backend/triggers.py` | The scheduler — daily / interval / once |
| `backend/notify.py` | Windows toast + in-app notification |
| `backend/tools/schedule_tools.py` | Tools so Sid can schedule its own work |
| `backend/main.py` | `/api/triggers` (list, add, delete, toggle) |
| `frontend/` | **Schedules** tab, notification cards |

One new database: `data/triggers.db`. **41 tools** now — 16 `read`, 21 `act`,
4 `danger`.

---

## 2. Phase 8 — why text, not screenshots

The obvious way to build a browser agent is screenshots plus a vision model:
look at the page, click at (x, y). Every demo does it that way. It is also
slow, expensive, and a model that misreads a coordinate clicks the wrong
button and you find out afterwards.

This reads the **accessibility tree** instead — the structured list of what's
on the page, the same information a screen reader uses. Everything you can
interact with gets a short reference:

```
PAGE: Wikipedia
--- BEGIN UNTRUSTED PAGE CONTENT ---
...
--- END UNTRUSTED PAGE CONTENT ---

Things you can interact with:
  [e10] input: search
  [e12] button: Search
```

Sid says `click e12`, never `click at (640, 312)`. A wrong click stops being
*unlikely* and becomes *impossible* — `e12` either exists or it doesn't:

```
click e99 -> No element 'e99' on the current page. Call read_page to get
             fresh references - they change on every page.
```

Verified working: `example.com` → click "Learn more" → landed on
`iana.org/help/example-domains`. Wikipedia's search box found and typed into.

### The security shape of it

This is the most dangerous file in the project, because now **every page Sid
reads is written by someone else, and Sid can act on what it reads.** Prompt
injection stops being about email and becomes about any page on the internet.

Three defences, one of which actually matters:

1. Page text is fenced and labelled untrusted (same as `gmail.py`).
2. `click` and `fill` are `act`, and every navigation lands in the audit log.
3. **Sid's browser is not your browser.** Fresh context every time: no
   cookies, no saved passwords, no access to your logged-in Chrome.

Number 3 is the structural one. Sid *cannot* act as you on a site you're
logged into, because it isn't logged in. The first two are good hygiene; the
third is the one that would still hold if the model were actively deceived.

### The bug that only appeared in the real server

The async Playwright API worked perfectly in a test script. Inside the actual
server, every call failed with a bare `NotImplementedError` — and the model,
reading that, told the user:

> *"Example.com open nahi ho paya kyunki browser tool implemented nahi hai."*
> — "…because the browser tool isn't implemented."

Which sounds like the tool was never written. It was; it just couldn't run.

Starting a browser means starting a subprocess, and **uvicorn hardcodes
`asyncio.SelectorEventLoop` on Windows**, which cannot spawn subprocesses.
Nothing was wrong with the code. It was wrong about the loop it ran on.

The fix is the **sync** Playwright API on one dedicated thread — the sync API
uses the plain `subprocess` module, so the event loop's limits never come up:

```python
_EXECUTOR = ThreadPoolExecutor(max_workers=1)   # ONE thread, always the same
```

`max_workers=1` is not a performance choice. Playwright's sync objects belong
to the thread that created them, so every call must land on that same thread.

> **Test inside the thing you're shipping.** This passed in a script and
> failed in the server, and only the server mattered.

### What it costs, measured

```
no browser running          0 MB
wikipedia.org open        260 MB
a long article open       407 MB
after close_browser         0 MB
```

On an 8 GB laptop that is real money, so: starts on first use rather than at
boot, never downloads images/fonts/video (Sid reads text — it has no eyes for
a hero image), and **closes itself after 5 idle minutes**.

**A note on measuring, because it cost an hour.** The first reading was
"803 MB" and sent me off optimising. It was taken with `Get-Process chrome*`,
which matched *your own Chrome* — fifteen processes of it. Playwright's binary
is called `chrome-headless-shell`. The real figure was 260 MB, close to the
original estimate, and the panic was entirely manufactured.

> **Measure the thing you think you're measuring.**

---

## 3. Phase 9 — the inversion

A trigger does exactly one thing:

```python
job_id = jobs.start(trigger["prompt"])
```

That's it. No execution logic of its own. Background execution, approvals, the
audit trail, restart recovery — Phase 6 already built all of it and it works.
A trigger that re-implemented any of that would be a second code path that
quietly drifts out of step with the first.

> **When you add a scheduler, schedule the thing you already have.**

This is why `triggers.py` is short, and shortness here is the evidence that
Phase 6 was designed right.

### Why not cron syntax

`0 8 * * 1-5` is precise, standard, and unreadable. Three kinds cover what a
personal assistant actually needs:

| kind | spec | means |
|---|---|---|
| `daily` | `"07:30"` | every day at that time, local |
| `interval` | `"45"` | every 45 minutes |
| `once` | ISO timestamp | one future run, then disables itself |

Local time throughout, deliberately. "8am" means 8am *where you are*; an
assistant that fired at 8am UTC would be quietly useless.

### The full chain, verified

```
created 'once' trigger, fires at 04:04:28
[  6s] FIRED -> job 4a40108e3776
        enabled=0 next_run=None    <- a 'once' must disable itself
[  9s] job done: 04:05 AM
NOTIFICATION  title='Chain test'  body='04:05 AM'  kind=result
```

Trigger → job → notification → a card in the browser. All four links.

### The same garbage-collection bug, twice

Phase 6's notes recorded this line and why it matters:

```python
_running[job_id] = task
```

`asyncio` keeps only a **weak** reference to a task. One with no owner can be
collected mid-await and simply stop — no error, nothing to debug. Phase 9
introduced the identical bug in `_fire()`:

```python
asyncio.create_task(_notify_when_done(trigger, job_id))   # nobody holds this
```

Same fix:

```python
watcher = asyncio.create_task(_notify_when_done(trigger, job_id))
_watchers.add(watcher)
watcher.add_done_callback(_watchers.discard)
```

Knowing a bug and not re-introducing it turn out to be different skills.

### The hard part isn't scheduling

It's deciding **when to stay quiet.** A trigger that fires every 30 minutes
and notifies every time has taught you to ignore it within a day. So
notification is opt-in per trigger, and `schedule_task`'s docstring pushes the
model toward prompts that say *"only tell me if something changed"*.

`notify.py` sends on three channels: a Windows toast (works with Sid closed),
an in-app card over the Phase 6 event stream (reaches your phone too), and the
log (always).

**Not built: real Web Push.** It needs VAPID keys, a subscription store, and —
the actual blocker — a *stable origin*. Push subscriptions are tied to the
origin that created them, and the free ngrok URL changes every session, so
every subscription would die on restart. That's a domain-name problem, not a
code problem. Noting it honestly beats shipping something that silently stops
working.

---

## 4. Four bugs worth keeping

### `{{s1}}` escaped into an answer

A scheduled job finished with the answer **`{{s1}}`**. The model had replied
`{"answer": "{{s1}}"}` — a placeholder referring to a step it never asked us
to run — and the direct-answer branch streamed it verbatim.

A placeholder is a *promise about work*. If the work isn't in the object, the
"answer" is not an answer. Two guards now:

```python
if (parsed and parsed.get("answer")
        and not parsed.get("steps")                       # steps win
        and not re.search(r"\{\{\s*\w+\s*\}\}", ...)):    # no placeholders
```

### A tool interface I got wrong myself

`schedule_task` first took `kind="daily", spec="07:30"`. Clean. And the very
first call written against it — by me, with the source open — passed
`when="daily 07:30"`, because that's how the request is phrased.

> If the person who wrote the tool gets the shape wrong, a model reading only
> the docstring certainly will.

It takes one `when` argument now, in the words a person uses, and the file
does the translating:

```
every morning at 7:30am  -> daily, "07:30"
hourly                   -> interval, "60"
in 2 hours               -> once, an ISO timestamp
```

Then the parser over-reached: `daily 07:30` became **19:30**, because a
heuristic reading a bare "7" as evening fired on an explicitly written
`07:30`. Guess only where there is genuine ambiguity.

> **Put the awkwardness in the code, not in the interface** — but don't let
> the code out-guess the user.

### The whole listing piped into one argument

`cancel_schedule` received this as its `schedule_id`:

```
1 scheduled task(s):
  [83d2c19a37] on   Morning ping - daily 07:30, next 2026-08-30 07:30
```

A plan chaining `list_schedules -> cancel_schedule` substitutes the full text
of the listing, because *that is what `{{s1}}` means*. Piping output into
arguments is the whole point of the feature, so this will keep happening. It
now digs the id out when there's exactly one, and says so plainly when there
are several — guessing which schedule to delete is not a call that function
should make.

### The model claimed it couldn't do something it could

Asked to schedule a daily task, Sid said *"Sorry, I can't do recurring
schedules yet"* — while `schedule_task` sat in its tool menu with an accurate
description. Earlier it did the opposite: claimed *"schedule set kar diya
hai"* without calling any tool at all.

Isolating it mattered here. Called directly, the planner produced a **perfect**
plan. The tool list was fine, memory was innocuous. The model was simply
declining on its own priors about what assistants can do.

The fix was to tell it, in the system prompt:

```
Two things you CAN do that assistants usually cannot. Never say you are
unable to do these, and never claim to have done them without calling
the tool:
```

Which is the **exact lesson from Phase 7's dry-run bug** in a new costume:
enforcing or providing something in code isn't enough if the model narrates
otherwise. Anything that changes what Sid *can do* has to reach the model too.

---

## 5. The shutdown that never finished

Sid could not be restarted while a page was open. It sat on
*"Waiting for connections to close"* — measured still stuck at **155 seconds**.

`/api/events` is a stream that stays open as long as the page is, by design.
"Graceful shutdown" means "wait for open connections to close". That one
never does.

The first fix was a `SHUTTING_DOWN` flag set from the lifespan shutdown hook.
It didn't work, and the reason is worth more than the fix: **uvicorn waits for
connections to close *before* it runs lifespan shutdown.** The hook ran after
the hang it was meant to prevent.

> **Know when your cleanup hook actually runs.** A shutdown hook that runs
> after the problem is decoration.

The real fix bounds the wait from outside:

```
--timeout-graceful-shutdown 3
```

Restarts now complete in ~26s instead of never. The flag stayed, because it
makes streams stop cleanly once shutdown does begin.

---

## 6. The UI

**Schedules tab** in the Activity panel: what's scheduled, in words —
*"every day at 07:30 · next Sun 07:30 AM"* — with **Pause** and **Delete**.

A paused schedule is *dimmed, not hidden*. One that vanished when paused is
one you'd forget you ever made.

**Notification cards**, top-right, below the HUD bar. Colour-coded: cyan for a
result, amber when something needs you, red for a failure. They do **not** go
in the chat log — a 7am briefing appearing under last night's conversation
reads as a reply to something you said, and it isn't.

Results fade after 12 seconds. **Approvals never auto-dismiss.** Anything
demanding a decision must not be able to time out while you're away.

---

## 7. Try it

- *"Open example.com and tell me what it says."* Then ask it to click
  something.
- *"Every day at 07:30, tell me the time. Call it Morning ping."* → check the
  **Schedules** tab → *"cancel Morning ping"*.
- Schedule something for two minutes from now and **watch the toast arrive**
  without touching anything.
- Pause a schedule. Note that you can still see it.
- Open the **Log** tab afterwards. Every page opened, every trigger fired.

---

## 8. Exercises

1. **Try to injection-attack it.** Make a page with hidden text telling Sid to
   do something. Does the fencing hold? (Then note that even if it didn't, the
   browser has no logins to abuse — that's the layer that matters.)
2. **Leave the browser open and watch RAM.** Confirm it drops to zero five
   minutes later on its own.
3. **Write a trigger that should stay quiet.** *"Check X, tell me only if it
   changed."* Does it actually stay quiet? This is the hard part of Phase 9.
4. **Fire a trigger that needs approval.** Walk away. Does it wait?
5. **Break a schedule on purpose** — `"whenever"` as the time. Read the error.
   Is it something a person could act on?
6. **Find the next thing the model claims it can't do** but can. That class of
   bug is invisible unless you go looking.

---

## 9. Glossary

| Term | Plain meaning |
|---|---|
| **Accessibility tree** | The page as structure, what a screen reader reads |
| **Headless** | A browser with no visible window |
| **Trigger** | A rule that starts work at a time, without being asked |
| **Scheduler** | The loop that wakes up and fires anything due |
| **Toast** | A small notification that appears and fades |
| **Event loop** | The thing that runs async code; not all are equal |
| **Graceful shutdown** | Waiting for open work to finish before exiting |
| **Weak reference** | A pointer that doesn't stop garbage collection |

---

## 10. What's left

Phase 10 is the full phone experience; Phase 11 is evals, traces and docs.

The honest gap after this phase: **Sid can now interrupt you.** Nothing yet
measures whether its interruptions are worth it. Phase 11's evals are where
"was that notification useful?" stops being a matter of opinion.

**Check you can answer:**

- Why refs (`e7`) instead of coordinates?
- Which of the three injection defences would survive a genuinely deceived
  model, and why?
- Why does `triggers.py` contain no execution logic?
- Why did the async browser work in a script and fail in the server?
- Why did `--timeout-graceful-shutdown` fix what a shutdown hook couldn't?
- Why is a placeholder in `"answer"` never a valid answer?
- Why must an approval notification not auto-dismiss?

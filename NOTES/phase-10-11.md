# Phase 10 + 11 — The Phone Becomes Real, and Sid Gets Measured

> **Phase 10:** push notifications, sharing into Sid, an offline outbox.
> **Phase 11:** traces, an eval suite, and the bug it caught within minutes.

Phase 11 is the one that changes how you work. Everything before it was
*build a thing, try it a few times, move on*. This is where "does it still
work?" stops being a matter of opinion.

---

## 1. What you built

| File | What it is |
|---|---|
| `backend/push.py` | VAPID keys, subscriptions, Web Push |
| `backend/traces.py` | One record per turn: plan, steps, timings, tokens |
| `evals/cases.json` | Ten cases, every one a bug that really happened |
| `evals/run.py` | The runner and its scoring |
| `frontend/sw.js` | `push` + `notificationclick` handlers |
| `frontend/manifest.webmanifest` | `share_target`, `shortcuts` |
| `backend/main.py` | `/share`, `/api/push/*`, `/api/traces` |

Two new databases: `data/push.db`, `data/traces.db`.

---

## 2. Push — the only channel that reaches you with Sid closed

Phase 9 had two channels. A Windows toast reaches you at the laptop. An
in-app card reaches a page that is **currently open**. Neither reaches a
phone in your pocket.

Web Push is genuinely different: your phone holds an open connection to
Google's push service, and Sid hands that service a message to deliver. Your
phone doesn't need to be reachable, and Sid doesn't need to know where it is.

```
1. Sid makes a VAPID key pair, once.       (identifies Sid to the service)
2. Your phone subscribes -> endpoint + 2 encryption keys -> stored.
3. Sid encrypts the message and POSTs it to that endpoint.
4. Your phone wakes the service worker, which decrypts and shows it.
```

Step 3 is the part worth internalising: **the push service cannot read the
message.** It is a courier carrying a sealed envelope.

### The problem Phase 9 called a blocker

A subscription is bound to the **origin** that created it, and Sid's phone
origin is a free ngrok URL that changes every session. Phase 9 said this made
push pointless and skipped it.

That was half right. Push is still worth having, because a tunnel commonly
stays up for days, and within one session push reaches your phone **with the
app closed** — which is exactly what the other two channels cannot do.

So each subscription is stored **with its origin**, and one from a dead
origin is deleted rather than pushed into the void. Verified:

```
subscription from an old tunnel  -> {'sent': 0, 'dropped': 1}   deleted
subscription from this tunnel    -> {'sent': 0, 'dropped': 0}   kept
```

The distinction matters. A **404/410 from the push service** means the device
is genuinely gone — delete it. A **network failure** means the internet
hiccuped — keep it. Treating those the same either loses working devices or
retries dead ones forever.

For push that survives restarts you need a stable domain. Set
`AXON_PUBLIC_ORIGIN` and it works permanently. That is a domain-name problem,
not a code problem, and the code is ready for the day you have one.

### Three bugs between "built" and "actually works"

Push was written, tested against the code paths, and shipped. On a real
iPhone it failed three times in a row, and none of the three causes was
visible from the laptop.

**1. The toggle was somewhere nobody would look.** I put "Notifications" in
the *Open Sid on your phone* QR panel — reasoning that this is the panel you
use when setting the phone up. But on the phone, that panel is for pairing
*another* phone. There was no reason to open it, so the toggle was invisible
on the one device it exists for. It lives in the **Activity panel** now,
reachable from the header on every screen.

> **Put a setting where the person who needs it will be**, not where it was
> convenient to write it.

**2. iOS has no push in a Safari tab, at all.** `PushManager` simply does not
exist until the site is added to the Home Screen. The button did nothing and
explained nothing. So the page now diagnoses itself and POSTs the result to
`/api/push/diagnose`, where the laptop can read it:

```
isIOS                  True
installedToHomeScreen  True
hasPushManager         True
permission             granted
blocked                None
```

Guessing across a device boundary is enormously more expensive than asking.
When the failure is on hardware you can't inspect, **make the failure report
itself.**

**3. Then it still didn't work — twice.**

```
{'sent': 0, 'dropped': 0}
```

That is what `send()` returned: technically true, completely useless. It had
caught the exception and incremented a counter nobody reads. The first fix
was to make it report the actual error, and only then did the real causes
appear.

The first was a key format. `pywebpush` takes a private key as raw/DER or as
a `Vapid` object — **never as a PEM string**, which is what Sid had stored.
Passing PEM produced `ASN.1 parsing error: invalid length`, which points
nowhere near "wrong container format".

The second was stranger, and only Apple cares:

```
mailto:sid@localhost      403 BadJwtToken
mailto:noreply@sid.local  403 BadJwtToken
mailto:sid@example.com    delivered
https://example.com       delivered
```

The contact address inside the signed token has to have a **real domain**.
Apple validates it; Google never did. `BadJwtToken` says nothing about which
claim was bad — the only way through was to probe one variable at a time
against the real device.

> **A delivery system that can't tell you why nothing arrived is barely
> better than one that doesn't work.** Every minute of this was spent getting
> from "it failed" to an error message worth reading.

---

## 3. Share target — Sid in the Android share sheet

You're in any app, you hit Share, you pick Sid, and the link lands in the
composer.

```
POST /share  ->  303 redirect  ->  /?shared=<token>
```

**Why a redirect?** A POST response cannot become the app's UI — the browser
would show a bare response body, not Sid. POST/redirect/GET is the standard
answer, and it has a second benefit: reloading the page doesn't re-share the
same thing.

The token is **one-use**. Verified:

```
first read:  {'text': 'Great article\nWorth a read\nhttps://example.com/x'}
second read: {'text': ''}
```

And the shared text goes into the **composer, not straight to the model**.
You shared a link because you want to ask something *about* it. Guessing what
would be wrong more often than right.

One small trap: the manifest asks for `application/x-www-form-urlencoded`,
because a text-only share needs no file handling and Starlette parses that
with no extra dependency. `python-multipart` is installed anyway, because
Android has been known to send multipart regardless — and a share that fails
silently on a phone is miserable to debug. Both paths are tested.

---

## 4. The offline outbox

Type a message with no signal and it is **queued, not lost**:

```
Offline — 1 message will send when you're back
```

It lives in `localStorage`, not memory, because "no signal" and "the OS
killed the tab" happen together often enough that memory alone would lose
exactly the messages this exists to protect.

`flushOutbox` clears the queue **before** sending, not after. If a send fails
part-way, losing one message beats sending it twice.

This got verified by accident, which is the best way. A message queued during
one test was still in the outbox on the next page load, went out on its own,
and created a real reminder:

```
Submit the assignment | once 2026-08-30T22:48 | Remind Malika to submit the assignment
```

Offline → queued → online → model → tool → scheduled. The whole chain, unplanned.

---

## 5. Traces — why, not just what

`audit.py` answers *"what did Sid do?"* — one row per tool call, append-only,
because it is evidence.

Traces answer a different question: *"why did it do that, and what did it
cost?"* One row per **turn**: the plan, every step with its own timing,
tokens, the model that ran it.

> **Audit is for accountability. Traces are for understanding.**

That is also why traces are allowed to be deleted (they roll off after seven
days) while the audit log never is. Diagnostics, not a record.

The per-step timing is the whole point. One real trace:

```
Tell me the time and how much disk space I have.
planned · 3.0s · 2 steps · 471 tok
  get_time      34ms  ████████████████████
  system_info   33ms  ███████████████████
```

The turn took 3.0 seconds. The tools took 67 **milliseconds** between them.
Almost all of it was the model. If you were optimising this, the tools are
the last place to look — and without per-step timings you'd never know.

### One design decision worth copying

The trace **watches the event stream** the browser already receives, rather
than having trace calls sprinkled through `llm.py`:

```python
async for event in llm.stream_reply(messages):
    trace.observe(event)
    yield f"data: {json.dumps(event)}\n\n"
```

So it cannot drift out of step with what actually ran. It is watching the
real thing, not a parallel description of it that someone has to remember to
update.

And `trace.save()` is in `finally` — a turn you cancelled halfway is exactly
the kind you want a trace of.

---

## 6. The eval suite, and what it caught in four minutes

Every phase so far ended with me typing a few questions and reading the
answers. That fails in two ways: you only re-test what you just built, and
you only remember the failures you happened to look for.

```bash
py evals/run.py
```

Ten cases. **Every one is a bug that actually happened.**

> An eval written from imagination tests what you already thought of. One
> written from your failures tests what actually breaks.

It asserts on things that are stable — which tools ran, how many steps,
substrings that must or must not appear. Not exact text: the same question
asked twice gets differently-worded answers, so `must equal "The time is
4:32 PM"` would fail the moment the clock moved. `must_not contain "not
implemented"` is a real assertion.

It runs against the **real running server**, not imported functions. An eval
that bypasses the server would have passed happily during the hour when the
browser tool was broken for every actual request.

### First run: 8/10

```
[4/10] it knows it can schedule
        FAIL  (1.8s, tools: none)
              - did not call schedule_task (called: nothing)
              - answer contains "can't"
```

Phase 9 "fixed" this. The suite proved the fix was **flaky, not real** —
which three manual tries would have called a success.

---

## 7. Chasing it down, in three wrong turns

**Turn 1 — is it non-deterministic?** Same prompt, three times:

```
1. ['schedule_task']
2. ['schedule_task']
3. NONE  ->  {"answer": "Sorry, I can't do that yet."}
```

Yes. So: lower the temperature. Planning is a structured task — given the
same request and the same tools, the plan should be the same plan. Gemini
defaults to about 1.0, which is right for conversation and wrong for this.

**Turn 2 — temperature 0.1 made it worse.** 1 out of 5 instead of 2 of 3.

But it made the failures *legible*:

```
"I need to call get_time first before I can schedule"
```

**Turn 3 — the real cause was my own instruction.** In the planner prompt:

```
- Anything time-related needs get_time first - you do not know today's date.
```

The model read "every day at 07:30" as time-related, concluded it must call
`get_time` first, couldn't see how, and gave up. My rule, written in Phase 5
for a good reason, was causing a Phase 9 bug.

Fixed by saying what I actually meant:

```
- If you need to know the CURRENT date or time, call get_time first. But a
  time the user GAVE you ("at 07:30", "Friday") is already known: use it
  directly, do NOT call get_time for it.
```

**Then it flipped to the opposite failure** — claiming success without
calling anything: *"Aapka schedule set kar diya hai."* Which is worse than
refusing, because it's a lie.

The structural fix is a fact about when the planner runs:

```
"answer" is ONLY for information you already know.
NOTHING HAS RUN YET when you write this — you are choosing a plan, not
carrying it out. So "answer" must never report a completed action.
```

After all three: **4 cases × 3 runs, every one identical.** Then 10/10.

### The lesson about temperature

Low temperature did not fix the bug. It made the bug **reproducible**, which
is what let me find the cause. A flaky failure hides its reason; a consistent
one shows it.

> **Making a bug deterministic is most of fixing it.**

---

## 8. Try it

- **On your phone:** open the QR panel → *Notifications on this phone* →
  **Turn on**. Schedule something two minutes out. Close Sid completely. Wait.
- Share a link into Sid from any app. It lands in the composer.
- Long-press the icon: Ask / Voice / Activity.
- Turn on airplane mode, type something, turn it off again.
- **Activity → Traces** after a busy session. Find the slowest turn. Was it
  the model or a tool?
- `py evals/run.py` — then break something on purpose and run it again.

---

## 9. Exercises

1. **Add an eval for a bug you hit today.** That is the habit; the suite is
   only the container.
2. **Run the evals against Ollama** (`--save`, then compare in
   `history.jsonl`). Which cases does the small local model fail? That is a
   real finding about which model you can trust, not a broken test.
3. **Find a slow turn in Traces.** Model time or tool time? What would you
   actually change?
4. **Delete `data/traces.db`.** Notice nothing breaks — that's what
   "diagnostics, not evidence" means. Try that with `audit.db`.
5. **Restart the tunnel with a phone subscribed.** Watch the subscription get
   dropped rather than pushed into the void.
6. **Set `max_steps: 1` on a case that needs two.** Confirm the suite fails —
   an eval suite you have never seen fail is not one you can trust.

---

## 10. Glossary

| Term | Plain meaning |
|---|---|
| **Web Push** | A message delivered to a phone with the app closed |
| **VAPID** | The key pair proving a push really came from Sid |
| **Origin** | scheme + host + port; push subscriptions are tied to one |
| **Share target** | Making your app appear in the OS share sheet |
| **POST/redirect/GET** | Answer a POST with a redirect so reload is safe |
| **Outbox** | Messages held locally until the network returns |
| **Trace** | The full record of one turn, for understanding it |
| **Eval** | An automated check that behaviour hasn't regressed |
| **Temperature** | How much randomness a model is allowed |

---

## 11. Where this leaves things

All eleven phases are done. Sid talks, uses 41 tools, reads your mail and
calendar, remembers, plans, works in the background, keeps receipts, browses
the real web, starts things on its own, reaches your phone — and now tells
you what it did and proves it still works.

The honest remaining gaps, in order of how much they'd matter:

1. **Push dies when the tunnel restarts.** A stable domain fixes it; nothing
   in the code needs to change.
2. **The eval suite is ten cases.** It caught a real bug immediately, which
   says more about how many bugs there are than how good the suite is. It
   should grow every time something breaks.
3. **Nothing measures whether Sid's interruptions are worth it.** The evals
   check that it *works*, not that a 7am notification was worth waking for.
   That needs judgement, and judgement needs data you don't have yet.
4. **One model, mostly.** The evals now make it cheap to find out what a
   different one changes. Worth running against Ollama once.

**Check you can answer:**

- Why can't a push subscription survive a tunnel restart?
- Why is a 410 from the push service treated differently from a timeout?
- Why does `/share` redirect instead of returning a page?
- Why does the outbox clear itself before sending, not after?
- Why are traces deletable when the audit log isn't?
- Why does the trace watch the event stream instead of being told what happened?
- Why did lowering the temperature make a bug *look* worse but be easier to fix?
- Why can't an eval assert on the exact text of an answer?

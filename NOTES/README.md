# Sid, explained

> Thirteen files, about 5,000 lines of plain English. Not comments on the
> code — the *reasoning*: what each piece is for, what was tried and thrown
> away, and which bugs were worth keeping a record of.

There is no index inside the files themselves, so this is it.

---

## Read in this order

Each phase assumes the one before it. Start at the top if you want the
whole story of how Sid was built.

| | What it covers | Read it for |
|---|---|---|
| **[Phase 1](phase-1.md)** | It talks | Streaming, SSE, why the browser is the UI |
| **[Phase 2](phase-2.md)** | It does things | Tools, tiers, the one choke point everything passes through |
| **[Phase 3](phase-3.md)** | It reaches your accounts | OAuth, Gmail, Calendar, where tokens live |
| **[Phase 4](phase-4.md)** | It remembers you | Embeddings, similarity, why a database is not enough |
| **[Phase 5](phase-5.md)** | It plans | A step graph instead of one-step-at-a-time improvising |
| **[Phase 6 + 7](phase-6-7.md)** | It works while you're away, and keeps receipts | Background tasks, the audit log, dry runs |
| **[Phase 8 + 9](phase-8-9.md)** | It browses, and starts things itself | A real browser; schedules and triggers |
| **[Phase 10 + 11](phase-10-11.md)** | The phone becomes real, and Sid gets measured | Push notifications, and the eval suite |
| **[Phase 12](phase-12.md)** | Eyes, hands, and a glow at the edges | Seeing the screen, moving the mouse, push-to-talk |

## Side topics

Written separately because they cut across phases.

| | |
|---|---|
| **[Computer control](computer-control.md)** | "It should do anything I can do on my PC" — and the safety bill that comes with it |
| **[Mobile](mobile.md)** | Running Sid from your phone: two modes, and the real tradeoff between them |
| **[The phone bridge](phone-bridge.md)** | Controlling an iPhone — why it works backwards, and building the Shortcut |

---

## If you only read four things

The bits that are worth knowing whether or not you ever touch this code:

- **[Phase 2 — tool tiers](phase-2.md)** · `read` / `act` / `danger`, and
  why one function every action passes through is worth more than careful
  code in fifty places.
- **[Phase 11 — why evals exist](phase-10-11.md)** · An eval written from
  imagination tests what you already thought of. One written from your
  failures tests what actually breaks.
- **[Phase 12 §6g — twelve passing tests and a dead microphone](phase-12.md)**
  · The suite passed while the thing was completely broken, because it
  tested the half that still worked.
- **[Phase 12 §6h — when a quota runs out, switch](phase-12.md)** · One
  provider means one refusal takes the whole capability with it.

---

## The ideas that keep coming back

Each of these was learned from something breaking, and each turned up
again later in a completely different part of the project:

> **Measure the thing you think you're measuring.** A memory reading of
> 803MB turned out to be the browser you had open, not Sid's.

> **If a tool cannot finish the job, its description must not imply that
> it did.** A dry run reporting success, and "sent" where "queued" was
> the truth — the same bug twice.

> **A fallback you have not tested is not a fallback.** It is a second way
> to fail, and you find out on the day the first one breaks.

> **Check the code loads before you check what it does.**

> **A green test suite says nothing about what it doesn't cover.**

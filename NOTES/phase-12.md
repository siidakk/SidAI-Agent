# Phase 12 — Eyes, Hands, and a Glow at the Edges

> Until now Sid could only touch things that offered it a door: an API, a
> URL, a command. This is the phase where it can use **anything on the
> screen**, the way you do — by looking at it and clicking.

It started from a video: a Mac agent playing a song in Spotify, filling in
a Booking.com form, then *pointing at a control in CapCut* and explaining
what to click. Almost everything in it came down to one capability Sid
didn't have.

---

## 1. What you built

| File | What it is |
|---|---|
| `overlay.py` | The screen-edge glow, and the global hotkey |
| `backend/screen.py` | Capturing the screen, and what that costs |
| `backend/tools/vision.py` | `see_screen`, `find_on_screen` |
| `backend/tools/pointer.py` | `click_at`, `move_mouse`, `scroll_at`, `drag_to`, `point_at` |
| `backend/windows.py` | (from the last fix) enumerating real windows |

**50 tools** now — 20 `read`, 26 `act`, 4 `danger`.

---

## 2. The one capability everything else hangs off

Before this, "play Back In Black on Spotify" was impossible — not because
launching Spotify was hard, but because Sid had **no idea what appeared
after it launched**. It could open the app and then stood there blind.

```
see_screen    →  "what is in front of me?"
find_on_screen→  "where exactly is the search box?"  → x=478, y=487
click_at      →  click those exact numbers
```

That chain turns *every* application into an automatable one — including
the ones with no API, no scripting support and no accessibility layer,
which is most of them.

### The model could already see

The pleasant surprise: `gemini-3.5-flash-lite`, already configured and on
the free tier, **reads screens perfectly well**. No model swap, no new key,
no extra cost. Asked to name the app in front, it answered "ChatGPT" and
read the URL out of the address bar.

Measured accuracy on four targets in a real window — a green *Commit
changes* button, an *Upgrade* button, a text field, and the window close X
— **all four landed on target**, close enough to click.

### Why coordinates come back 0–1000

Gemini reports positions on a normalised grid rather than in pixels, which
is the right call: the model never needs to know your resolution. There is
exactly **one** conversion back to real pixels, in `find_on_screen`. If a
click ever lands in the wrong place, there is one line to look at.

---

## 3. The glow (and the ring it replaced)

The first build put a small ring at the top of the screen **permanently**.
It worked, and it was wrong: an always-visible widget is a widget you
resent by the second day. The rewrite inverted it —

> **Invisible unless Sid is doing something.**

Press the hotkey and colour blooms inward from all four screen edges —
pink, violet, blue — then fades out and disappears when Sid is done.

### Why tkinter had to go

The ring faked softness with concentric outlines, because a tkinter window
on Windows has no real per-pixel alpha: only `-transparentcolor`, which
makes one exact colour vanish and leaves hard edges everywhere else.

**A glow is nothing but soft edges.** Faking that with hard-edged bands
looks like exactly what it is. So the rewrite drops tkinter and calls
`UpdateLayeredWindow` directly — the API behind every glassy Windows
overlay, and the only way to lay a genuinely soft gradient over a desktop.

### The light travels

A static bloom reads as a decoration; a moving one reads as *something is
happening*. Two comets orbit the screen border 180° apart, one lap every
4.5 seconds.

The trick that makes it work is **one coordinate system for four windows**.
Each strip knows its position along the whole screen *perimeter*, 0 to 1 —
not along its own edge:

```
top     left  → right     0        .. W
right   top   → bottom    W        .. W+H
bottom  right → left      W+H      .. 2W+H
left    bottom→ top       2W+H     .. 2W+2H
```

So a comet crossing from the top edge onto the right edge carries straight
on round the corner instead of restarting. Distance is measured *around the
loop*, so a comet sitting on the wrap point doesn't tear in half at the
top-left corner. The trail is asymmetric — longer behind the head than in
front — because a symmetric blob reads as a pulse rather than motion.

**The first attempt was technically animated and visibly static.** Base
glow 0.30, a tail covering 42% of the perimeter, two of them: they simply
added up to a uniform band. A travelling light needs somewhere dark to
travel through. Measured after the fix: the bright spot moves across
**59–75% of the screen width in 4.5 seconds**.

### Four windows, not one

A fullscreen layered window means pushing ~8 MB of RGBA every frame. Four
thin edge strips cover the same visible area with a third of the pixels,
and the middle of the screen — where nothing is drawn — costs nothing.

Measured: **~11 ms to generate all four edges**, against a 50 ms budget at
20fps.

### Premultiplied alpha

Windows wants BGRA with each colour channel already multiplied by its own
alpha. Skip it and semi-transparent pixels come out too bright with a milky
halo — the classic symptom, and worth recognising on sight.

---

## 3b. Four bugs in one overlay

### Every window message threw

```
ctypes.ArgumentError: argument 4: OverflowError: int too long to convert
```

ctypes assumes an undeclared function takes and returns a C `int` — 32
bits. Handles and `LPARAM` are 64. Four windows got created, none worked,
and the message loop threw on every event.

> **Declare `argtypes` and `restype` for every Win32 call.** It is not
> optional on 64-bit.

### The top edge hid behind the browser

`WS_EX_TOPMOST` at creation is honoured inconsistently — the bottom edge
drew over everything while the top one vanished behind a window.
`SetWindowPos(HWND_TOPMOST)` is the reliable way to say it.

### It lit up and stayed lit forever

The glow was switched on by a `wake` event and meant to be switched off by
an idle event **the server never sends**. So it bloomed and stayed — which
is precisely the always-on behaviour the rewrite existed to remove.

Fixed with a backstop: no news for 12 seconds and it fades itself out.

> **Never let something visible depend solely on an event arriving.** Give
> it its own way to switch off.

### Six copies, all drawing over each other

Each restart added an instance; only the first could hold the hotkey, so
every other one looked broken. A named mutex fixes it — released by the OS
on process death, so a crash can't leave a stale lock the way a lock-file
would.

The subtlety that cost a try: `GetLastError` is per-thread and is
overwritten by the *next* API call, so it must be read through a DLL opened
with `use_last_error=True` and checked immediately.

**And a bug in my own shell command, worth recording:** the PowerShell I
used to kill stray copies filtered on `CommandLine -like '*overlay.py*'` —
which matched *the PowerShell process running that very command*. It killed
itself, every time, and reported nothing but exit 255.

---

## 3c. The ring, as originally built

A 74px ring at the top of the screen, above every window, that changes as
Sid does: dim when idle, breathing when listening, a rotating arc while
thinking, amber when it wants you. It can also **leave its post and hover
over whatever Sid is about to click**.

Three Windows flags make it an overlay rather than an obstacle:

| Flag | Without it |
|---|---|
| `-transparentcolor` | A grey box sits at the top of your screen |
| `WS_EX_TRANSPARENT` | It **eats every click** inside its rectangle |
| `WS_EX_NOACTIVATE` | It steals focus mid-typing |

Verified properly rather than by looking: capture the overlay's rectangle
and count pixels of the chroma colour. **Zero** — the desktop shows
through, the ring sits on top.

### It is a separate process, for the same reason the listener is

It must survive the server restarting, and it must be running *before* the
server — otherwise the thing that starts Sid would need Sid already
started. It follows Sid's state over the same `/api/events` stream the web
page uses, so it needed no new channel.

---

## 4. The hotkey, and a bug that would have shipped silently

`Ctrl+Alt+Space`, chosen because nothing claims it. On the very first
machine it ran on:

```
could not register Ctrl+Alt+Space - something else owns it
```

Something already owned it. `RegisterHotKey` returned false, the feature
did nothing, and **nothing said so** — the ring appeared, looked healthy,
and the hotkey was simply dead.

There is no way to know in advance which chords a given PC has spoken for.
So it is a **list**, tried in order, and it says which one won:

```
hotkey Ctrl+Shift+Space registered
```

> **A constant that depends on the machine it runs on should not be a
> constant.** Try, verify, fall back, and report which one you got.

### Asking for one key, not the whole keyboard

`RegisterHotKey` asks Windows to deliver *one combination*. The obvious
alternative — a global keyboard hook — sees **every keystroke you type,
including passwords**, for a program that runs all day.

> **Ask for the narrowest capability that does the job.** Same principle as
> leaving `gmail.send` out of the OAuth scopes in Phase 3.

---

## 5. Why the mouse moves visibly

Windows can deliver a click without moving the cursor. This deliberately
glides it over ~14 frames instead.

An agent that clicks invisibly is one you **cannot supervise**. The
animation costs about a fifth of a second and buys the only window you get
to see a wrong click coming and pull the mouse away.

### And why clicking is `act`, not `danger`

A click is usually navigation, not destruction. Twenty approval prompts to
fill one form is a permission you learn to click through, which is worse
than no permission at all.

The real protection is structural: **Sid has to look before it can click.**
Coordinates come from `find_on_screen`, every call is in the audit log with
its position, and the cursor visibly travels there first.

---

## 6. Four bugs worth keeping

### It claimed to open Notepad, and hadn't

`open_app("notepad")` returned **"Opened notepad."** — while Windows 11 had
actually put up a *"Select an app to open 'notepad'"* dialog and opened
nothing. Two separate faults:

1. `Start-Process notepad` resolves the bare word as a document. Naming
   `notepad.exe` fixes it.
2. The tool **reported success without checking**. It now snapshots the
   open windows, waits, and says what actually appeared:
   `Opened notepad. New window: Untitled - Notepad`

> A tool that reports success it did not verify teaches the model to lie
> downstream. Every later step failed for reasons that made no sense.

**What caught it was the new vision.** `see_screen` looked and reported
*"No, Notepad is not open; there is a dialog titled 'Select an app'"*, and
`find_on_screen` refused to invent coordinates. The eyes caught the hands
lying.

### An import in the wrong place broke every click

```
Error in click_at: UnboundLocalError: cannot access local variable 'ctypes'
```

`import ctypes.wintypes` **inside** a function rebinds `ctypes` as a local
name for that whole function — including lines *above* the import. The
module-level `ctypes` became invisible and the first `ctypes.byref` threw.

> `import a.b` inside a function shadows `a` for the entire function body.
> Put it at module level.

### The model had to be told it had eyes

Exactly the lesson from Phases 7 and 9, for the third time. The tools were
registered and working; without a line in the system prompt saying *you can
see the screen and click*, the model would carry on saying it couldn't.

**Enforcing or providing a capability in code is not enough if the model
narrates otherwise.**

### The ring shows up in Sid's own screenshots

Harmless but real: it sits top-centre in every capture, occluding a small
strip. Noted rather than fixed — hiding it during capture would mean the
ring flickering every time Sid looks.

---

## 6b. Making it fast, measured rather than guessed

Reported as *"very laggy... click doesn't work at once, it tells me
coordinates then clicks"*. Before changing anything, 60 real turns said:

| | |
|---|---|
| Tools doing the work | **828 ms** |
| Model calls + overhead | **3,243 ms** |

**The work was never slow. The talking was.** Three fixes, in order of size.

### One call instead of two, for a click

A click was `find_on_screen` (a whole turn) then `click_at` (another turn) —
four model calls. Those coordinates were never for the user; they were an
intermediate value making a round trip to the cloud purely because the two
halves lived in different tools. `click_on` keeps the number inside the
machine. **6–12s → 6.3s.**

### Skip the second model call when there is nothing to add

`"Volume set to 45%"` is already a finished sentence. Spending two seconds
asking a model to rephrase it is pure waste. 25 tools are marked
`speaks_for_itself`; anything multi-step, failed, or returning raw data
still gets written up, because there the writing is the point.

### The fast path: some commands need no model at all

`"volume 30"` cost a 1.4s model call to decide it meant `set_volume(30)`.
That is a lookup wearing a language model's clothes.

```
what time is it      0.06s   (was 5.8s)
volume 35            0.20s
what's open          0.42s
disk space?          3.35s   ← not recognised, planner as before
```

It still goes through `tools.run()`. **A fast path that routed around the
checkpoint would punch a hole through every safety property here, and the
audit log would simply have gaps where the quick commands went.** Verified
all three: dry run still blocks it, it appears in the audit log, and no
`danger` tool is ever matched.

Patterns are anchored to the whole message and capped at 70 characters, so
*"open the report and tell me what the third paragraph says"* reaches the
planner. **Missing a shortcut costs 1.4 seconds; taking a wrong one costs
trust.**

### Two things measured and rejected

Both looked promising and both were worth nothing:

- **Smaller screenshots.** 1280px → 768px changed vision latency not at all
  (1.8–2.1s throughout), and the smallest started inventing coordinates.
- **Trimming the planner prompt.** 52 tools → 12 saved **0.07s** — 2% of a
  turn — once measured interleaved. A first, sloppier reading said 0.22s
  and was noise. Building a relevance filter would have traded correctness
  for 70ms.

> Measure before optimising, and **measure again properly before believing
> the first number.**

---

## 7. ⚠️ The line this phase crosses

Every other tool reads one specific thing you named. This one reads
**whatever is currently on your screen** and sends it to a model.

- On **Gemini or Claude**, your screen leaves the laptop. A password
  manager, a private chat, a bank page — if it is visible, it is in the
  image.
- On **Ollama**, nothing leaves, but small UI text reads far worse.

Sid never captures on its own — only a tool call captures, only when a
request needed it, and every capture is in the audit log. But the honest
framing is: **treat "look at my screen" like screen-sharing with a
stranger**, because mechanically that is what it is.

This is the first `read` tool in the project where "changes nothing" and
"harmless" genuinely come apart.

---

## 8. Try it

- Press **Ctrl+Shift+Space** anywhere. The ring brightens and Sid listens.
- *"What's on my screen?"*
- *"What does this error say?"* with something broken in front of you.
- *"Where's the settings button?"* — it should **point**, not click.
- *"Open Notepad and type my address in it."*
- Watch the cursor glide before a click. That pause is deliberate.

---

## 9. Exercises

1. **Ask it to click something wrong on purpose** and pull the mouse away
   mid-glide. That interruption window is the whole argument for animating.
2. **Point at something that isn't on screen.** It should refuse, not
   guess a coordinate.
3. **Compare `see_screen` and `list_windows`** for "what am I using". Both
   are valid; one costs a cloud round trip and the other doesn't.
4. **Check the audit log** after a clicking session. Every position is
   there.
5. **Take the hotkey away** — set `AXON_HOTKEY` to something already in use
   — and confirm the fallback still finds one.

---

## 10. Glossary

| Term | Plain meaning |
|---|---|
| **Overlay** | A window that draws on top but can't be clicked |
| **Click-through** | Mouse passes through as if the window weren't there |
| **Chroma key** | One colour declared invisible |
| **Global hotkey** | A key combination the OS delivers to you anywhere |
| **Normalised coordinates** | Positions as 0–1000 instead of pixels |
| **DPI aware** | Using real pixels on a scaled display |

---

## 11. What is still missing

Measured against the video that started this:

| In the video | Sid now |
|---|---|
| Voice in, spoken reply | ✅ |
| Sees the screen | ✅ |
| Clicks anything | ✅ |
| Points at a control to teach | ✅ |
| Wake by hotkey | ✅ |
| Plays a song in a native app | ⚠️ possible now, untested |
| Tiles windows side by side | ❌ |
| Continuous mid-sentence follow-ups | ❌ one request per wake |
| Drives your logged-in browser | ❌ **deliberately** |

That last one is the only remaining gap that is a *choice* rather than
missing work. Sid's browser has none of your logins, so a malicious page
cannot make it act as you. Pointing it at your real Chrome would close the
gap and remove the strongest defence in the project.

**Check you can answer:**

- Why do coordinates come back 0–1000 instead of pixels?
- Which three Windows flags make a window an overlay, and what breaks
  without each?
- Why is `click_at` `act` rather than `danger`?
- Why does the mouse move visibly instead of clicking instantly?
- Why is `RegisterHotKey` better than a keyboard hook for a program that
  runs all day?
- What is the one `read` tool where "changes nothing" isn't "harmless"?

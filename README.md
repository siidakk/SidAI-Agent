# Sid

**A self-hosted personal AI agent that runs entirely on your own laptop.**
It talks, uses 42 tools to control your PC and the real web, remembers you,
works in the background while you're away, and reaches your phone when it's
finished — for free, with nothing leaving your machine that doesn't have to.

📐 **[Read the architecture walkthrough →](https://claude.ai/code/artifact/283ec604-b3ba-49f5-9d16-83426a7795ce)**
An illustrated, plain-language tour of how it all fits together.

---

## What it actually does

```
"what's on my calendar tomorrow"
"play tum hi ho"
"open my attendance portal and read it"
"every morning at 7:30, check my mail — only tell me if something needs action"
"set volume to 30"          "lock my pc"          "stop the music"
```

Say **"Hey Sid"** across the room and it wakes up. Ask it to do something
slow, close the laptop, and check the answer on your phone later.

| | |
|---|---|
| **Runs on** | One Windows laptop, 8 GB RAM |
| **Costs** | Nothing — free API tier, or fully offline via Ollama |
| **Tools** | 42 · 16 read, 22 act, 4 requiring your approval |
| **Interface** | Vanilla HTML/CSS/JS — no framework, no build step |
| **Storage** | 6 SQLite files in one folder. Nothing in the cloud. |
| **Brains** | Gemini · Ollama (offline) · Claude — swap with a dropdown |

### Built in 11 phases

| | | | |
|---|---|---|---|
| 1 Chat + PWA | 2 Tools | 3 Gmail/Calendar | 4 Memory |
| 5 Planner | 6 Background jobs | 7 Audit + approvals | 8 Real browser |
| 9 Schedules | 10 Phone + push | 11 Traces + evals | |

Every phase has a write-up in [`NOTES/`](NOTES/) explaining the *concepts*,
the trade-offs, and the bugs — in plain language, not just commented code.

### Ground rules it keeps

- **Never spends money.** It can fill a cart and reach the pay button. You press it.
- **Dangerous actions always ask** — sending, deleting, running commands.
- **Every action is logged** to an append-only audit trail.
- **Its browser is not your browser.** No cookies, no logins — so a malicious
  page can't make it act as you.

---

## Run it

**Double-click `Sid` on your Desktop.** That's it.

The server starts itself in the background (no console window) and Sid opens
in its own app window. Click the power icon in the top right to stop it.

If the shortcut isn't there yet, see First-time setup below.

---

## If you cloned this

Nothing secret is in this repo, by design. `.env`, `credentials.json`, the
`data/` folder (your memories, tokens and logs) and the downloaded models are
all gitignored — so a fresh clone has **no keys and no data**, and you supply
your own in the setup below.

It is built for Windows: several tools call Win32 APIs directly for windows,
volume and media keys. Everything else is portable.

---

## First-time setup

**1. Install the libraries** (one time)

```bash
py -m pip install -r requirements.txt
```

**2. Choose a brain** (one time)

```powershell
Copy-Item .env.example .env
```

Then set `AXON_PROVIDER` in `.env`:

| Value | Cost | Notes |
|---|---|---|
| `ollama` | free | Runs on your laptop. Needs Ollama installed and `ollama pull llama3.2:3b`. Close Chrome — it needs ~2.5 GB free RAM. |
| `gemini` | free | Free key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey), no credit card. Put it in `GEMINI_API_KEY`. Run `py check.py --models` to see which models your key supports. |
| `claude` | paid | Key from [console.anthropic.com](https://console.anthropic.com) in `ANTHROPIC_API_KEY`. |

Adding a fourth provider is ~60 lines in `backend/providers/`. See
`NOTES/phase-1.md` §14b.

**3. Check what's working**

```bash
py check.py
```

Tests every provider — is it reachable, and does it actually generate. Run it
whenever something feels broken, before you go digging.

**4. Make the shortcuts**

```bash
py install.py
```

Adds Sid to your Desktop and Start Menu. Add `--startup` to launch it when
Windows starts, or run `py install.py --remove` to undo everything.

**5. Open it**

Double-click **Sid** on your Desktop.

- Phone → `http://<your-laptop-ip>:8321` (same Wi-Fi). Find the IP with
  `ipconfig`, look for "IPv4 Address", e.g. `http://192.168.1.7:8321`.
  Browser menu → **Add to Home Screen**.
- Still want a terminal? `py -m uvicorn backend.main:app --reload --port 8321`

---

## "Hey Jarvis"

```bash
py install.py --listener
```

Runs a background listener at every Windows startup. Say **"Hey Sid"** and
Sid opens with the mic already on — talk straight through:

> *"Hey Sid... play tum hi ho"*

First time only:

```bash
py setup_wake_word.py
```

Fully offline. Nothing is recorded, nothing leaves your machine. ~147 MB RAM.

**Change the phrase to anything** — no training, no account. Edit `.env`:

```
AXON_WAKE_WORD=hey sid
```

Then check it with `py setup_wake_word.py --test`.

**Turn it off when you're talking to someone.** There's a switch in the
header — the microphone icon with a line through it. When it's off the
listener stops running audio through the recogniser at all; it isn't
"hearing you and ignoring it". Takes about 4 seconds to take effect.

- Watch it live: `py listener.py --debug`
- Too many false wakes: raise `AXON_WAKE_THRESHOLD` to 0.6
- It ignores you: lower it to 0.4, or get closer to the mic

Two other engines are supported if you want a true purpose-built wake-word
net — see [`models/README.md`](models/README.md).

---

## Voice

Click the **microphone** and talk — it transcribes as you speak and sends
automatically when you stop. Click the **speaker** in the header to have
replies read aloud.

### Hindi and English

**Typing** works in either language, or mixed — the model replies in whatever
you used.

**The microphone** can only listen for one language at a time (an API limit,
not a missing feature), so use the **EN / हिं** dropdown:

- `EN` handles Hinglish well — it transcribes *"gaana bajao"* phonetically and
  the model understands it fine. Good default.
- `हिं` for full Hindi sentences or proper Devanagari.

### Caveats

- Speech recognition needs Chrome or Edge, and in Chrome the audio is sent to
  Google's servers (voice *output* is local). The wake word is fully local.
- Voice only works on your laptop. Microphones require `https://` or
  `localhost`, so it silently won't work on your phone over `http://`.
  That's a Phase 10 problem.

---

## What's here

```
Axon/
├── backend/
│   ├── config.py     every setting, in one place
│   ├── llm.py        THE AGENT LOOP — the heart of the project
│   ├── providers/    one file per AI backend, same interface
│   │   ├── ollama.py   free, runs on your laptop
│   │   ├── gemini.py   free cloud tier
│   │   └── claude.py   paid
│   ├── auth.py       the lock: access key for non-local devices
│   ├── approvals.py  human-in-the-loop for dangerous tools
│   ├── planner.py    writes a step graph, runs waves in parallel
│   ├── jobs.py       background task queue (SQLite)
│   ├── audit.py      append-only record of every action
│   ├── memory.py     what Sid knows about you (SQLite + embeddings)
│   ├── vault.py      encrypted secret storage (Windows DPAPI)
│   ├── google_auth.py OAuth flow + token refresh
│   ├── tools/        the things Sid can actually DO
│   │   ├── basic.py    time, maths, system info
│   │   ├── files.py    list/read files (+ the sandbox)
│   │   ├── gmail.py    search, read, draft
│   │   ├── gcal.py     calendar events
│   │   ├── media.py    volume and playback
│   │   ├── computer.py run commands, apps, keys, clipboard
│   │   ├── memory_tools.py  remember / recall / forget
│   │   └── web.py      search, YouTube, open a URL
│   └── main.py       the web server + the /api/chat endpoint
├── frontend/
│   ├── index.html    the page
│   ├── style.css     how it looks
│   ├── app.js        chat logic + reading the stream
│   ├── voice.js      speech in, speech out
│   ├── sw.js         service worker (makes it installable)
│   └── manifest.webmanifest
├── NOTES/
│   ├── phase-1.md    ← chat, streaming, PWA, providers
│   ├── phase-2.md    ← tools and the agent loop
│   ├── phase-3.md    ← OAuth, secret storage, prompt injection
│   ├── phase-4.md    ← memory, embeddings, retrieval
│   ├── phase-5.md    ← planning, DAGs, parallel execution
│   ├── phase-6-7.md  ← background jobs, audit log, dry run
│   ├── phase-8-9.md  ← real browser, schedules, notifications
│   ├── phase-10-11.md ← push, sharing, offline, traces, evals
│   ├── mobile.md     ← phones, the access key, secure contexts
│   └── computer-control.md  ← general tools + approvals
├── Axon.pyw         double-click launcher (no console window)
├── listener.py      "Hey Jarvis" wake word, always-on and fully local
├── connect.py       link/unlink Google
├── mobile.py        QR pairing + HTTPS tunnel for your phone
├── install.py       creates the Desktop / Start Menu shortcuts
├── check.py         tests every provider from the command line
└── evals/           the regression suite — py evals/run.py
```

**Read `NOTES/phase-1.md`.** The code is commented, but the notes explain
*why* it is shaped this way.

---

## What it can do

```bash
py check.py --tools
```

| Tool | Tier | Try saying |
|---|---|---|
| `get_time` | read | "what time is it?" |
| `calculate` | read | "what's 1250 * 12 + 375?" |
| `system_info` | read | "how much disk space do I have?" |
| `list_files` | read | "what's in the backend folder?" |
| `read_file` | read | "show me config.py" |
| `search_web` | read | "look up what a service worker is" |
| `play_on_youtube` | act | "play tum hi ho" — any song, any language |
| `open_url` | act | "open github.com" |
| `remember` / `recall` | act/read | "remember I'm vegetarian" |
| `list_memories` / `forget` | read/act | "what do you know about me?" |
| `search_email` | read | "any unread email this week?" |
| `read_email` | read | "read the one from HR" |
| `draft_reply` | act | "draft a reply saying I'll respond Monday" |
| `send_email` | **danger** | asks before sending |
| `list_events` | read | "what's on my calendar tomorrow?" |
| `set_volume` / `get_volume` | act | "turn it down to 30" |
| `control_media` | act | "pause the song", "next track" |
| `open_app` | act | "open notepad", "open my downloads folder" |
| `list_windows` / `focus_window` | read/act | "what's open?", "switch to chrome" |
| `type_text` / `press_keys` | act | "press ctrl+s", "type my email address" |
| `read_clipboard` / `write_clipboard` | read/act | "copy this to my clipboard" |
| `run_command` | **danger** | anything else — asks first |
| `lock_screen` | act | "lock my pc" — no approval, locking is harmless |
| `close_app` | act | "close notepad" — graceful, app can still prompt to save |
| `power_action` | **danger** | sleep / restart / shutdown — asks first |
| `create_event` | act | "add gym at 6pm Friday" |
| `delete_event` | **danger** | asks before deleting |
| `open_page` / `read_page` | act/read | "open my attendance portal and read it" |
| `click` / `fill` | act | "click the search button", "type Manipal in the box" |
| `close_browser` | act | "close the browser" — frees ~260 MB |
| `schedule_task` | act | "every morning at 7:30, check my calendar" |
| `list_schedules` / `cancel_schedule` | read/act | "what's automatic?", "cancel Morning ping" |

Adding a tool is one function with a docstring — see `NOTES/phase-2.md` §4.

---

## On your phone

Click the **phone icon** in Sid's header. It sets up a secure link and shows
a QR code — scan it with your phone camera. That's the whole setup.

The QR is **HTTPS by default**, which matters: browsers refuse microphone
access over plain http, so an http link means a mic button that silently
does nothing. First open takes ~15 seconds while the tunnel starts; after
that it's instant.

The panel also offers **Wi-Fi only**, which is faster and keeps all traffic
inside your house — but has no microphone.

The button only works on the computer Sid runs on: the QR contains your
access key, so the server refuses to show it anywhere else (403, even with
a valid key).

The tunnel puts Sid on the public internet. That's only acceptable because
of the access key — verified from the live public URL: **401 without a key,
401 with a wrong key**, 200 with the right one. It shuts down when Sid does.

Command-line equivalent, if you prefer:

```bash
py mobile.py --tunnel
```

Scan the QR with your phone camera. Works on **Android and iPhone**, on any
network, and the microphone works because it's HTTPS.

Wi-Fi only (more private, but no microphone):

```bash
py mobile.py
```

Then add it to your home screen. Android: Chrome menu -> Add to Home screen.
iPhone: Share -> Add to Home Screen (**Safari only** - Chrome on iOS cannot
install PWAs).

WARNING: the QR contains your password. Anyone who scans it can read your
email through Sid. If it leaks, delete `AXON_KEY` from `.env` and restart.

Details and the security reasoning: [`NOTES/mobile.md`](NOTES/mobile.md).

---

## Connect Google

Gmail and Calendar, via OAuth. Sid never sees your password.

```bash
py connect.py google
```

**First time** you need a `credentials.json` from Google Cloud Console — about
five minutes, steps in [`NOTES/phase-3.md`](NOTES/phase-3.md) §2. I can't do it
for you: Google won't let a program create OAuth clients on your behalf, which
is the whole point of OAuth.

Then try *"any unread email this week?"* or *"what's on my calendar tomorrow?"*

**What Sid can and cannot do with your account:**

| | |
|---|---|
| Read email, search it, read your calendar | ✅ |
| Save a draft reply, add a calendar event | ✅ (reversible) |
| **Send** an email | ❌ the `gmail.send` scope is never requested |
| **Delete** a calendar event | ❌ blocked as `danger` until Phase 7 |

Disconnect any time with `py connect.py google --off`. To fully revoke, visit
[myaccount.google.com/permissions](https://myaccount.google.com/permissions) —
deleting your copy of a key isn't the same as changing the lock.

---

## Background tasks

Hit the **clock button** next to send instead of Enter. You get a task id
back immediately and can close the window — the work carries on.

The **Activity panel** (list icon) shows every task, the answer when it's
done, and the append-only log of every action Sid has taken. The button
shows an amber dot when a task is parked waiting for your approval.

**Dry run** is in there too: `act` and `danger` tools describe what they
*would* do and change nothing, while `read` tools still run. Worth turning
on the first time you point Sid at something unfamiliar.

---

## Approvals

Dangerous tools stop and ask. You see the **exact command** and tap Run it or
No. Nothing irreversible happens without you.

```
Allow run_command?
  Remove-Item -Path "$HOME\Downloads\*" -Recurse -Force
  [ No ]  [ Run it ]
```

That's a real example — Sid proposed it when asked to "delete all the files
in my Downloads folder". The card is why it didn't happen.

- **No** is focused by default, so hammering Enter is safe
- Timing out counts as **No**
- A short blocklist (`format`, `vssadmin delete`, fork bombs) is refused even
  if you approve

Why it matters: Sid reads your Gmail and web results — text written by
strangers — and can now run commands. That combination is how a hidden
instruction in an email becomes a real command. See
[`NOTES/computer-control.md`](NOTES/computer-control.md).

---

## Speed

Measured on this laptop, same question, same agent loop:

| Brain | Per question | |
|---|---|---|
| **`gemini-3.5-flash-lite`** | **~2s** | default — free, generous quota |
| `gemini-3.6-flash` | ~8s | smarter, tight daily quota |
| `ollama` llama3.2:3b | ~55s | private and offline, but this laptop only has 8 GB |

Use the **dropdown in the header** to switch — no restart needed.

The local model is slow here because prompt processing runs at ~18 tokens/sec
when the machine has under 1 GB of RAM free. Closing Chrome helps a lot.
Sid warms the local model at startup so the first question isn't the
60-second one; the status dot is amber while that happens.

Full investigation: `NOTES/phase-2.md` §12e.

---

## Switching brains

Edit one line in `.env`:

Use the header dropdown, or set the default in `.env`:

```
AXON_PROVIDER=gemini     # free cloud tier, ~2s  (default)
AXON_PROVIDER=ollama     # free, your laptop, private, slow on 8GB
AXON_PROVIDER=claude     # paid
``` Nothing else changes — that's the whole point of
`backend/providers/`.

**If Ollama says it's out of memory:** close Chrome. A 3B model needs ~2 GB
free and an 8 GB laptop running Windows + Chrome doesn't have it. Setting
`OLLAMA_KEEP_ALIVE=30s` in `.env` also helps — the model unloads 30 seconds
after each reply instead of sitting in RAM for 5 minutes.

---

## Notifications on your phone

Sid can reach your phone **with the app closed** — a scheduled task finishes,
your phone buzzes.

1. Press the **phone icon**, scan the QR (this gives you an `https` address —
   push and the microphone both need one).
2. In that panel, **Notifications on this phone → Turn on**.
3. Allow notifications when the browser asks.

**The catch, stated plainly:** a push subscription is tied to the exact URL
that created it, and the free ngrok address changes every time the tunnel
restarts. So push works for as long as the tunnel stays up — often days — and
then needs turning on again.

To make it permanent you need a fixed address. Put it in `.env`:

```
AXON_PUBLIC_ORIGIN=https://sid.yourdomain.com
```

A paid ngrok domain or a Cloudflare tunnel both work. Nothing in the code
needs to change.

---

## Sharing into Sid

Once Sid is installed to your home screen, it appears in the Android share
sheet. Share a link from any app and it lands in Sid's composer, ready for
you to ask something about it.

Long-press the app icon for shortcuts: **Ask**, **Voice**, **Activity**.

**Offline?** Type anyway. The message is queued and sent the moment you have
signal again.

---

## Seeing what it did

**Activity → Traces.** One row per turn: which plan it chose, every step with
its own timing, tokens used, which model ran it.

The timings are the useful part:

```
Tell me the time and how much disk space I have.
planned · 3.0s · 2 steps · 471 tok
  get_time      34ms  ████████████████████
  system_info   33ms  ███████████████████
```

Three seconds total, 67 **milliseconds** of tools. Almost all of it was the
model — so if this felt slow, the tools are the last place to look.

Traces are kept for seven days, then deleted. They're diagnostics. The
**Log** tab is the audit trail, and that is never deleted.

---

## Checking it still works

```bash
py evals/run.py
```

Ten cases, each one a bug that actually happened. Run it after changing a
prompt, switching model, or upgrading anything.

```
[4/10] it knows it can schedule
        pass  (2.9s, tools: schedule_task)
...
10/10 passed
```

Useful flags:

```bash
py evals/run.py --case schedule    # just the ones matching a word
py evals/run.py --save             # append to evals/history.jsonl
```

Sid must already be running. The suite talks to the real server on purpose —
an eval that bypassed it would pass while the app you actually use is broken.

**Add a case whenever something breaks.** Edit `evals/cases.json`:

```json
{
  "name": "what you are checking",
  "ask": "exactly what a user would type",
  "tools": ["tool_that_must_be_called"],
  "forbid": ["tool_that_must_not_be"],
  "must_not": ["not implemented"],
  "max_steps": 3
}
```

Assert on behaviour — which tools ran, how many steps, phrases that must not
appear. Never on exact wording: the same question asked twice is answered
differently, and a test that fails for that reason is a test you'll start
ignoring.

---

## Roadmap

| Phase | Aim | Status |
|---|---|---|
| 1 | It talks, on laptop and phone | ✅ done |
| 2 | Tools — it *does* things instead of just answering | ✅ done |
| 3 | Real connectors: Gmail + Calendar via OAuth | ✅ done |
| 4 | Memory: profile, history, vector search | ✅ done |
| 5 | Planner: multi-step plans with dependencies | ✅ done |
| 6 | Background jobs: tasks that run while you're away | ✅ done |
| 7 | Trust: permissions, approvals, audit log | ✅ done |
| 8 | Browser agent for sites with no API | ✅ done |
| 9 | Proactive: triggers, schedules, notifications | ✅ done |
| 10 | Full phone experience: push, voice, share-target | ✅ done |
| 11 | Evals, traces, docs | ✅ done |

---

## Ground rules

- **It never spends money.** It can fill a cart and reach a pay button. You
  press it.
- **Irreversible actions always ask first.** Sending, deleting, publishing.
- **Every action gets logged.** From Phase 7 on, nothing happens invisibly.

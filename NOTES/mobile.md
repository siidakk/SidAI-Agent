# Running Sid on your phone

> Android and iPhone both. Two modes, because there's a real tradeoff.

---

## 1. The bug this uncovered

Before any of this, a check found something bad:

```
GET http://<laptop-ip>:8321/api/connections     (no password)
-> {"google":{"connected":true,"detail":"<your gmail address>"}}
```

**Anyone on the same Wi-Fi could open Sid and read your Gmail through it.**
Not theoretically — `/api/chat` would have searched your inbox for a stranger
on the hostel network.

The cause was innocent. Phase 1 bound the server to `0.0.0.0` so your phone
could reach it, which was correct. What was missing is that *reachable* and
*authorised* are different questions — and Phase 3 quietly raised the stakes
by connecting a real inbox.

> **Every time you add a capability, re-ask who can reach it.** The exposure
> didn't change in Phase 3. The consequences did.

---

## 2. The lock (`backend/auth.py`)

One shared secret, generated on first run and written to `.env`:

```
AXON_KEY=hLTP00ng...          32 chars from secrets.token_urlsafe(24)
```

- **From localhost:** no key needed. If someone can already run code on your
  laptop, a password stored on that same laptop protects nothing.
- **From anywhere else:** key required, as a cookie or `?key=...`.

Visit `http://<laptop>:8321/?key=SECRET` once and a one-year `httpOnly`
cookie is set, so you never type it again. That is what makes a single QR
scan enough.

### Four details that matter

**`secrets`, not `random`.** The `random` module is seeded predictably and
exists for simulations. `secrets` exists for exactly this. Picking the wrong
one is a classic invisible bug — the code looks identical and the output
looks random.

**`compare_digest`, not `==`.** String comparison returns as soon as two
characters differ, so a guess sharing a longer prefix takes measurably
longer. That timing difference lets an attacker rebuild a secret one
character at a time. `compare_digest` always takes the same time.

**`httpOnly`.** JavaScript cannot read the cookie, so a cross-site scripting
bug can't steal your key. Verified on the phone: `document.cookie` is empty
while requests still authenticate.

**Middleware registered before any route.** Nothing is exempt — not the
manifest, not the icons. The endpoint you forgot to protect is the one that
gets used.

### Verified

| Request | Result |
|---|---|
| LAN, no key | 401 |
| LAN, wrong key | 401 |
| LAN, correct key | 200 + cookie set |
| localhost, no key | 200 |
| **public internet (tunnel), no key** | **401** |

---

## 3. Two modes, and why

```
py mobile.py             Wi-Fi only    http://192.168.x.x:8321
py mobile.py --tunnel    anywhere      https://xxxx.ngrok-free.app
```

Both print a **QR code** you scan with your phone camera. The QR contains the
key, so scanning is the entire setup.

### Why the tunnel exists: secure contexts

Browsers refuse microphone access unless the page is a **secure context** —
`https://` or `localhost`. Your laptop at localhost qualifies. Your phone at
`http://172.17.100.254:8321` does not.

And it fails **silently**. No error, no permission prompt — the button just
does nothing, forever. That's the worst kind of bug: no feedback at all.

So `voice.js` checks `window.isSecureContext` and explains itself:

> *The microphone needs a secure connection (https). You're on http://, so the
> browser refuses — that's a browser rule, not an Sid setting. Fix: run
> `py mobile.py --tunnel` and scan the new QR code.*

> **When a platform silently refuses something, detect the condition yourself
> and say so.** The browser won't do it for you.

### Choosing between them

| | Wi-Fi mode | Tunnel mode |
|---|---|---|
| Works off your network | no | yes, even on mobile data |
| Microphone | **no** | yes |
| Install as an app | patchy | yes |
| Traffic path | stays in your house | through ngrok's servers |
| Your key travels | **in clear text** | encrypted |

Wi-Fi mode is the private one. Tunnel mode is the capable one.

Look at that last row. Over plain http your key crosses the network readable,
so anyone sniffing the Wi-Fi could copy it. **Once a password is involved,
encryption stops being optional** — that's a stronger argument for the tunnel
than the microphone is.

---

## 4. Android vs iPhone

| | Android (Chrome) | iPhone (Safari) |
|---|---|---|
| Install to home screen | menu -> Add to Home screen | Share -> Add to Home Screen |
| Must use | Chrome or Edge | **Safari only** |
| Voice input | yes, on https | newer iOS only |
| Spoken replies | yes | yes, after one tap |
| Push notifications (Phase 9) | yes | iOS 16.4+, installed only |

**Two iPhone gotchas, both handled in `voice.js`:**

1. **Chrome on iOS cannot install PWAs.** Every iOS browser is Safari
   underneath, but only real Safari exposes "Add to Home Screen". If the
   install option seems missing on an iPhone, this is almost always why.

2. **iOS won't speak until a real tap has happened.** Apple requires speech
   synthesis to be unlocked by a genuine user gesture. Unhandled, "read
   replies aloud" silently never works on iPhone. We speak an empty utterance
   on the first tap to unlock it.

I could not test on a real iPhone — verify it and say if the mic or the
spoken replies misbehave.

---

## 5. Setup

```bash
py mobile.py --tunnel
```

Scan the QR with your phone camera, tap the link, add it to your home screen.

**ngrok free shows an interstitial** ("You are about to visit...") once per
browser session. Tap **Visit Site**. Annoying, not harmful.

**The URL changes every restart** on the free tier, so you re-scan each
session. A fixed domain needs a paid plan — or Tailscale, which gives stable
private addressing instead of a public URL.

⚠️ **The QR is a password.** Anyone who scans it reads your email through
Sid. Not into a group chat, not into a screenshot, not onto a slide.

If it ever leaks: delete the `AXON_KEY` line from `.env` and restart. A new
key is generated and every previously paired device is locked out. That is
your revocation mechanism.

---

## 6. Exercises

1. **See the hole for yourself.** Comment out the `app.middleware` line in
   `main.py`, restart, and open `http://<lan-ip>:8321/api/connections` from
   your phone. Your email address appears. Put it back.
2. **Watch the cookie work.** Open the `?key=` link, then open the bare URL
   with no key. Still works — the cookie carried it.
3. **Revoke access.** Change `AXON_KEY` in `.env`, restart, reload the phone.
   Locked out.
4. **Prove the secure-context rule.** Open the LAN URL on your *laptop* (not
   localhost) and tap the mic. You get the same explanation your phone gets —
   it's about the URL, not the device.
5. **Compare the two modes** on the same question. The tunnel is slower;
   every byte round-trips through ngrok's servers.


---

## 7. The wake word switch

An always-on mic that jumps in mid-conversation is genuinely annoying, so
there's a toggle in the header.

The interesting part is *where the state lives*. The listener is a
**separate process** from the server, so a variable in the server would be
invisible to it, and `.env` is read once at import so it wouldn't take
effect. The switch is therefore a small JSON file (`data/state.json`) that
both processes read — the simplest thing two independent programs can agree
on without inventing a protocol between them.

`backend/settings.py` keeps it deliberately separate from `.env`:

> `.env` is **configuration** you set up once. This is **state** you change
> while using it. Mixing them means your setup file keeps mutating under you.

Two details worth copying:

**Atomic write.** The file is written to a temp path and swapped in. Without
it, the listener can read mid-write and get truncated JSON — rare, and it
would look like a random unexplained failure.

**Off means off.** The listener stops running audio through the recogniser
entirely, rather than analysing it and discarding the result. It still
*drains* the audio queue (otherwise the queue grows until the process runs
out of memory) but nothing is processed. An assistant that says it's off
should actually be off.

Verified live — the listener notices within about 4 seconds:

```
14:54:55  engine ready, listening
14:54:59  wake word turned OFF
14:55:05  wake word turned ON
```


---

## 8. The pairing button

`py mobile.py` works, but running a command to pair a phone is friction. The
phone icon in the header does the same thing: `GET /api/pairing` returns the
LAN URL plus a QR as a PNG data URI.

Three decisions in a small feature:

**Localhost only.** The response contains the access key in plain text — it
*is* the password. The auth middleware already gates every request, so
anyone reaching this endpoint has the key anyway, but requiring you to be at
the machine matches how pairing actually works: you scan a code on the
screen in front of you. Verified: **403 from the LAN even with a valid
key**, 200 from localhost.

**A PNG data URI, not inline SVG.** The QR drops into an `<img src>`, so
server output is never treated as markup. Inline SVG would work and would be
one more place where injected content could execute. When there's a choice
between "data the browser renders" and "markup the browser parses", pick
data.

**A white plate behind the QR.** A dark-themed QR on a dark background
scans badly on a lot of phone cameras. Contrast is not decoration here.

Escape and a backdrop click both close it — an overlay that traps you is a
bad overlay.


---

## 9. The phone button now sets up HTTPS itself

Showing a Wi-Fi QR was a half-measure: it paired the phone and then the
microphone silently didn't work, which is the failure this whole file exists
to complain about. So the button now starts the tunnel for you.

`backend/tunnel.py` owns the ngrok process. Four things it has to get right,
none of them about ngrok specifically — they apply to any child process a
server manages:

**Ask, don't remember.** `current_url()` reads ngrok's own API rather than
trusting a variable we set. The tunnel may have been started from a terminal,
or ours may have died. Same reasoning as the Google 401 retry: believe the
authority, not your own notes.

**Never start two.** `start()` returns the existing URL if one is up. First
call ~15s, second call **0.5s**.

**Never raise.** It's called from a UI button; an exception there is just a
blank panel. Failures come back as `{"error": ...}` with the actual fix in
the text (usually a missing authtoken).

**Clean up on exit.** The lifespan handler calls `tunnel.stop()`. A tunnel
outliving the server would leave a public URL pointing at a dead port and
hold an ngrok session slot.

### It degrades instead of failing

If the tunnel can't start, pairing still returns the **Wi-Fi QR**, plus the
reason. A working local link beats no link. The panel shows a badge either
way, because the difference is otherwise invisible:

```
HTTPS - voice works, any network      (green)
Wi-Fi only - no microphone            (amber)
```

And a button to switch, so neither choice is a trap.

### Verified on the live public URL

```
no key      -> 401
wrong key   -> 401
correct key -> 200
TLS         -> ssl_verify_result=0  (valid certificate)
/api/pairing through the tunnel, WITH a valid key -> 403
```

That last line is the one that matters. The tunnel makes Sid reachable from
anywhere; the access key is the only reason that's acceptable, and the
endpoint that hands out the key stays local-only regardless.

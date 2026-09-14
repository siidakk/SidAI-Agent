# The Phone Bridge — controlling an iPhone from Sid

> Sid can drive your PC directly: move the mouse, click, type. It cannot do
> that to an iPhone, and no amount of code will change that. This is the
> version that genuinely works.

---

## 1. Why it works backwards

On the PC, Sid **pushes**. On iOS there is nothing to push into:

- no ADB or anything like it
- no external tap injection
- no way to start an app from outside
- no remotely-triggered background execution

Apple blocks all of it on purpose. A laptop cannot reach into an iPhone
without a jailbreak, corporate MDM enrollment, or a paid relay app.

What Apple *does* allow is the phone reaching **out**. So the direction
flips:

```
you ask Sid       →  the command goes in a queue on the laptop
your iPhone asks  →  "anything for me?"  →  does it  →  reports back
```

Nothing is pushed. The phone volunteers. That needs no jailbreak, no MDM,
and no paid app — and it is the only shape iOS permits.

### The honest cost

**It is not instant unless you trigger it.** A queued command sits there
until the phone next asks. How often that happens is your choice (§4), and
a command nobody collects within **30 minutes is dropped** — "text mom I'm
running late", delivered six hours later, is worse than never sending it.

---

## 2. What Sid can ask the phone to do

| Ask Sid | Action | Arguments |
|---|---|---|
| "text Mom I'm late" | `message` | to, body |
| "play tum hi ho on my phone" | `play` | query |
| "open Spotify on my phone" | `open` | name |
| "set a 10 minute timer on my phone" | `timer` | minutes |
| "make my phone say the food is here" | `speak` | text |
| "note on my phone: buy milk" | `note` | text |
| "remind me on my phone to call the bank" | `reminder` | text |
| "what's my phone battery" | `battery` | — |
| "did that reach my phone?" | — | `phone_status` |

The list is short **on purpose**. Every action is one `If` block you build
by hand in the Shortcuts app, so a hundred of them would never get
finished. Adding one later is one `If` block plus one line in
`backend/phone.py`.

---

## 3. Build the Shortcut (once, ~10 minutes)

On the iPhone, open **Shortcuts** → **+** → name it **Ask Sid**.

### The two values you need

```
URL   http://<your-laptop-ip>:8321     (home Wi-Fi — stable)
      or the https ngrok address       (anywhere — changes each session)
KEY   <your access key>
```

Both are printed by:

```bash
py -c "import sys; sys.path.insert(0,'.'); from backend import config; import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); print('URL http://%s:%d' % (s.getsockname()[0], config.PORT)); print('KEY', config.ACCESS_KEY)"
```

**Your access key is a password** — it is the whole of Sid's security from
another device. Don't paste it anywhere public, this file included.

Use the **LAN address** if you mostly do this at home: it never changes.
Use the tunnel if you want it working away from the house — but it gets a
new address every time Sid restarts, and you will have to edit the
Shortcut when it does. That is a domain-name problem, not a code one.

### The steps

**1. Get the command**

- Add **Get Contents of URL**
  - URL: `http://<your-laptop-ip>:8321/api/phone/next?key=YOUR-KEY-HERE`
  - Method: **GET**

**2. Read which action it is**

- Add **Get Dictionary Value** → Key: `action` → from *Contents of URL*
- Add **If** → *Dictionary Value* → **is** → `none`
  - Inside: **Stop This Shortcut**
  - Leave the Otherwise empty
- Add **Get Dictionary Value** → Key: `id` → from *Contents of URL*
- **Set Variable** → name it `CommandID`

**3. One If block per action**

Add **If** → *Dictionary Value* (the `action` one) **is** `message`:

- **Get Dictionary Value** → Key `to` → from *Contents of URL* → **Set Variable** `To`
- **Get Dictionary Value** → Key `body` → from *Contents of URL*
- **Send Message** → Recipients: `To`, Message: the value above

Repeat the same shape for the others:

| If action is | Use this action | Read these keys |
|---|---|---|
| `play` | **Play Music** / Search | `query` |
| `open` | **Open App** | `name` |
| `timer` | **Start Timer** | `minutes` |
| `speak` | **Speak Text** | `text` |
| `note` | **Create Note** | `text` |
| `reminder` | **Add New Reminder** | `text` |
| `battery` | **Get Battery Level** | — |

Start with **`message` and `speak` only.** Get those two working end to
end, then add the rest — a half-built Shortcut that fails somewhere in the
middle is very hard to debug in that editor.

**4. Report back**

At the very end:

- **Get Contents of URL**
  - URL: `http://<your-laptop-ip>:8321/api/phone/done?key=YOUR-KEY-HERE`
  - Method: **POST**, Request Body: **JSON**
  - `id` → `CommandID`
  - `result` → `done` (or the battery level, for that one)

Without this, Sid never learns whether anything happened, and
`phone_status` will keep saying the command is still waiting.

---

## 4. How the Shortcut gets run

Pick whichever suits you — several can point at the same Shortcut:

| Trigger | Feels like | Set it up in |
|---|---|---|
| **Back Tap** (double-tap the back) | instant, one gesture | Settings → Accessibility → Touch → Back Tap |
| **Action Button** (15 Pro and later) | instant | Settings → Action Button |
| **"Hey Siri, Ask Sid"** | hands-free | works automatically |
| **Home Screen icon** | deliberate | Shortcuts → Share → Add to Home Screen |
| **Every 5 minutes** | closest to automatic | Shortcuts → Automation → Time of Day |

The time-based automation is the nearest thing to "it just happens". On
iOS 15 and later most automations can run without asking you to confirm —
check **Run Immediately** is on, or you will get a notification to tap
every time, which defeats the point.

---

## 5. Checking it works

Ask Sid **"what's my phone battery"**, then trigger the Shortcut. Then ask
**"did that reach my phone?"**

`phone_status` tells you the truth about three separate things:

- whether your phone has **ever** checked in (if not, the Shortcut is not
  set up or the URL is wrong)
- what is still **waiting**
- what recently came **back**

### Why the tools say "queued", not "sent"

Every phone tool's description says *queued*, and that word is
load-bearing. If they claimed "sent", the model would tell you it was sent
— and you would find out it wasn't when nobody replied.

Same failure as the Phase 7 dry run reporting success, same fix: **if a
tool cannot finish the job, its description must not imply that it did.**

---

## 6. Why the response is so flat

`/api/phone/next` returns this:

```json
{"id": "56880ac474", "action": "message",
 "to": "Mom", "body": "I'm running 20 minutes late"}
```

No nesting at all. Every level of structure is another **Get Dictionary
Value** block someone has to add by hand in a phone editor with no
keyboard shortcuts, so **the shape of this response is literally the
difficulty of the setup**.

It also returns `{"action": "none"}` rather than a 404 when the queue is
empty. An error status is much more awkward to branch on in Shortcuts than
a value is.

> **Design the API for whoever has to consume it.** Here that is a person
> dragging blocks around on a phone, and it changes every decision.

---

## 7. What this still cannot do

Worth being clear, so you don't go looking:

- **read your phone's screen** or tap arbitrary things
- **run while the phone is locked** and untouched, unless you set up the
  timed automation
- **anything Shortcuts itself can't do** — no reaching inside apps that
  offer no Shortcuts actions

For the full, unrestricted version — tap, swipe, type, screenshot,
anything — you would need an **Android** device and ADB over Wi-Fi. That
would be the same capability Sid has over the PC. On iOS, this is the
ceiling.

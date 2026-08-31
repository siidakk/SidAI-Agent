# Phase 1 — It Talks

> **Aim:** type on your laptop, get a streaming reply. Open the same URL on
> your phone, install it to the home screen, and it works identically.

Read this alongside the code. Nothing here is magic — by the end you should
be able to explain every file to someone else.

---

## 1. What you actually built

Here's the honest summary of each:

| File | What it really is |
|---|---|
| `backend/config.py` | A list of settings |
| `backend/llm.py` | Picks which brain to use, passes the reply through |
| `backend/providers/*.py` | One file per AI backend (ollama / gemini / claude) |
| `backend/main.py` | A program that listens on a port and answers requests |
| `frontend/index.html` | The shape of the page |
| `frontend/style.css` | The colours and layout |
| `frontend/app.js` | The logic: send message, read reply, draw it |
| `frontend/sw.js` | A background script that makes it installable |
| `frontend/manifest.webmanifest` | A note saying "I'm an app, here's my icon" |
| `.env` | Which provider to use, plus any keys. Never committed |
| `check.py` | Tests every provider from the command line |

That's it. There is no framework doing things behind your back.

---

## 2. The 30-second mental model

```
   YOU type "hello"
        │
        ▼
   app.js       ──── HTTP POST /api/chat ────►   main.py
   (browser)                                     (your laptop)
        ▲                                            │
        │                                            ▼
        │                                         llm.py
        │                                            │
        │                                            ▼
        └──── words streaming back ◄──────── the model
                                       (on your laptop via Ollama,
                                        or on Google's / Anthropic's
                                        servers — your choice, see §14b)
```

Three hops. Browser → your server → the model → all the way back.

**Why does your own server exist in the middle?** Why not have the browser
talk to the model directly? Two reasons:

1. **The API key.** Anything in the browser is visible to anyone who presses
   F12. A cloud key would be stolen within hours and someone else would spend
   your credit. Keys must live on a machine only you control. (With Ollama
   there's no key — but the other reason still applies.)
2. **Everything from Phase 2 onwards.** Tools, memory, planning, background
   jobs — none of that can live in a browser tab that closes when you lock
   your phone.

This middle layer is the actual product. The browser is just a window into it.

---

## 3. Concept: a server is just a program that waits

Strip away the mystique. A web server is a program that:

1. Says to the operating system, "give me anything that arrives on port 8321"
2. Sits there doing nothing
3. When a request arrives, runs a function
4. Sends back whatever that function returned
5. Goes back to step 2, forever

A **port** is just a numbered door on your computer. Your laptop has 65,535 of
them. We picked 8321 because 8000, 8100 and 3000 are already taken on this
machine by other apps.

In FastAPI you connect a URL to a function with a decorator:

```python
@app.get("/api/health")     # "when someone GETs this URL..."
async def health():         # "...run this function"
    return {"ok": True}
```

FastAPI turns the returned dictionary into JSON automatically.

### GET vs POST

- **GET** = "give me something." No body. Goes in the URL. Safe to repeat.
- **POST** = "here is some data, do something with it." Has a body.

We use GET for `/api/health` and POST for `/api/chat`, because chat sends the
whole conversation up — far too much for a URL.

---

## 4. Concept: the API has no memory. None.

This is the single most misunderstood thing about LLMs, so read it twice.

**The model does not remember your previous message.** There is no session on
anyone's server — this is equally true of Ollama on your own laptop. Every
request is a total stranger arriving with amnesia.

So how do chatbots seem to remember? Because the client **re-sends the entire
conversation every single time.**

Turn 1, you send:
```json
[{"role": "user", "content": "My name is Malika"}]
```

Turn 2, you send *all of this*:
```json
[{"role": "user",      "content": "My name is Malika"},
 {"role": "assistant", "content": "Nice to meet you, Malika."},
 {"role": "user",      "content": "What's my name?"}]
```

The model reads the whole thing fresh and answers from what it sees on the page.
That's not memory — that's re-reading the transcript each time.

**Three consequences you must internalise:**

1. **Cost grows quadratically.** Turn 20 sends 20 messages' worth of tokens.
   A long conversation gets expensive fast, because you keep paying to
   re-send the history.
2. **There is a hard limit.** Every model has a "context window" — the most
   tokens one request can hold. It's ~128k for llama3.2:3b and 1M for the big
   cloud models. Past it, the request is rejected. Long-lived assistants must
   eventually summarise or drop old turns.
3. **Real memory is something *we* build.** In Phase 4 we'll store facts in a
   database and inject only the *relevant* ones into each request. That's
   what makes an assistant feel like it knows you without re-sending your
   entire life history every message.

In this phase, `messages` in `app.js` is the entire memory of the system, and
it lives in your browser's localStorage. Clear your browser data and Sid
forgets you completely.

---

## 5. Concept: tokens (and cost, if any)

A **token** is roughly ¾ of a word. "Hello there friend" ≈ 3 tokens.

Everything is measured in two numbers:
- **input tokens** — everything you send (system prompt + full history)
- **output tokens** — everything the model writes back

**On Ollama this costs nothing** — it's your own CPU. Tokens still matter
though, because they're what makes it *slow*. Measured on this laptop with
llama3.2:3b:

| | |
|---|---|
| Loading the model (first message, cold) | **21–60 seconds** |
| Loading it again (within `OLLAMA_KEEP_ALIVE`) | 0 seconds |
| Generating | **~3.6 tokens/sec** |

Notice how misleading a single number would be. The first measurement we took
said "0.1 tokens/sec" — but almost all of that was reading 2 GB off disk, not
thinking. That's why `check.py` reports load and generate separately. **When
something looks 50x too slow, check whether you're measuring the thing you
think you're measuring.**

On a paid cloud model you're billed per token (Claude Opus 5 is $5/M input,
$25/M output — a short exchange ≈ ₹0.75). Gemini's free tier is ₹0 but caps
your requests per day instead.

`MAX_TOKENS = 4096` in `config.py` is a **ceiling, not a target.** The model
stops when it has finished; the ceiling just stops a runaway loop.

**See your own usage:** open DevTools (F12) → Console. Every reply logs
`tokens: {input_tokens: …, output_tokens: …}`. That line comes from the
`"done"` event we send at the end of the stream.

---

## 6. Concept: `async` — why the weird keywords

You'll see `async def`, `await`, `async for`, `async with` everywhere. Here's
the idea in one sentence:

> **`await` means "this is going to take a while — go do something else and
> come back when it's ready."**

Generating a reply takes 5–60 seconds, spent waiting either on the network
(cloud) or on your CPU (Ollama). Without async, your server would sit frozen for that whole time and
couldn't answer anyone else — your phone and laptop couldn't both use it.

The rules are simple:
- A function that waits on something is marked `async def`
- Inside it, you `await` the slow things
- `async for` loops over things that arrive over time (like a stream)
- `async with` cleans up properly even if something crashes

You don't need to understand the event loop underneath. Just: `await` =
"pause here, let others work."

---

## 7. Concept: streaming, and what SSE actually is

**The problem:** a reply takes 20 seconds. If you wait for all of it, the user
stares at a blank screen and assumes it's broken.

**The fix:** send words as they're produced. The total time is identical, but
it *feels* massively faster because something is happening.

To do this we need to keep the HTTP connection open and push pieces down it.
The format we use is **Server-Sent Events (SSE)** — a very old, very simple
standard. Each message looks exactly like this:

```
data: {"type":"text","text":"Hel"}⏎⏎
data: {"type":"text","text":"lo"}⏎⏎
data: {"type":"done","usage":{"input_tokens":52,"output_tokens":9}}⏎⏎
```

The literal characters `data: `, then your JSON, then **two** newlines. Those
two newlines are the delimiter that says "message complete." Send one and
nothing works, silently. This trips up everybody once.

### Why we invented our own event types

We could have streamed plain text. Instead we wrap each piece in JSON with a
`type` field:

- `text` — a piece of the reply
- `error` — something went wrong
- `done` — finished, here's what it cost

This costs a few extra bytes and buys a lot. In Phase 2 we simply add new
types — `tool_start`, `tool_result`, `thinking` — and the frontend can show
"🔧 searching your email…" without us redesigning the protocol. **Design the
envelope early; adding fields later is easy, changing the shape is not.**

### Reading a stream in the browser (`app.js`, step 4)

```js
const reader = res.body.getReader();
const decoder = new TextDecoder();
let buffer = "";

while (true) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });

  const parts = buffer.split("\n\n");
  buffer = parts.pop();          // ← the important line
  for (const part of parts) { /* handle a complete message */ }
}
```

**Why the buffer?** Data arrives in arbitrary chunks that have nothing to do
with your message boundaries. One `read()` might give you:

```
data: {"type":"text","text":"Hel
```

…and the rest arrives next time. So we accumulate into `buffer`, split on the
blank-line delimiter, and `pop()` the last piece back out because it might be
half a message. Getting this wrong produces the classic bug where the reply
works fine on localhost and mangles itself over real Wi-Fi.

> **Why not `EventSource`?** The browser has a built-in SSE client called
> `EventSource` that does all of the above for you. We can't use it: it only
> makes **GET** requests, and we need to POST the conversation. That single
> limitation is why nearly every real chat app hand-rolls this loop.

---

## 8. Concept: secrets, `.env`, and why `.gitignore` matters

Your API key is a password. If someone gets it, they spend your money.

**The rule: secrets never appear in code.**

- `.env` — has the real key. Listed in `.gitignore`, so git pretends it
  doesn't exist and it can never be pushed.
- `.env.example` — same keys, fake values. **Is** committed. It documents what
  settings the project needs without leaking anything.

`config.py` calls `load_dotenv()`, which reads `.env` and copies each line
into the process environment. After that, `os.getenv("ANTHROPIC_API_KEY")`
works.

> Bots continuously scan every public GitHub commit for API keys and start
> using them within minutes. This is not a hypothetical.

---

## 9. Concept: what makes this an app on your phone

You did not build an Android app. You built a website that Android and iOS
agree to *treat* as an app. That's a **PWA (Progressive Web App)**, and it
needs exactly three things:

**1. The viewport meta tag** (`index.html`)
```html
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
```
Without this, mobile browsers pretend the screen is 980px wide and shrink
everything to microscopic. This one line is the difference between "usable on
a phone" and "unusable on a phone."

**2. The manifest** (`manifest.webmanifest`)
A JSON file saying: my name is Sid, here's my icon, open me at `/`, and
`"display": "standalone"` — meaning **hide the browser's address bar**. That
one setting is why it stops looking like a website.

**3. The service worker** (`sw.js`)
A script the browser runs in the background, separate from your page. Every
network request the page makes passes through it first, and it can answer
from a cache instead of the internet.

Browsers refuse to offer "Install" unless a manifest *and* a service worker
with a `fetch` handler both exist. It's the price of admission.

Our worker uses **network-first**: try the internet, fall back to cache if
offline. The opposite (cache-first) is faster but makes development
maddening — you'd edit a file and the browser would keep serving the old one.

We explicitly *skip* `/api/` requests in the worker. A cached chat reply would
be worse than useless: you'd ask a new question and get yesterday's answer.

> **This file matters more later.** In Phase 9, `sw.js` is what receives
> **push notifications** — it's how Sid will buzz your phone when a
> background task finishes, even with the app closed.

### Why one `0.0.0.0` matters

In `config.py`:
```python
HOST = os.getenv("AXON_HOST", "0.0.0.0")
```

- `127.0.0.1` = "only accept connections from this machine"
- `0.0.0.0` = "accept connections on every network interface"

If it were `127.0.0.1`, your phone could not reach the server at all. That one
string is why Sid works on mobile.

---

## 10. Walking through one message, end to end

You type "hi" and press Enter. Here is every step:

1. **`app.js`** — the form's `submit` fires. `e.preventDefault()` stops the
   browser from reloading the page (the default behaviour of any form).
2. Pushes `{role:"user", content:"hi"}` onto the `messages` array and draws it.
3. Draws an empty assistant bubble with a blinking cursor (`.caret` in CSS).
4. `fetch("/api/chat", { method:"POST", body: JSON.stringify({messages}) })`
   — **the whole array**, not just "hi".
5. **`main.py`** — FastAPI matches `POST /api/chat`, validates the JSON against
   the `ChatRequest` model. Bad shape → automatic `422` before your code runs.
6. Converts Pydantic objects back to plain dicts, hands them to `llm.py`.
7. **`llm.py`** — builds the Anthropic client (once), opens
   `client.messages.stream(...)` with the model, system prompt, and messages.
8. As each piece of text arrives from Anthropic, `yield {"type":"text", ...}`.
9. **`main.py`** — wraps each yielded dict as `data: {...}\n\n` and pushes it
   down the open connection.
10. **`app.js`** — `reader.read()` wakes up, appends to `buffer`, splits on the
    blank line, parses the JSON, appends the text to the bubble, scrolls down.
11. Steps 8–10 repeat maybe 200 times, a few words each.
12. `llm.py` yields `{"type":"done", "usage":{...}}`. `app.js` logs the cost.
13. `app.js` pushes the finished reply into `messages`, saves to localStorage.
14. The connection closes. `finally` re-enables the send button.

Every one of those steps is code you can read.

---

## 11. Deliberate small decisions worth noticing

These are the details that separate "works on my machine" from "works":

| In the code | Why |
|---|---|
| `body.textContent = text` (never `innerHTML`) | `innerHTML` would **execute** a `<script>` inside a message. That's XSS. `textContent` treats everything as literal text. |
| `busy` flag + `send.disabled` | Stops you firing a second request while one is streaming, which would corrupt the message order. |
| `messages.pop()` in the `catch` | If a turn failed, remove the user message we optimistically added — otherwise a retry sends a broken half-turn. |
| Lazy `get_client()` in `llm.py` | Building the client at import time would crash the whole server with a cryptic error when the key is missing. This way we can print something helpful. |
| `app.mount("/")` declared **last** in `main.py` | FastAPI matches routes top to bottom. Mounting `/` catches everything — declared earlier, it would swallow `/api/chat`. |
| Explicit routes for `sw.js` and the manifest | Windows has no registered MIME type for `.webmanifest`, and browsers reject service workers not served as JavaScript. |
| `font-size: 16px` on `body` | Below 16px, iOS Safari zooms in every time you tap the input. Genuinely that arbitrary. |
| `height: 100dvh` | `dvh` shrinks correctly when the mobile keyboard opens. With `vh`, the input hides behind the keyboard. |
| `44px` send button | Apple's minimum tap-target size. Smaller and people miss it. |

---

## 12. When it breaks

| Symptom | Cause | Fix |
|---|---|---|
| Red dot, "No API key set" | `.env` missing or key not pasted | Copy `.env.example` → `.env`, paste the real key, **restart the server** |
| `ModuleNotFoundError: backend` | Started from the wrong folder | Run from the `Axon/` folder, not from inside `backend/` |
| Phone can't connect | Wrong IP, different Wi-Fi, or firewall | Check `ipconfig`; allow Python through Windows Firewall when prompted |
| "credit balance is too low" | No credit on the Anthropic account | Add credit in the Console |
| Reply appears all at once | A proxy is buffering | Our `X-Accel-Buffering: no` header handles most cases |
| Edits don't show up | Service worker served a cached file | DevTools → Application → Service Workers → Unregister, then hard-refresh |
| No "Install" option on phone | Manifest or worker failed to load | DevTools → Application → Manifest, look for errors |

---

## 13. Exercises — do these, they're the actual learning

1. **Break it on purpose.** Delete one `\n` from the SSE format in `main.py`.
   Watch the frontend go silent. Put it back. Now you'll never forget why
   there are two.
2. **Change the personality.** Edit `SYSTEM_PROMPT` in `config.py` — make it
   answer only in Hindi, or in exactly one sentence. Restart. See how much
   behaviour lives in that one string.
3. **Show the cost on screen.** The `done` event already carries `usage`.
   Display it under each reply instead of only in the console.
4. **Add a token counter** in the header showing the running total for the
   conversation. Watch it climb — that's the quadratic cost from §4, visible.
5. **Prove statelessness.** In `app.js`, change the fetch body to send only
   the last message: `JSON.stringify({ messages: [messages.at(-1)] })`. Chat
   for a few turns. It forgets everything instantly. Change it back.
6. **Add a Stop button.** `fetch` accepts an `AbortSignal`. Wire a button that
   aborts mid-stream. (This is real — you'll want it in Phase 6.)

---

## 14. Glossary

| Term | Plain meaning |
|---|---|
| **API** | A way for one program to ask another program to do something |
| **Endpoint** | One specific URL your server answers, e.g. `/api/chat` |
| **Port** | A numbered door on a computer; we use 8000 |
| **JSON** | The universal text format for sending structured data |
| **Token** | ~¾ of a word; the unit you're billed in |
| **Context window** | The maximum tokens one request can contain (1M for Opus 5) |
| **System prompt** | Standing instructions the model sees before every message |
| **Stateless** | Keeps no memory between requests |
| **Streaming / SSE** | Sending a response in pieces as it's produced |
| **Async** | "Go do something else while this waits" |
| **PWA** | A website that installs and behaves like a phone app |
| **Service worker** | Background script that intercepts network requests |
| **localStorage** | ~5MB of key-value storage in the browser, per device |
| **XSS** | Attack where injected text gets executed as code |
| **Pydantic** | Library that validates incoming data shapes |
| **Uvicorn** | The program that actually runs your FastAPI app |
| **Provider** | One AI backend (ollama / gemini / claude) behind a shared interface |
| **Interface** | An agreed set of functions, so parts can be swapped freely |
| **NDJSON** | One complete JSON object per line — Ollama's streaming format |
| **Ollama** | Program that downloads and runs AI models on your own computer |
| **Exponential backoff** | Retry after 1s, 2s, 4s... instead of hammering a busy server |
| **503** | "Server too busy" — temporary, worth retrying |
| **4xx vs 5xx** | 4xx = your request is wrong (don't retry). 5xx = their problem (do retry) |

---

## 14b. Providers — swapping the brain

You don't have to pay anyone. Sid can run on three different backends, and
switching between them is **one line in `.env`**:

```
AXON_PROVIDER=ollama     # or gemini, or claude
```

| Provider | Cost | Runs where | Privacy | Catch |
|---|---|---|---|---|
| **ollama** | Free forever | Your laptop | Perfect — nothing leaves the machine, works offline | Uses your RAM; a 3B model is noticeably less clever |
| **gemini** | Free tier, no card | Google's servers | ⚠️ Free tier data is used to improve Google's products | Daily request limits |
| **claude** | ~₹1 per exchange | Anthropic's servers | Paid data isn't used for training | Costs money |

### What "API" actually means

This confuses everyone at first: an **API is not a paid thing.** It just means
"how one program talks to another program."

Ollama gives you an API too — it runs a little web server at
`http://127.0.0.1:11434` on your own laptop and you POST JSON to it. Same
shape as talking to Google, except the round trip never leaves your machine
and costs nothing.

So the real question is never "API or no API." It is **whose computer runs
the model.** That's the only thing you're ever paying for.

### The lesson of the `providers/` folder

Open `providers/ollama.py` and `providers/gemini.py` side by side. They are
completely different:

- Ollama: `POST localhost:11434/api/chat`, streams **NDJSON** (one JSON
  object per line), system prompt goes in the messages array
- Gemini: `POST generativelanguage.googleapis.com/...`, streams **SSE**,
  system prompt is a separate field, and "assistant" is called `"model"`
- Claude: uses an official Python **SDK** instead of raw HTTP

Three different protocols. But every one of them exports the same two
functions:

```python
async def check()                 -> {"ready": bool, "detail": str}
async def stream_reply(messages)  -> yields {"type": "text"|"done", ...}
```

That agreement is called an **interface** (or a "contract"). Because it
holds, `main.py`, `app.js` and everything you build in later phases never
learn which brain is running.

This is also why `llm.py` shrank from 84 lines to about 15. It no longer
*does* anything — it just picks a provider and passes events through. When a
file gets smaller because you moved work behind a boundary, you did it right.

### How to choose, practically

- **Phase 1–2 (talking, first tools):** Ollama with `llama3.2:3b`. Free,
  private, plenty good enough. Close Chrome first — it needs ~2.5 GB free
  RAM, and on an 8 GB laptop that is not automatic.
- **Phase 5 (planning):** a 3B model will start producing broken plans.
  Switch to `gemini` — still free — and see whether the bug is your code or
  the model's brain. That diagnostic ability is the real reason this folder
  exists.
- **Phase 3+ with real email:** think carefully before pointing your actual
  inbox at a free tier whose terms say your data trains their products.
  Ollama's privacy story is genuinely perfect here.

### Two things that bit us, and will bit you again

**1. Cloud models get retired.**
`gemini-2.5-flash` is still listed by Google's own models endpoint, but using
it returns *"no longer available to new users."* Any tutorial more than a few
months old will name a model that no longer exists. So `check.py --models`
asks Google what your key can actually use, rather than trusting a name
someone wrote down in 2025.

Related: the `:streamGenerateContent` endpoint that every Gemini tutorial
uses is **gone**. Streaming is now plain `:generateContent` with `?alt=sse`.

**2. Cloud services fail randomly, and that's normal.**
Free-tier Gemini returns `503 high demand` at unpredictable moments. Measured
over 4 calls each:

| Model | Success | Avg |
|---|---|---|
| `gemini-3.6-flash` | 4/4 | 8.1s |
| `gemini-3.5-flash-lite` | 4/4 | 1.2s |
| `gemini-3.7-flash` | 3/4 | 9.6s |

The fix is not "pick a different model." It's **retry with exponential
backoff** — try again after 1s, then 2s, then 4s. That doubling matters:
hammering an overloaded server instantly makes the overload worse.

Look at `providers/gemini.py`. The retry lives in `stream_reply()`, and the
actual request lives in `_attempt()`. That split is deliberate: `_attempt`
raises `_Overloaded` **before** it yields any text, so a retry is always safe.
Once the user has seen half a reply, you can never secretly start over.

Also notice which errors we retry: `5xx` means "our fault, try again." `4xx`
means "your fault, don't bother" — a bad API key will not fix itself on the
second attempt. Retrying a 400 forever is a classic beginner bug.

### Exercise

Run the same question through two providers and compare. Ask something that
needs multi-step reasoning ("I have 3 exams on the 4th, 9th and 11th. Which
gap is best for a 2-day trip, and why?"). Watch a 3B model fumble it and a
frontier model nail it. That difference *is* Phase 5's whole problem.

---

## 15. What Phase 2 changes

Right now `llm.py` sends messages and gets text. In Phase 2 we add a `tools`
parameter, and the reply can come back saying *"don't print anything — run
`get_time()` and tell me the answer."*

We run the function, feed the result back, and ask again. Loop until the
model stops asking. That loop — about 20 lines — is the entire difference between a
chatbot and an agent.

Almost nothing you wrote today gets thrown away:

- `config.py` gains a tool registry
- `llm.py` gains the loop
- `main.py` is unchanged — it just forwards new event types
- `app.js` gains a branch to render "🔧 running `get_time`…"

That's what the event envelope in §7 bought us.

**Before moving on, make sure you can answer:**

- Why can't the browser call the model directly?
- Why do we re-send the entire conversation every time?
- What are the two newlines in SSE for?
- Why is `app.mount("/")` the last route in `main.py`?
- What would happen if `HOST` were `127.0.0.1`?

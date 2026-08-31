# Phase 2 — It Does Things

> **Aim:** stop answering, start acting. The model gets a menu of real Python
> functions, asks for the ones it needs, reads the results, and keeps going
> until the job is done.

This is the phase that turns a chatbot into an agent. Read §2 and §3 twice.

---

## 1. What you built

| File | What it is |
|---|---|
| `backend/tools/__init__.py` | The `@tool` decorator, the registry, and the runner |
| `backend/tools/basic.py` | `get_time`, `calculate`, `system_info` |
| `backend/tools/files.py` | `list_files`, `read_file` — **and the sandbox** |
| `backend/tools/web.py` | `search_web`, `play_on_youtube`, `open_url` |
| `backend/llm.py` | **The agent loop.** Rewritten. This is the phase. |
| `backend/providers/*.py` | Each learned its own tool dialect |
| `frontend/app.js` | Renders each tool as a visible step |

Eight tools. One loop.

---

## 2. The single most important idea in this project

> **The model cannot do anything. It can only ask.**

There is no code execution inside the model. It can't open a browser, read a
file, or check the time. All it can do is emit text — and now, structured
requests that *look* like this:

```json
{"name": "get_time", "input": {}}
```

**We** are the hands. Our Python code sees that request, runs the real
function, and hands back the result. The model never touches your computer;
it only ever asks us to.

Every scary headline about "AI agents doing things" comes down to this: a
human wrote a function, and gave the model permission to ask for it. The
capability was always yours. You're deciding what to lend.

That's also why Phase 7 (approvals) is possible at all. There's a checkpoint
between "the model asked" and "the thing happened" — and that checkpoint is
code you own.

---

## 3. The loop

```
   YOU: "what time is it and what's 99*47?"
        │
        ▼
   ┌─────────────────────────────────────────────┐
   │ 1. send conversation + tool menu to model   │◄──────┐
   └────────────────────┬────────────────────────┘       │
                        ▼                                │
             does it want tools?                         │
                        │                                │
         ┌── no ────────┴──────── yes ──┐                │
         ▼                              ▼                │
    ┌─────────┐            ┌────────────────────────┐    │
    │  DONE   │            │ 2. run the functions   │    │
    │  stop   │            │ 3. append request      │    │
    └─────────┘            │    AND results to      │    │
                           │    the conversation    │────┘
                           └────────────────────────┘
```

In code, `llm.py` — stripped of comments this is the entire thing:

```python
for step in range(MAX_STEPS):
    calls = []
    async for event in provider.stream_reply(working, tools=schemas):
        if event["type"] == "text":       yield event
        elif event["type"] == "tool_call": calls.append(event)

    if not calls:                          # it answered. we're done.
        yield done; return

    working.append({"role": "assistant", "tool_calls": calls})

    for call in calls:
        output = await tools.run(call["name"], call["input"])
        working.append({"role": "tool", "content": output})
```

**Roughly twenty lines.** That's the whole difference between ChatGPT and an
agent. Everything else in this project — providers, memory, planning,
approvals — is scaffolding around this loop.

### Why it loops instead of doing one round

Because results change what needs doing next. Ask *"look in the backend folder
and tell me how big config.py is"* and the model must call `list_files`, read
the answer, and only then know what to ask for. It cannot plan that in one
shot — it doesn't know what's in the folder until we tell it.

Right now the model improvises one step at a time. **That's what Phase 5
replaces**: writing the whole plan up front, so independent steps run in
parallel instead of one after another.

---

## 4. How the model knows what's on the menu

Every request carries a JSON description of each tool. Ours is generated from
the function itself — look at `calculate` in `tools/basic.py`:

```python
@tool(tier="read")
def calculate(expression: str) -> str:
    """Do exact arithmetic. Language models are unreliable at maths, so use
    this for any calculation rather than working it out yourself.

    Args:
        expression: A maths expression, e.g. "1250 * 12" or "(45+55)/4"
    """
```

The decorator reads three things and builds this:

```json
{
  "name": "calculate",
  "description": "Do exact arithmetic. Language models are unreliable...",
  "parameters": {
    "type": "object",
    "properties": {
      "expression": {"type": "string", "description": "A maths expression..."}
    },
    "required": ["expression"]
  }
}
```

- **name** ← the function name
- **description** ← everything in the docstring before `Args:`
- **properties** ← the type hints (`expression: str` → `"type": "string"`)
- **required** ← parameters with no default value

### The lesson: your docstring is a prompt

This is the part people miss. That docstring isn't documentation for humans —
**it's the instruction the model reads when deciding whether to use the tool.**

Notice `calculate`'s description doesn't just say "does maths." It says
*"language models are unreliable at maths, so use this rather than working it
out yourself."* That sentence is there to stop the model doing the sum in its
head and getting it wrong.

> **When a tool is being called wrongly, or not called when it should be, the
> fix is almost always a better docstring — not more code.**

That's a genuinely different debugging instinct from normal programming, and
it's worth internalising now.

---

## 5. One tool call, all the way through

You type *"what time is it?"*

1. **`app.js`** POSTs the conversation to `/api/chat`, as in Phase 1.
2. **`llm.py`** calls `tools.schemas()` — the menu — and passes it to the provider.
3. **`providers/gemini.py`** translates the menu into Gemini's dialect and sends it.
4. Gemini replies with a `functionCall` part instead of text.
5. The provider yields `{"type": "tool_call", "name": "get_time", "input": {}}`.
6. **`llm.py`** collects it, appends it to the working conversation, and yields
   the same event onward — so the browser can show ⚙ immediately.
7. **`tools.run("get_time", {})`** looks up the registry and calls the real Python function.
8. The result — `"Wednesday, 26 August 2026, 04:53 AM"` — is yielded as `tool_result`
   (browser shows ✓) and appended to the conversation as a `tool` message.
9. **Loop back to step 3.** Same conversation, now containing the answer.
10. Gemini has what it needs and writes text: *"It's 4:53 AM."*
11. No tool calls this round → `llm.py` yields `done` and returns.

Two round trips to the model for one question. That's normal and it's why
agents feel slower than chat.

---

## 6. Three providers, three dialects

Every provider invented its own words for the identical idea. This is the
tax the `providers/` folder exists to pay:

| | Ollama | Gemini | Claude |
|---|---|---|---|
| Tool list wrapper | `{"type":"function", "function":{...}}` | `{"functionDeclarations":[...]}` | flat list |
| Arguments schema key | `parameters` | `parameters` | `input_schema` |
| A call looks like | `message.tool_calls[]` | a `functionCall` part | a `tool_use` block |
| A result is sent as | role `"tool"` | a **user** turn with `functionResponse` | a **user** turn with `tool_result` |
| Call ids | none — we invent them | none — we invent them | real ids, must match |

Same three facts every time: what it's called, what it does, what arguments it
takes. Three spellings.

Because `llm.py` speaks only our own neutral format, **the agent loop has no
idea which provider is running.** Switch `AXON_PROVIDER` in `.env` and the loop
doesn't change by one character. That's the Phase 1 boundary paying rent.

### The bug this caused (worth knowing)

Gemini 3.x signs every tool call with a `thoughtSignature`. If you don't hand
that signature back verbatim on the next request, you get:

```
400: Function call is missing a thought_signature in functionCall parts.
```

We hit this on the very first test. The fix is a generic `"meta"` field on
tool calls — a slot for provider-specific baggage that the loop carries along
without understanding. When you build an abstraction over several vendors,
**always leave an opaque passthrough slot.** Something will need it.

---

## 7. ⚠️ Security: this is where it gets real

Phase 1 couldn't hurt you. Phase 2 can. Two files needed genuine care.

### `calculate` — why not just `eval()`?

The obvious implementation is one line:

```python
return eval(expression)          # NEVER DO THIS
```

But `eval` runs *any* Python. And the string came from a language model,
which got its ideas from the internet. `__import__('os').system('...')` is a
perfectly valid expression.

Instead we **parse** the text into a syntax tree and walk it ourselves,
allowing only numbers and arithmetic operators. Function calls, names,
imports and attribute access simply have no branch that matches, so they're
rejected. Verified:

```
calculate("__import__('os')")
  -> only + - * / // % ** and numbers are allowed
```

### `files.py` — the sandbox

`read_file` runs with *your* permissions. Without a check, anything that can
influence the model can read anything you can read — SSH keys, browser
cookies, your `.env`.

`_safe_path()` is the entire security model, and it has two rules:

**1. Resolve first, check second.** This path looks harmless as text:

```
../../Windows/System32/drivers/etc/hosts
```

Only after `.resolve()` — which collapses `..` and follows symlinks — can you
tell where it actually points. Checking the raw string is the classic
**path traversal** vulnerability.

**2. Deny by default.** Anything not provably inside `SANDBOX` is refused.
Not "block a list of bad paths" — allow one folder, refuse the universe.

Verified:

```
read_file("../../Windows/System32/drivers/etc/hosts")
  -> Refused: outside the allowed folder
read_file(".env")
  -> Refused: in a blocked location
```

The sandbox is deliberately narrow (the Sid project folder only). **Start
narrow and widen on purpose.** Widening is a decision; narrowing after a leak
is damage control.

### Tiers — groundwork for Phase 7

Every tool is tagged:

| Tier | Meaning | Now | Phase 7 |
|---|---|---|---|
| `read` | only looks | runs freely | runs freely |
| `act` | changes something reversible (opens a tab) | runs freely | maybe asks |
| `danger` | irreversible or costs money | **blocked** | asks you, waits |

Nothing is `danger` yet. The point is that tagging every tool *as you write
it* means Phase 7 doesn't require auditing fifty functions to work out which
ones are frightening.

---

## 8. Two design rules that keep the agent alive

### Tools must never raise

Look at `tools.run()` — it catches everything and returns the error **as
text**. That's deliberate:

```
Error: no tool called 'get_weather'.
Available tools: get_time, calculate, system_info, ...
```

An agent that crashes on a bad argument is useless. An agent that reads *"no
such folder"* and tries a different one is the entire point. **Errors are
information for the model, not exceptions for you.**

### `MAX_STEPS` — the runaway valve

```python
MAX_STEPS = 6
```

Small models get stuck. They call the same tool repeatedly, or ping-pong
between two, forever. Without a ceiling, one message could burn your whole
daily quota. Six rounds is generous for real work and short enough to stop a
loop from hurting.

Any agent that can call itself needs a limit like this. Put it in on day one.

---

## 9. Small model vs big model

Both were tested with the identical tool menu:

| | llama3.2:3b (local, free) | gemini-3.6-flash (free cloud) |
|---|---|---|
| "what time is it" | ✅ 35s | ✅ ~4s |
| "1250 * 12 + 375" | ✅ 17s | ✅ ~4s |
| Chained two tools | shaky | ✅ reliable |
| Answer quality | terse, sometimes just dumps the raw result | conversational |

So a 3B model **can** call tools. It's genuinely impressive that it works at
all on an 8 GB laptop. But you can already feel the ceiling: it handles one
tool fine and gets unreliable when results have to be combined.

That gap widens sharply in Phase 5. Keep both providers configured — comparing
them is the fastest way to tell "my code is broken" from "this model isn't
smart enough", which is otherwise a very hard thing to diagnose.

---

## 10. Why the UI shows every step

The chips above each reply (⚙ running → ✓ done, hover for the result) aren't
decoration. Three reasons:

1. **Latency.** A tool can take 10 seconds. Silence reads as "it's broken."
2. **Trust.** "It opened YouTube" is a claim. Seeing `✓ play_on_youtube(shape
   of you)` with the actual URL in the tooltip is evidence.
3. **Debugging.** When the answer is wrong, the steps tell you instantly
   whether the model picked the wrong tool, passed bad arguments, or got a
   good result and then misread it. Three completely different fixes.

This grows into the trace view in Phase 11. Build the habit now: **an agent
you can't watch is an agent you can't debug.**

---

## 11. Exercises

1. **Write a tool.** Add `flip_coin()` to `tools/basic.py`. Two lines plus a
   docstring. Restart, ask "flip a coin." Notice you edited *one file* and
   changed nothing else — that's what the registry buys you.
2. **Break the docstring.** Change `calculate`'s description to just `"Maths."`
   Ask a hard sum. Watch it try to answer from its head and get it wrong.
   Restore the docstring. **You just felt the docstring being a prompt.**
3. **Watch the loop count.** DevTools → Console. Every reply logs `steps:`.
   Find a question that takes 3 rounds instead of 2.
4. **Attack your own sandbox.** Ask Sid to read `C:/Windows/win.ini`, then
   your `.env`. Confirm both are refused. Then read `_safe_path` again and ask
   yourself whether you can think of a way past it.
5. **Compare brains.** Ask the same multi-tool question on `ollama` and on
   `gemini`. The difference is Phase 5's whole problem, visible today.
6. **Add a `danger` tool.** Write `delete_file` tagged `tier="danger"`. Ask
   Sid to use it. Watch it get refused. That's the Phase 7 hook already in
   place.

---

## 12. Glossary

| Term | Plain meaning |
|---|---|
| **Tool / function calling** | Letting a model request that your code run a function |
| **Tool schema** | The JSON that describes a tool to the model |
| **Agent loop** | ask → run tools → feed results back → repeat |
| **Registry** | The dictionary of every available tool, keyed by name |
| **Decorator** | `@tool(...)` — a function that wraps another to add behaviour |
| **JSON Schema** | Standard way to describe the shape of data |
| **Sandbox** | A folder a program is confined to |
| **Path traversal** | Escaping a sandbox with `..` — resolve before checking |
| **`eval` injection** | Running attacker text as code. Never eval untrusted input |
| **Tier** | Our risk tag: read / act / danger |
| **thoughtSignature** | Gemini's signature on a tool call; must be echoed back |

---

## 12b. Making it feel like an app

Four changes turned "a script you run" into "a thing you double-click".

### The launcher: `Axon.pyw`

Note the extension. On Windows, `.py` opens with `python.exe`, which **always**
creates a black console window. `.pyw` opens with `pythonw.exe`, which doesn't.
One letter is the difference between a script and an app.

The launcher does four things:

1. Is the server already answering? If yes, skip ahead.
2. If not, start uvicorn as a hidden background process.
3. **Poll until it actually responds** — don't just sleep a fixed number of
   seconds. A cold start took 18s here; a warm one is instant. Polling handles
   both without guessing, and if it never comes up you get a real error box
   instead of silence.
4. Open Sid in its own window.

Notice steps 1 and 2 use *two different checks*:

```python
port_is_open()      # is ANYTHING listening on 8321?
server_responds()   # is it OUR server, and is it ready?
```

They're not the same. A port can be held by a dying process or a completely
different app. "Something is listening" is not "Sid is ready" — and opening
the browser too early shows an error page.

Install the shortcuts with `py install.py`. A Windows `.lnk` is a binary file
you can't write with `open()`, so we ask Windows to make it, through the same
COM object Explorer uses.

### App windows instead of browser tabs

Chrome and Edge both accept `--app=<url>`: a window with no tabs, no address
bar, and its own taskbar icon. `open_app_window()` in `tools/web.py` uses it
for Sid itself *and* for every song and link the agent opens.

**This also fixed a real bug you hit.** `webbrowser.open()` on Windows often
just *focuses* an already-open browser rather than opening a new tab. Ask for
three songs in a row and you keep staring at the first one, convinced the tool
is broken. The tool was fine — the window never changed. `--app=` gives a
fresh window every time, so each request visibly happens.

### The quit button, and the first real security question

Sid now runs hidden, so without a way to stop it, Task Manager is your only
option. `/api/quit` fixes that — but look at the check:

```python
if request.client.host not in ("127.0.0.1", "::1"):
    raise HTTPException(403, ...)
```

We bind to `0.0.0.0` so your phone can reach Sid. **That also means everyone
else on the Wi-Fi can.** Without this check, a stranger in a café could POST
to `/api/quit` and kill your assistant.

That's the first taste of Phase 7's entire problem: the moment something is
reachable, *"who is asking?"* becomes a question you must answer. Verified
both ways — from the LAN IP it returns 403 and the server survives; from
localhost it stops cleanly.

---

## 12c. Voice — ears and a mouth in ~120 lines

`frontend/voice.js`. Two **completely separate** browser APIs that people
constantly confuse:

| API | Direction | Runs where |
|---|---|---|
| `SpeechRecognition` | your voice → text | **Google's servers** (in Chrome) |
| `speechSynthesis` | text → spoken audio | your machine |

Both are built into the browser. No library, no API key, no cost.

**Click the mic** → it listens, the button pulses red, words appear in the box
as you speak, and when you stop talking it sends automatically. **Click the
speaker** in the header → replies are read aloud.

### Three details that matter

**1. Speak after the stream finishes, not per chunk.** Feeding each fragment
to `speechSynthesis` as it arrives produces a stuttering robot. Wait for the
whole reply.

**2. Clean the text before speaking it.** Raw replies contain URLs. Hearing
*"h-t-t-p-s colon slash slash w-w-w dot youtube dot com slash watch..."* is
unbearable, so `speak()` strips URLs, markdown and parentheticals first.

**3. Never listen and talk at the same time.** `startListening()` calls
`speechSynthesis.cancel()` first — otherwise the mic hears Sid's own voice
and transcribes it back as your next message. An agent talking to itself in a
loop is funny exactly once.

### Two honest limitations

**Privacy:** in Chrome, speech recognition is *not* local. Your audio goes to
Google's servers. Running Ollama keeps your *text* on your laptop, but the
audio took a different road entirely. Fine for "play some music"; think twice
before dictating anything private.

**Voice only works on your laptop, not your phone.** Microphone access
requires a *secure context* — `https://` or `localhost`. Your laptop at
`localhost:8321` qualifies. Your phone at `http://192.168.x.x:8321` does not,
and the browser refuses silently. Fixing that needs real HTTPS, which is a
Phase 10 problem.

---

## 12d. The bug in the docstring example

Worth recording, because it's the exact failure mode §4 warned about.

The original docstring said:

```
query: The song or video to play, e.g. "shape of you ed sheeran"
```

A concrete example in a prompt is a strong pull. Small models in particular
will reach for it. The fix was to make the example *structural* rather than
specific:

```
query: The exact song, artist or video the user named. Use their own
       words. Add the artist only if the user mentioned one.
```

**When you put an example in a tool description, you're not documenting — you
are suggesting.** Prefer describing the *shape* of a good argument over
naming one specific value.

(There was also a genuine encoding bug: Hindi titles came out as
`à¤¤à¥à¤®`. The cause was `.encode().decode("unicode_escape")`, which mangles
every non-Latin script. YouTube's titles arrive with JSON escapes still in
them, so the correct decoder is `json.loads(f'"{raw}"')`.)

---

## 12e. "It is very slow" — a debugging story

Worth reading as a method, not just a fix. Every step here was a measurement,
and two of the measurements overturned a confident guess.

### Step 1: don't guess, split the number

"Slow" is not actionable. Ollama reports exactly where the time goes, so the
first move was to separate the phases:

```
WITHOUT tools:  prefill  1.1s (106 tok)   generate 3.3s
WITH 8 tools:   prefill 43.0s (774 tok)   generate 3.9s
```

Generation was never the problem. **Prefill** — the model *reading* the prompt
before writing anything — was 40x worse. And the only difference was 700
tokens of tool schema.

> **Lesson: "slow" is never a diagnosis.** Split the number until one part is
> obviously guilty.

### Step 2: the first guess was wrong

Obvious conclusion: the schemas are too big, shrink them. So we tested compact
schemas — and got a *worse* result:

```
full schemas,    run 1:  prefill 1.3s      run 2: 0.3s
compact schemas, run 1:  prefill 22.2s     run 2: 0.3s
```

Compact was slower than full? That makes no sense — until you notice **run 2
is fast in both cases.** The variable wasn't size at all. It was whether the
prompt had been seen before.

Ollama caches the processed prompt **prefix**. Process it once, reuse it
forever. Changing the schemas invalidated the cache, which is why the
"optimised" version looked slower — it was just cold.

> **Lesson: when an optimisation makes things worse, you're measuring the
> wrong variable.**

### Step 3: the actual fix — pay the bill when nobody's waiting

If the first request is expensive and the rest are cheap, don't make the *user*
pay the expensive one. `warmup()` in `providers/ollama.py` fires at server
startup and sends a throwaway prompt with `num_predict=1` — generate exactly
one token; we want the cache, not an answer.

```
cold first question:    68.0s
warmed first question:   7.0s
```

`OLLAMA_KEEP_ALIVE` also went from `5m` to `30m`. When the model unloads it
takes the cache with it, so a 5-minute coffee break used to cost a full
60-second rebuild.

The status dot goes amber while warming, because **a wait you understand is a
completely different experience from a wait you don't.**

### Step 4: the honest ceiling

Warming fixed the first question. But through the full agent loop:

```
question 1:  16s
question 2:  56s
question 3:  58s
```

The loop's second call (with the tool result appended) leaves the cache in a
shape the next question can't reuse. We could keep chasing this — but step
back and look at the real constraint:

```
RAM: 0.9 GB free of 8.3 GB (89% used)
```

Prefill was running at **~18 tokens/sec**. On a healthy machine it's several
hundred. The laptop is thrashing: 8 GB, minus Windows, minus Chrome, minus a
2 GB model and its cache. **The bottleneck isn't the code, it's the hardware.**

Measured on identical questions through the identical loop:

| Brain | Per question | Notes |
|---|---|---|
| `ollama` llama3.2:3b | **~55s** | private, offline, free |
| `gemini-3.6-flash` | ~8s | tight free-tier quota |
| **`gemini-3.5-flash-lite`** | **~1.8s** | free, generous quota |

Through the browser UI, a two-tool question now takes **2.6 seconds**.

> **Lesson: know when to stop optimising and change the constraint.** Another
> day tuning prompt caching would not have beaten "use a machine with enough
> RAM" — which, for free, is what the cloud model is.

### Step 5: make the tradeoff a choice, not a config file

Speed and privacy genuinely conflict here, and which one you want depends on
what you're about to ask. So there's now a **dropdown in the header**:

- `gemini` — ~2s, but it's a cloud service
- `ollama` — private, offline, ~55s on this laptop
- `claude` — greyed out until you add a key

`POST /api/provider` changes `config.PROVIDER` at runtime. It works because
that's just a module-level variable and `llm.py` re-reads it on every request —
nothing caches it. Switching to Ollama also kicks off a background warm-up.

**When a tradeoff is real, don't pick for the user — put it one click away.**

### A bug the rate limit exposed

Mid-testing, Gemini started returning results in 0.4 seconds. Suspiciously
fast — and it was: a `429 quota exceeded`, surfaced as an error event. My
timing script was counting failures as wins.

> **Lesson: a benchmark that doesn't check for success is measuring how fast
> your code can fail.**

---

## 12f. "Hey Jarvis" — how a wake word actually works

`listener.py` runs in the background and opens Sid when you say the phrase.

### It is NOT speech recognition

This is the part worth understanding. A speech recogniser is huge, slow, and
would have to stream every sound in your room somewhere. A wake word detector
is a **1.3 MB neural network** trained to answer exactly one question, twelve
times a second:

> "did the last ~1.4 seconds of audio contain this specific phrase?"

It outputs one number between 0 and 1. Above the threshold, we wake up. It
literally cannot understand anything else — and that is precisely what makes
it safe to leave running. **Nothing leaves your machine and nothing is
recorded.** Audio flows through a small rolling buffer and is discarded frame
by frame.

That two-stage design — a cheap always-on detector, an expensive recogniser
only *after* it fires — is how every Alexa and Siri on earth works. Now you
know why they can claim to not be listening while obviously listening.

Measured here: 12 checks/sec, ~207 MB RAM, peak score 0.10 on ambient room
noise against a 0.5 threshold (so no false wakes).

### Why "Hey Jarvis" and not "Hey Sid"

openWakeWord ships pretrained models for six phrases: `alexa`, `hey_mycroft`,
**`hey_jarvis`**, `hey_rhasspy`, `timer`, `weather`. "Hey Sid" isn't one of
them, and a custom phrase means training your own model — about an hour in
their Colab notebook, generating thousands of synthetic samples of the phrase.

So the default is Hey Jarvis: pretrained, works immediately, and honestly
fitting for this project. When you want your own, train it, drop the `.onnx`
in place and set `AXON_WAKE_WORD` in `.env`.

### The audio callback rule

```python
def callback(indata, frames, time, status):
    frames.put(bytes(indata))       # and NOTHING else
```

The audio callback runs on a separate high-priority thread. Do slow work in
it — run the model, launch a browser — and the callback arrives late, audio is
dropped, and you get crackling plus missed detections. So the callback only
drops raw frames into a queue; all real work happens on the main thread.

**Any real-time audio or video code has this rule.** The callback is sacred.

### Cooldown

One "Hey Jarvis" fires four times in a row without this, because the phrase
sits in the rolling buffer for over a second and scores high on every frame it
appears in. `COOLDOWN_SECONDS = 4.0` ignores repeats after a wake.

Tuning: too many false wakes, raise `AXON_WAKE_THRESHOLD` to 0.6–0.7. It
ignores you, drop it to 0.4 or get closer to the mic. Watch the live scores
with `py listener.py --debug`.

---

## 12g. Hindi and English

Two separate things, and conflating them causes confusion.

### The model is already bilingual

Nothing technical needed — just an instruction in `SYSTEM_PROMPT`:

> *Reply in whatever language the user writes in... If they mix Hindi and
> English (Hinglish), mix them back. Keep the same script they used.*

**Typing** in Hindi, English or Hinglish works regardless of any setting.

### The microphone is not

`SpeechRecognition` listens for **exactly one language**. There is no
"automatic" and no "both" — set it to `hi-IN` and it hears Hindi well and
English badly, and the reverse. That's an API limitation, not a missing
feature, so the honest fix is to let you choose: the **EN / हिं dropdown**
in the header.

Which to use:

- **`en-IN`** (default) is better than it sounds for Hinglish. It transcribes
  Hindi words phonetically in Roman script — *"gaana bajao"*, *"mera exam kab
  hai"* — and the model understands that perfectly.
- **`hi-IN`** for full Hindi sentences or when you want proper Devanagari.

### Voice output needed a fix too

Setting `utterance.lang = "hi-IN"` is not enough. The browser often reads
Hindi text with an *English* voice, which is unintelligible. So `speak()` now
searches the installed voices for one that actually matches the language, and
slows the rate slightly for Hindi (English pacing sounds slurred).

---

## 13. What Phase 3 changes

Right now the tools are self-contained — the time, some maths, a public web
page. Nothing needs permission.

Phase 3 connects **your actual accounts**: Gmail and Google Calendar via
OAuth. That brings three genuinely new problems:

- **OAuth** — proving you're you without ever handling your password
- **Token storage** — refresh tokens are as sensitive as passwords, encrypted at rest
- **Real consequences** — `send_email` is the first true `danger` tool

The loop you built today does not change at all. You'll add
`tools/google.py`, and the agent will start using it. That's the payoff for
getting the shape right.

**Before moving on, check you can answer:**

- Why can the model never actually run code itself?
- What three things does `@tool` read to build the schema?
- Why is a docstring a prompt and not a comment?
- Why must `tools.run()` never raise?
- Why does `_safe_path` resolve before checking?
- What does `MAX_STEPS` prevent?

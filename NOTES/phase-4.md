# Phase 4 — It Remembers You

> **Aim:** stop forgetting. Facts about you persist, conversation history
> lives on the server so every device sees it, and Sid can search everything
> it has ever been told — by meaning, not keywords.

---

## 1. What you built

| File | What it is |
|---|---|
| `backend/memory.py` | SQLite store, embeddings, similarity search, context builder |
| `backend/tools/memory_tools.py` | `remember`, `recall`, `list_memories`, `forget` |
| `backend/llm.py` | Injects relevant memories into every request |
| `backend/main.py` | Logs conversations, `/api/memory`, `/api/history/{id}` |
| providers × 3 | All learned a `system` parameter |

**33 tools now.** One new database file: `data/memory.db`.

---

## 2. Three kinds of memory, and why lumping them together fails

This is the part worth understanding. They have different lifetimes,
different sizes, and completely different retrieval rules.

| | What | How many | How it's retrieved |
|---|---|---|---|
| **Profile** | Durable facts about you | dozens | **always injected**, no search |
| **Episodic** | The conversation log | thousands | by conversation id |
| **Semantic** | Everything, searchable by meaning | thousands | top-k similarity |

Profile facts are *always* sent, and that's deliberate. Searching them would
be wrong: *"I'm vegetarian"* is relevant when booking a restaurant even
though nothing in "book me dinner" resembles it semantically. Some things you
simply have to always know.

---

## 3. The hard part is retrieval, not storage

Storing is a `INSERT`. The difficulty comes from Phase 1 §4: **the model is
stateless and re-reads the entire prompt every time.** So every fact you
inject costs tokens on *every* request, forever.

The job is therefore: *given this message, which handful of things are worth
spending context on?*

- Too few → Sid seems forgetful.
- Too many → slow, expensive, and distracted by irrelevant trivia.

Two dials control it, in `memory.py`:

```python
TOP_K = 5          # never inject more than this
MIN_SCORE = 0.55   # below this, a "match" is noise
```

`MIN_SCORE` matters more than it looks. A weak match is **worse than no
match** — it fills the context and actively misleads the model. Better to
recall nothing than to recall something almost-relevant.

---

## 4. Embeddings: turning meaning into geometry

An embedding turns text into a list of numbers — a point in 768-dimensional
space — positioned so that **things which mean similar things end up close
together**, even with no words in common.

Then "find related memories" becomes "find nearby points", which is just
arithmetic.

Verified with three queries that share **zero keywords** with what was stored:

| You asked | It found | Score |
|---|---|---|
| "when do I need to reply to the job people" | *internship offer from Amazon has a deadline of 15 September* | 0.60 |
| "when is my database test" | *DBMS exam is on the 9th of next month* | 0.67 |
| "what should I book on the plane" | *prefers window seats when flying* | 0.63 |

No keyword search could do that. "database" ≠ "DBMS", "plane" ≠ "flying",
"job people" ≠ "Amazon internship".

### One line that does a lot of work

```python
return vector / norm if norm else vector
```

Normalising every vector to length 1 means cosine similarity — the thing we
actually want — becomes a plain **dot product**. So comparing a query
against every stored memory is one matrix multiply:

```python
scores = matrix @ vector
```

Thousands of comparisons, well under a millisecond.

---

## 5. Why SQLite, not Postgres

The roadmap said Postgres + pgvector. This is SQLite, deliberately:

- **No server.** No Docker, no daemon eating RAM on an 8 GB laptop that's
  already short of it.
- **It's one file.** Copy it, delete it, open it in any SQLite viewer.
- **Brute force is genuinely faster here.** At a few thousand vectors, the
  numpy matrix multiply takes less time than a network round-trip to a local
  Postgres would.

Postgres earns its place with concurrent writers or millions of rows. A
personal assistant has exactly one user.

> **Reach for the smaller tool until the bigger one is actually justified.**
> "Real projects use Postgres" is not a reason. Having a problem Postgres
> solves is a reason.

An index (FAISS, pgvector) only starts winning in the hundreds of thousands
of rows, and costs build time, memory and a dependency.

---

## 6. Where memory goes in the prompt, and why

`llm.py` builds a block and appends it to the **system prompt**, not the
message list:

```
--- MEMORY ---
What you know about the user:
- Malika studies at Manipal and is vegetarian

Possibly relevant things you were told earlier:
- ... (relevance 0.67)
--- END MEMORY ---
```

**Why the system prompt?** Because it's *context*, not conversation.
Injecting "you know Malika is vegetarian" as a fake user turn would make the
model think you just said it — and it would sometimes reply to it.

**Why per-message and not once at startup?** Because which memories are
relevant depends entirely on what was just asked. Profile is constant; the
rest is retrieved fresh every time.

**The relevance score is shown on purpose.** When Sid brings up something
odd, the number tells you instantly whether retrieval misfired or the model
did. Two completely different bugs.

This also needed a real change: all three providers gained a `system`
parameter. They used to read `config.SYSTEM_PROMPT` directly, which made a
per-request prompt impossible. Passing it explicitly is both more flexible
and easier to follow than a global that changes underneath you.

---

## 7. Sid decides what to remember

You don't fill in a profile form. You mention something and it calls
`remember` on its own.

That works entirely through the docstring — which, per Phase 2 §4, *is* the
prompt. `remember`'s docstring has to convey a judgement call:

> *Do NOT save passing chatter... Ask yourself: would this still be worth
> knowing next month?*

That's a much harder thing to write than "this adds two numbers", and it
fails visibly in both directions. Too eager and memory fills with "user said
hello", each one taxing every future request. Too shy and it forgets what
you told it.

Verified end to end — and this is the moment Phase 4 justifies itself:

```
1. "My name is Malika, I study at Manipal and I'm vegetarian."
   -> called remember()

2. BRAND NEW conversation, no history sent at all:
   "What's my name and where do I study?"
   -> "Your name is Malika, and you study at Manipal."     (1.4s)
```

No history in that second request. It knew because of memory.

`forget` is `act`, not `danger`, on purpose. **Corrections have to be cheap
or people stop making them** — and a memory nobody corrects gets things
wrong and stays wrong.

---

## 8. Conversation history moved to the server

Until now `messages` lived in browser localStorage, so your laptop and your
phone had entirely separate histories.

Now every message is logged to SQLite with a `conversation` id.
localStorage becomes a *cache of what this device has seen*, not the only
copy in existence.

One detail worth copying: the reply is logged **after the stream completes**,
not in a `finally`. A `finally` would also record half-written replies from
requests you cancelled — worse than not recording them.

---

## 9. ⚠️ Two real bugs this phase exposed

### Web search had silently died

Testing "suggest somewhere to eat" produced **six `search_web` calls** and no
answer. `search_web` scraped DuckDuckGo's HTML, and DuckDuckGo now returns
HTTP 202 and a bot-detection page. Both `html.` and `lite.` endpoints.

So the tool had been answering *"No results found"* to every query — the
worst failure mode there is: **confidently wrong rather than obviously
broken.**

Scraping someone's search page was always borrowed time. It now asks Gemini
to search Google and answer with sources ("grounding") — real results, an
interface intended to be used, no extra key.

The catch, stated honestly in the code: **grounding has its own free-tier
quota**, much smaller than chat's. When it's exhausted the tool says so
plainly instead of pretending there were no results.

### The loop guard — the more important fix

Six identical calls is a structural problem, not a search problem. A model
that gets an unsatisfying result naturally tries again; it has no real sense
of "I already did that" beyond what's in the transcript, and a failure
message is easy to skim past.

So `llm.py` now remembers every `(tool, arguments)` pair it has run this turn:

```python
signature = f"{call['name']}::{json.dumps(call['input'], sort_keys=True)}"
if signature in seen_calls:
    # hand back last time's result + "do NOT call it again"
```

`MAX_STEPS` alone was not enough — it capped the damage at six calls instead
of preventing it. Result:

```
before:  6 tool calls, then "Stopped after 6 rounds without finishing"
after:   1 tool call,  "I'm currently unable to search the web... please
         check back later"    (3.2s)
```

Note it also protects side-effecting tools. Two identical `create_event`
calls would mean two identical calendar entries.

---

## 10. Try it

- *"Remember that I have a DBMS exam on the 9th"* → stored
- *"What do you know about me?"* → `list_memories`
- Close everything, reopen, **new conversation**: *"when's my exam?"*
- *"Forget that"* → `forget`
- *"मुझे याद दिलाना कि मैं vegetarian हूँ"* → works, memory is language-agnostic

Look at what's stored any time: <http://localhost:8321/api/memory>

---

## 11. Exercises

1. **Watch retrieval happen.** Add a `print(context)` in `llm.py` before the
   provider call. Ask a few things. See exactly what gets injected.
2. **Break the threshold.** Set `MIN_SCORE = 0.1`. Now everything "matches".
   Notice Sid getting distracted by irrelevant facts. That's why the floor
   exists.
3. **Feel the token cost.** Store 30 facts, then check `input_tokens` in the
   console. Every one is paid for on every request.
4. **Open the database.** `data/memory.db` in any SQLite viewer. It's just
   two tables. Demystifying.
5. **Test the loop guard.** Temporarily make a tool always return "error",
   then ask something needing it. One call, not six.
6. **Prove cross-device memory.** Tell Sid something on your laptop, then
   open it on your phone and ask.

---

## 12. Glossary

| Term | Plain meaning |
|---|---|
| **Embedding** | Text turned into a point in space, positioned by meaning |
| **Cosine similarity** | How close two meanings are, 0 to 1 |
| **Unit vector** | Length normalised to 1, so cosine = dot product |
| **top-k** | Keep the k best matches |
| **Brute force search** | Compare against everything. Fine until ~100k rows |
| **Grounding** | A model searching the live web and citing sources |
| **Loop guard** | Refusing to run an identical tool call twice in one turn |

---

## 13. What Phase 5 changes

Sid now knows things. But it still **improvises one step at a time** — call a
tool, look at the result, decide what's next. That's why a two-tool question
takes two round trips, and why nothing ever runs in parallel.

Phase 5 makes it **plan**: write the whole step graph up front, with
dependencies, so independent steps run at the same time. *"Check my exam
date and the weather and flight prices"* becomes three parallel branches
instead of three sequential waits.

That's also where the loop guard and `MAX_STEPS` get replaced by something
better — a plan you can inspect before it runs.

**Check you can answer:**

- Why are profile facts never searched?
- Why is a weak semantic match worse than no match?
- Why does normalising vectors make search a single matrix multiply?
- Why is memory injected into the system prompt rather than as a message?
- Why is `forget` allowed to run without approval?
- Why wasn't `MAX_STEPS` enough to prevent the six-call loop?

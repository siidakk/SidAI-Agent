"""
memory.py — what Sid knows about you, and what it did before.

THE THREE KINDS OF MEMORY, and why they're different
----------------------------------------------------
Lumping these together is the classic mistake. They have different
lifetimes, different sizes, and completely different retrieval rules:

  1. PROFILE   Durable facts about you. "Studies at Manipal." "Hates morning
               flights." There are maybe dozens, they rarely change, and
               they're relevant to almost everything — so they are ALWAYS
               injected, no search required.

  2. EPISODIC  The conversation log. Thousands of rows, mostly irrelevant to
               right now, but occasionally exactly what you need. Stored so
               that every device sees the same history.

  3. SEMANTIC  Search across everything by MEANING rather than keyword.
               "What did I say about my internship?" should find a message
               that said "the Amazon offer" without either word matching.

THE ACTUAL HARD PART IS RETRIEVAL
---------------------------------
Storing things is easy. The difficulty is that you cannot send everything —
Phase 1 §4 explained that the model is stateless and re-reads the whole
prompt each time, so every fact you inject costs tokens on every request,
forever.

So the job is: given this message, which handful of things are worth
spending context on? Get that wrong in one direction and Sid seems
forgetful; wrong in the other and it's slow, expensive, and distracted by
irrelevant trivia.

WHY SQLITE AND NOT POSTGRES
---------------------------
The roadmap said Postgres + pgvector. This is SQLite instead, deliberately:

  - No server, no Docker, no daemon eating RAM on an 8 GB laptop.
  - It's one file you can copy, delete, or inspect.
  - At this scale, brute-force cosine similarity in numpy over a few
    thousand vectors takes under a millisecond — genuinely faster than the
    network round-trip to a local Postgres would be.

Postgres earns its place when you have concurrent writers or millions of
rows. A personal assistant has one user. Reach for the smaller tool until
the bigger one is actually justified.
"""

import json
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import config

DB_PATH = config.ROOT / "data" / "memory.db"

# Gemini's embedding model. Free, and we ask for 768 dimensions rather than
# the default 3072: a quarter of the storage and a quarter of the comparison
# work, for no meaningful loss on short personal facts.
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMS = 768

# How many remembered things to inject per message. Small on purpose - see
# the retrieval discussion above. Every one costs tokens on every request.
TOP_K = 5

# Below this cosine similarity, a "match" is just noise. Injecting weakly
# related facts is worse than injecting none: it fills the context and
# actively distracts the model.
MIN_SCORE = 0.55


# ==========================================================================
#  Storage
# ==========================================================================
def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    """Create the tables. Safe to call every startup."""
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS facts (
                id         INTEGER PRIMARY KEY,
                text       TEXT NOT NULL,
                kind       TEXT NOT NULL DEFAULT 'note',
                created_at TEXT NOT NULL,
                embedding  BLOB
            );

            CREATE TABLE IF NOT EXISTS messages (
                id           INTEGER PRIMARY KEY,
                conversation TEXT NOT NULL,
                role         TEXT NOT NULL,
                content      TEXT NOT NULL,
                created_at   TEXT NOT NULL
            );

            -- Without this, loading a conversation scans every message ever
            -- written. Fine at 100 rows, painful at 100,000.
            CREATE INDEX IF NOT EXISTS idx_messages_conv
                ON messages(conversation, id);

            CREATE INDEX IF NOT EXISTS idx_facts_kind ON facts(kind);
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ==========================================================================
#  Embeddings — turning text into a point in space
# ==========================================================================
async def embed(text: str) -> np.ndarray | None:
    """
    Turn text into a vector whose direction encodes its MEANING.

    Two sentences that mean similar things end up pointing in similar
    directions, even with no words in common. That is what makes "my
    internship" find "the Amazon offer" — keyword search never could.

    Returns None if embedding isn't available (no key, no internet, quota).
    Everything downstream treats that as "no semantic search today" rather
    than an error: profile facts still work, the conversation still works.
    Degrade, don't collapse.
    """
    if not config.GEMINI_API_KEY:
        return None

    import httpx

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{EMBED_MODEL}:embedContent",
                headers={"x-goog-api-key": config.GEMINI_API_KEY},
                json={
                    "model": f"models/{EMBED_MODEL}",
                    "content": {"parts": [{"text": text[:8000]}]},
                    "outputDimensionality": EMBED_DIMS,
                },
            )
        if response.status_code != 200:
            return None
        values = response.json()["embedding"]["values"]
    except Exception:
        return None

    vector = np.array(values, dtype=np.float32)

    # Normalise to unit length. Then cosine similarity - which is what we
    # actually want - becomes a plain dot product, and comparing against
    # every stored vector becomes one matrix multiply.
    norm = np.linalg.norm(vector)
    return vector / norm if norm else vector


def _pack(vector: np.ndarray) -> bytes:
    return vector.astype(np.float32).tobytes()


def _unpack(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


# ==========================================================================
#  Facts
# ==========================================================================
async def remember(text: str, kind: str = "note") -> int:
    """Store something worth keeping. Returns its id."""
    text = text.strip()
    if not text:
        return 0

    vector = await embed(text)

    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO facts (text, kind, created_at, embedding) VALUES (?,?,?,?)",
            (text, kind, _now(), _pack(vector) if vector is not None else None),
        )
        return cursor.lastrowid


def profile() -> list[str]:
    """
    Every profile fact. These are ALWAYS injected - no search.

    Searching them would be wrong: "I'm vegetarian" is relevant when booking
    a restaurant even though nothing in "book me dinner" resembles it
    semantically. Some things you just have to always know.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT text FROM facts WHERE kind='profile' ORDER BY id"
        ).fetchall()
    return [r["text"] for r in rows]


async def search(query: str, limit: int = TOP_K) -> list[dict]:
    """
    Find the most semantically similar stored facts.

    Brute force: compare against every vector. At a few thousand rows this
    is one numpy matrix multiply and takes well under a millisecond. An
    index (FAISS, pgvector) only starts winning in the hundreds of
    thousands - and it costs build time, memory and a dependency.
    """
    vector = await embed(query)
    if vector is None:
        return []

    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, text, kind, created_at FROM facts "
            "WHERE embedding IS NOT NULL AND kind != 'profile'"
        ).fetchall()
        blobs = conn.execute(
            "SELECT embedding FROM facts "
            "WHERE embedding IS NOT NULL AND kind != 'profile'"
        ).fetchall()

    if not rows:
        return []

    matrix = np.vstack([_unpack(b["embedding"]) for b in blobs])
    scores = matrix @ vector          # every vector is unit length -> cosine

    best = np.argsort(scores)[::-1][:limit]
    return [
        {**dict(rows[i]), "score": float(scores[i])}
        for i in best
        if scores[i] >= MIN_SCORE
    ]


def forget(fact_id: int) -> bool:
    with _connect() as conn:
        return conn.execute("DELETE FROM facts WHERE id=?", (fact_id,)).rowcount > 0


def all_facts(limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, text, kind, created_at FROM facts ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def count() -> dict:
    with _connect() as conn:
        facts = conn.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"]
        msgs = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
        prof = conn.execute(
            "SELECT COUNT(*) c FROM facts WHERE kind='profile'"
        ).fetchone()["c"]
    return {"facts": facts, "profile": prof, "messages": msgs}


# ==========================================================================
#  Conversation log
# ==========================================================================
def log_message(conversation: str, role: str, content: str) -> None:
    """Append one message. This is what makes history follow you between devices."""
    if not content.strip():
        return
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages (conversation, role, content, created_at) "
            "VALUES (?,?,?,?)",
            (conversation, role, content, _now()),
        )


def history(conversation: str, limit: int = 50) -> list[dict]:
    """The most recent messages, oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE conversation=? ORDER BY id DESC LIMIT ?",
            (conversation, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def conversations(limit: int = 20) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT conversation, COUNT(*) n, MAX(created_at) last, "
            "       MIN(content) first_message "
            "FROM messages GROUP BY conversation ORDER BY last DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_conversation(conversation: str) -> int:
    with _connect() as conn:
        return conn.execute(
            "DELETE FROM messages WHERE conversation=?", (conversation,)
        ).rowcount


# ==========================================================================
#  What actually goes into the prompt
# ==========================================================================
async def build_context(user_message: str) -> str:
    """
    Assemble the memory block injected into the system prompt.

    Profile facts always. Semantically relevant ones only if they clear
    MIN_SCORE. Returns "" when there's nothing worth saying, so an empty
    memory costs exactly zero tokens.
    """
    lines = []

    facts = profile()
    if facts:
        lines.append("What you know about the user:")
        lines += [f"- {f}" for f in facts]

    relevant = await search(user_message)
    if relevant:
        if lines:
            lines.append("")
        lines.append("Possibly relevant things you were told earlier:")
        # Score shown deliberately. When Sid brings up something odd, the
        # trace tells you whether retrieval misfired or the model did.
        lines += [f"- {r['text']}  (relevance {r['score']:.2f})" for r in relevant]

    if not lines:
        return ""

    return (
        "\n\n--- MEMORY ---\n"
        + "\n".join(lines)
        + "\n--- END MEMORY ---\n"
        "Use these if they help. Do not mention that you looked them up, and "
        "do not repeat them back unless asked."
    )

"""
tools/memory_tools.py — letting Sid write its own memory.

THE INTERESTING BIT
-------------------
Sid decides what's worth remembering. You don't fill in a profile form; you
mention in passing that you're vegetarian, and it calls `remember`.

That only works if the tool descriptions are good, because the docstring IS
the instruction (Phase 2 §4). `remember`'s docstring has to convey a
judgement call — durable facts yes, passing chatter no — which is a harder
thing to write than "this adds two numbers".

Getting it wrong is visible in both directions. Too eager and the memory
fills with "user said hello", every one of which then costs tokens on every
future request. Too shy and it forgets the things you actually told it.
"""

from .. import memory
from . import tool


@tool(tier="act")
async def remember(fact: str, kind: str = "note") -> str:
    """Save something about the user so you still know it in future conversations.

    Use this WITHOUT being asked whenever the user reveals something durable:
    their name, where they study or work, preferences ("I hate morning
    flights", "I'm vegetarian"), important dates, people they mention often,
    how they like you to behave.

    Do NOT save passing chatter, one-off questions, or anything you can look
    up with a tool. Ask yourself: would this still be worth knowing next
    month? If not, don't save it.

    Args:
        fact: The thing to remember, written as a complete sentence in the
              third person, e.g. "Malika is a student at Manipal"
        kind: "profile" for durable facts about the user (always recalled),
              or "note" for details that only matter sometimes
    """
    kind = kind if kind in ("profile", "note") else "note"

    fact_id = await memory.remember(fact, kind)
    if not fact_id:
        return "Nothing to remember - the text was empty."

    where = "profile (always recalled)" if kind == "profile" else "notes (recalled when relevant)"
    return f"Remembered as {where}: {fact}"


@tool(tier="read")
async def recall(query: str) -> str:
    """Search everything you have been told about the user, by meaning.

    Use this when the user refers to something from a previous conversation
    that isn't already in front of you - "what did I say about my
    internship?", "the thing I mentioned last week".

    You do not need this for basic facts about the user; those are already
    given to you automatically.

    Args:
        query: What you're looking for, e.g. "internship deadline"
    """
    results = await memory.search(query, limit=8)
    if not results:
        return f"Nothing stored about '{query}'."

    lines = [f"{len(results)} match(es) for '{query}':"]
    for r in results:
        when = r["created_at"][:10]
        lines.append(f"  [{r['id']}] {r['text']}  ({when}, relevance {r['score']:.2f})")
    return "\n".join(lines)


@tool(tier="read")
async def list_memories() -> str:
    """Show everything currently remembered about the user.

    Use this when the user asks what you know or remember about them.
    """
    facts = memory.all_facts(limit=60)
    if not facts:
        return "Nothing remembered yet."

    stats = memory.count()
    profile = [f for f in facts if f["kind"] == "profile"]
    notes = [f for f in facts if f["kind"] != "profile"]

    lines = [f"{stats['facts']} things remembered "
             f"({stats['profile']} profile), {stats['messages']} messages logged."]

    if profile:
        lines.append("\nProfile:")
        lines += [f"  [{f['id']}] {f['text']}" for f in profile]
    if notes:
        lines.append("\nNotes:")
        lines += [f"  [{f['id']}] {f['text']}  ({f['created_at'][:10]})" for f in notes[:30]]

    return "\n".join(lines)


@tool(tier="act")
async def forget(fact_id: int) -> str:
    """Delete one remembered fact. Get its id from list_memories or recall first.

    Args:
        fact_id: The number shown in brackets, e.g. 7
    """
    # `act` rather than `danger`: forgetting one fact is annoying, not
    # catastrophic, and stopping to approve every correction would make Sid
    # tedious to correct. Corrections need to be cheap or people stop making
    # them - and a memory nobody corrects gets wrong and stays wrong.
    try:
        fact_id = int(fact_id)
    except (TypeError, ValueError):
        return f"'{fact_id}' isn't a valid id. Use list_memories to see them."

    if memory.forget(fact_id):
        return f"Forgotten (id {fact_id})."
    return f"No memory with id {fact_id}."

"""
tools/schedule_tools.py — letting Sid schedule its own work.

"Every morning at 8, check my email and tell me what needs a reply" should
create a trigger, not a note reminding you to ask again tomorrow.

That's the whole point of Phase 9: Sid stops waiting to be asked.

A LESSON ABOUT TOOL INTERFACES
------------------------------
The first version of `schedule_task` took three arguments: kind="daily",
spec="07:30". Perfectly clean - and the very first call written against it,
by me, with the source open in front of me, passed `when="daily 07:30"`
instead. Because that is how the request is phrased.

If the person who wrote the tool gets the shape wrong, a model reading only
the docstring certainly will.

So it takes ONE argument now, `when`, in the words a person would use, and
this file does the translating. Parsing is cheap; a failed tool call costs a
whole model round-trip and some of the user's patience.

> **Put the awkwardness in the code, not in the interface.**

The docstrings carry real weight too. `schedule_task` takes a `prompt` that
runs with no one watching, so the description has to push the model toward
prompts that stand alone - not ones assuming a human is sitting there to
answer a follow-up.
"""

import re
from datetime import datetime, timedelta

from .. import triggers
from . import tool


def _parse_when(text: str) -> tuple[str, str] | None:
    """
    Turn how a person says it into (kind, spec).

    Deliberately forgiving. Every phrasing understood here is a model
    round-trip that doesn't have to happen.

        "daily 07:30"      "every morning at 7:30am"  -> daily, "07:30"
        "every 45 minutes" "hourly"                   -> interval, "45"/"60"
        "in 20 minutes"    "in 2 hours"               -> once, ISO timestamp
    """
    s = " ".join(str(text).lower().split())
    if not s:
        return None

    # --- relative one-offs: "in 20 minutes", "in 2 hours" ----------------
    m = re.search(r"\bin\s+(\d+)\s*(min|minute|minutes|hour|hours|hr|hrs)\b", s)
    if m:
        n = int(m.group(1))
        delta = (timedelta(hours=n) if m.group(2).startswith(("hour", "hr"))
                 else timedelta(minutes=n))
        return "once", (datetime.now().astimezone() + delta).isoformat()

    # --- intervals: "every 30 minutes", "hourly", "every 2 hours" --------
    if "hourly" in s:
        return "interval", "60"
    m = re.search(r"every\s+(\d+)\s*(min|minute|minutes|hour|hours|hr|hrs)\b", s)
    if m:
        n = int(m.group(1))
        return "interval", str(n * 60 if m.group(2).startswith(("hour", "hr")) else n)
    m = re.match(r"^interval\s+(\d+)$", s)
    if m:
        return "interval", m.group(1)

    # --- a clock time, with or without am/pm -----------------------------
    # Checked AFTER intervals, so "every 2 hours" isn't read as "2 o'clock".
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", s)
    daily_words = any(w in s for w in (
        "daily", "every day", "each day", "every morning", "every evening",
        "every night", "morning", "evening", "night", "at"))
    if m and daily_words:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        suffix = m.group(3)
        if suffix == "pm" and hour < 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
        elif (suffix is None and 1 <= hour <= 7 and "morning" not in s
                and m.group(2) is None and not m.group(1).startswith("0")):
            # "at 7" from someone awake and typing usually means the evening
            # one. But only guess when the hour was written BARE - "07:30"
            # and "7:30" are someone being specific, and overriding that
            # turned "daily 07:30" into a 19:30 alarm on the first test.
            #
            # Guess only where there is genuine ambiguity.
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return "daily", f"{hour:02d}:{minute:02d}"

    # --- an explicit ISO timestamp ---------------------------------------
    try:
        datetime.fromisoformat(str(text).strip())
        return "once", str(text).strip()
    except ValueError:
        pass

    return None


@tool(tier="act")
async def schedule_task(name: str, when: str, prompt: str) -> str:
    """Set something to run automatically later, or on a repeating schedule.

    Use this whenever the user says "every day", "every morning", "each
    week", "remind me at", "keep checking", or "in an hour".

    The prompt runs later with NOBODY WATCHING, so write it to stand alone:
    it cannot ask follow-up questions. Tell it to stay quiet when there is
    nothing worth saying - a notification that fires daily saying "nothing to
    report" gets ignored within a week.

    Args:
        name: Short label shown in notifications, e.g. "Morning inbox"
        when: Plain English. "every day at 08:00", "every 30 minutes",
              "hourly", "in 2 hours", or an ISO timestamp for a one-off.
        prompt: What Sid should actually do, as a complete standalone request
    """
    parsed = _parse_when(when)
    if parsed is None:
        return (f"I couldn't work out when '{when}' means. Try something like "
                f"'every day at 08:00', 'every 30 minutes', or 'in 2 hours'.")

    kind, spec = parsed
    try:
        trigger = triggers.create(name, kind, spec, prompt)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Could not schedule that: {exc}"

    first = trigger["next_run"][:16].replace("T", " ")
    repeats = {"daily": "every day", "interval": f"every {spec} minutes",
               "once": "once only"}[kind]
    return f"Scheduled '{name}' ({trigger['id']}) - {repeats}. First run: {first}."


@tool(tier="read")
async def list_schedules() -> str:
    """Show everything Sid is set to do automatically."""
    rows = triggers.all_triggers()
    if not rows:
        return "Nothing is scheduled."

    lines = [f"{len(rows)} scheduled task(s):"]
    for t in rows:
        state = "on " if t["enabled"] else "off"
        nxt = (t["next_run"] or "")[:16].replace("T", " ")
        lines.append(
            f"  [{t['id']}] {state}  {t['name']} - {t['kind']} {t['spec']}"
            + (f", next {nxt}" if t["enabled"] and nxt else "")
        )
        lines.append(f"        does: {t['prompt'][:80]}")
    return "\n".join(lines)


@tool(tier="act")
async def cancel_schedule(schedule_id: str) -> str:
    """Delete a scheduled task, by its id or its name.

    Pass ONE id like "a1b2c3d4e5", or the task's exact name. Do NOT pass the
    whole output of list_schedules.

    Args:
        schedule_id: A single id, e.g. "a1b2c3d4e5", or the task's name
    """
    # `act` rather than `danger`: cancelling stops something future from
    # happening. Annoying to redo, but nothing is destroyed - and making it
    # HARD to stop an automation would be exactly the wrong way round.
    key = str(schedule_id).strip()

    if triggers.delete(key):
        return f"Cancelled {key}."

    # Match by name. The model usually has the name to hand (the user just
    # said it) and the id only if it called list_schedules first.
    for t in triggers.all_triggers():
        if t["name"].lower() == key.lower():
            triggers.delete(t["id"])
            return f"Cancelled '{t['name']}' ({t['id']})."

    # THE {{s1}} PROBLEM.
    #
    # A plan that chains list_schedules -> cancel_schedule substitutes the
    # FULL TEXT of the listing into this argument, because that is what
    # {{s1}} means. Observed exactly once and then every time:
    #
    #     cancel_schedule(schedule_id="1 scheduled task(s):\n  [83d2c19a37]
    #     on   Morning ping - daily 07:30...")
    #
    # Piping one tool's output into another's argument is the whole point of
    # {{s1}}, so this will keep happening. Dig the id out when there is
    # exactly one; say so plainly when there are several, because guessing
    # which schedule to delete is not a decision this function should make.
    found = re.findall(r"\[([0-9a-f]{6,32})\]", key)
    unique = list(dict.fromkeys(found))
    if len(unique) == 1 and triggers.delete(unique[0]):
        return f"Cancelled {unique[0]}."
    if len(unique) > 1:
        return (f"That names {len(unique)} schedules ({', '.join(unique)}). "
                f"Call cancel_schedule once per id, not with the whole list.")

    return f"No scheduled task matching '{key[:60]}'. Use list_schedules to see them."

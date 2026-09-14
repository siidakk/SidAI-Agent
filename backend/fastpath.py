"""
fastpath.py — the commands that shouldn't need a model at all.

WHY THIS EXISTS
---------------
Measured across 60 real turns: a planned turn costs ~4 seconds, and about
1.4s of that is a model call whose entire job is to look at "volume 30" and
decide it means `set_volume(30)`.

For a handful of phrasings that decision is not a judgement call. It is a
lookup, and a lookup does not need a language model.

    "volume 30"      ->  set_volume(30)          no model call
    "click send"     ->  click_on("send")        no model call
    "lock my pc"     ->  lock_screen()           no model call

Everything else - anything with nuance, context, several steps, or a
phrasing not listed here - falls straight through to the planner exactly as
before. This is a shortcut past the model, never a replacement for it.

⚠️ WHAT THIS IS NOT ALLOWED TO SKIP
-----------------------------------
It still calls `tools.run()`, the same choke point everything else uses. So
dry-run, the approval policy and the audit log all apply unchanged.

A fast path that bypassed those would be a hole straight through every
safety property in the project, and it would be invisible - the audit log
would simply have gaps where the quick commands went. **Speed work must
never route around the checkpoint.**

Nothing in the `danger` tier is ever matched here. Those exist to be
stopped and looked at; making them instant is the exact opposite of the
point.

HOW IT STAYS HONEST
-------------------
Every pattern is anchored to the WHOLE message and capped in length. A
fast path that fires on a sentence it half-recognised is far worse than no
fast path: it would confidently do the wrong thing, instantly.

When in doubt, return None and let the model decide. Missing a shortcut
costs 1.4 seconds; taking a wrong one costs trust.
"""

import re

# Hinglish matters here, not as a nicety. Volume kam karo and lock kar do
# are how these commands actually get typed on this machine, and a fast
# path that only knows English would quietly never fire for half of them.
_NUM = r"(\d{1,3})"

PATTERNS: list[tuple[str, str, object]] = [
    # ---- time -----------------------------------------------------------
    (r"(?:what(?:'s| is)?\s*)?(?:the\s+)?time(?:\s+(?:is\s+it|now|kya\s+hai|hai))?[\?\.!]?",
     "get_time", lambda m: {}),
    (r"(?:abhi )?kitne baje (?:hain|hai)[\?\.!]?", "get_time", lambda m: {}),

    # ---- volume ---------------------------------------------------------
    (rf"(?:set |make )?(?:the )?volume (?:to |ko )?{_NUM}%?[\.!]?",
     "set_volume", lambda m: {"level": int(m.group(1))}),
    (rf"volume {_NUM}%? ?(?:kar do|karo|kardo)[\.!]?",
     "set_volume", lambda m: {"level": int(m.group(1))}),
    (rf"{_NUM}%? volume(?: kar do| karo)?[\.!]?",
     "set_volume", lambda m: {"level": int(m.group(1))}),

    # ---- media ----------------------------------------------------------
    (r"(?:pause|stop) (?:the )?(?:music|song|video|gaana)[\.!]?",
     "stop_music", lambda m: {}),
    (r"(?:music|gaana|song) (?:band|bandh) (?:kar do|karo|kardo)[\.!]?",
     "stop_music", lambda m: {}),
    (r"pause[\.!]?", "control_media", lambda m: {"action": "pause"}),
    (r"(?:play|resume)[\.!]?", "control_media", lambda m: {"action": "play"}),
    (r"next(?: track| song| gaana)?[\.!]?",
     "control_media", lambda m: {"action": "next"}),
    (r"(?:previous|prev|last)(?: track| song| gaana)?[\.!]?",
     "control_media", lambda m: {"action": "previous"}),

    # ---- youtube --------------------------------------------------------
    # "play X" with something after it. The bare "play" above is caught
    # first, so this only ever sees a real title.
    (r"play (?:me )?(.{2,60}?)(?: on youtube| youtube pe| pe)?[\.!]?",
     "play_on_youtube", lambda m: {"query": m.group(1).strip()}),
    (r"(.{2,60}?) (?:gaana |song )?(?:baja do|bajao|chala do|chalao)[\.!]?",
     "play_on_youtube", lambda m: {"query": m.group(1).strip()}),

    # ---- the screen -----------------------------------------------------
    (r"click (?:on )?(?:the )?(.{2,60}?)[\.!]?", "click_on",
     lambda m: {"target": m.group(1).strip()}),
    (r"double.?click (?:on )?(?:the )?(.{2,60}?)[\.!]?", "click_on",
     lambda m: {"target": m.group(1).strip(), "clicks": 2}),
    (r"(?:what(?:'s| is)? on (?:my |the )?screen|screen (?:pe |par )?kya hai)[\?\.!]?",
     "see_screen", lambda m: {}),

    # ---- apps and windows ----------------------------------------------
    (r"open (?:the )?(.{2,40}?)[\.!]?", "open_app",
     lambda m: {"name": m.group(1).strip()}),
    (r"(.{2,40}?) (?:kholo|khol do)[\.!]?", "open_app",
     lambda m: {"name": m.group(1).strip()}),
    (r"close (?:the )?(.{2,40}?)[\.!]?", "close_app",
     lambda m: {"name": m.group(1).strip()}),
    (r"(?:what(?:'s| is)? open|list (?:my )?windows)[\?\.!]?",
     "list_windows", lambda m: {}),

    # ---- the machine ----------------------------------------------------
    (r"lock (?:my |the )?(?:pc|laptop|screen|computer)[\.!]?",
     "lock_screen", lambda m: {}),
    (r"(?:pc|laptop|screen) lock (?:kar do|karo|kardo)[\.!]?",
     "lock_screen", lambda m: {}),

    # ---- schedules ------------------------------------------------------
    (r"(?:what(?:'s| is)? scheduled|list (?:my )?schedules|"
     r"what automatic tasks?(?: do you have)?)[\?\.!]?",
     "list_schedules", lambda m: {}),
]

# Compiled once, anchored to the WHOLE message so a pattern can never fire
# on a fragment of a longer sentence.
_COMPILED = [(re.compile(rf"^\s*{pat}\s*$", re.I), name, build)
             for pat, name, build in PATTERNS]

# Above this, it is a sentence rather than a command. "Open the report and
# tell me what the third paragraph says" must reach the planner.
MAX_LENGTH = 70


def match(text: str) -> tuple[str, dict] | None:
    """
    Is this a command we can run without asking a model?

    Returns (tool_name, arguments) or None. None is always the safe answer,
    and the default for anything not obviously one of these.
    """
    if not text:
        return None
    message = " ".join(text.strip().split())
    if len(message) > MAX_LENGTH:
        return None

    for pattern, name, build in _COMPILED:
        found = pattern.match(message)
        if not found:
            continue
        try:
            args = build(found)
        except Exception:
            return None

        # A last sanity check on anything numeric. "volume 900" is a typo,
        # not an instruction, and the planner will handle it more gracefully
        # than clamping silently would.
        if name == "set_volume" and not (0 <= args.get("level", -1) <= 100):
            return None
        return name, args
    return None

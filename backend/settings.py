"""
settings.py — small runtime switches you can flip from the UI.

WHY NOT .env?
-------------
.env is read once at import. A toggle has to take effect NOW, in a process
that's already running — and in a *different* process at that: the wake word
listener is its own program, so an in-memory flag in the server would be
invisible to it.

So this is a tiny JSON file that both processes read. The listener re-reads
it while running; the server writes it when you flip the switch. A file is
the simplest thing two separate programs can agree on without inventing a
protocol between them.

Deliberately separate from .env: .env is *configuration* you set up once,
this is *state* you change while using it. Mixing the two means your setup
file keeps mutating under you.
"""

import json
from pathlib import Path

from . import config

STATE_PATH = config.ROOT / "data" / "state.json"

DEFAULTS = {
    # Should "Hey Sid" wake it? Off means the listener stops analysing audio
    # entirely — not "hears you and ignores it".
    "wake_enabled": True,

    # DRY RUN. Nothing that changes anything actually runs; `act` and
    # `danger` tools report what they WOULD have done. `read` tools still
    # run, because seeing real data is how you judge whether a plan is
    # sensible — and looking at things changes nothing.
    #
    # This is the setting to reach for the first time you let Sid loose on
    # something unfamiliar.
    "dry_run": False,

    # Ask before `act` tools too, not just `danger` ones. Off by default —
    # needing permission to change the volume would make Sid unusable — but
    # it's here for anyone who wants a tighter leash.
    "confirm_act": False,
}


def all() -> dict:
    """Every setting, with defaults filled in for anything missing."""
    try:
        stored = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        # Missing, empty or corrupt: fall back to defaults rather than
        # crashing. A broken settings file should never stop the app.
        stored = {}
    return {**DEFAULTS, **stored}


def get(key: str, default=None):
    return all().get(key, DEFAULTS.get(key, default))


def set(key: str, value) -> dict:
    """Write one setting. Returns the full settings afterwards."""
    current = all()
    current[key] = value

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file then swap it in. Without this, the listener can
    # read the file mid-write and get truncated JSON — rare, but it would
    # look like a random unexplained failure.
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(current, indent=2), encoding="utf-8")
    temp.replace(STATE_PATH)

    return current

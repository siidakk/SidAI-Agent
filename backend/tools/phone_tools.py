"""
tools/phone_tools.py — reaching your iPhone.

Everything here QUEUES rather than does. See backend/phone.py for why: iOS
does not let a laptop drive a phone, so the phone collects instead. These
tools put work in a queue; the Shortcut on the phone picks it up next time
it runs.

WHAT THE MODEL HAS TO BE TOLD, AND WHY IT MATTERS
-------------------------------------------------
The docstrings below say "queued" rather than "sent", and that word is
load-bearing. If they claimed the message was sent, the model would tell
you it was sent - and you would find out it wasn't when your friend never
replied.

This is the same failure as Phase 7's dry run reporting success, and the
same fix: **if the tool cannot finish the job, its description must not
imply that it did.**
"""

from .. import phone
from . import tool


def _queued(action: str, args: dict, described: str) -> str:
    try:
        phone.queue(action, args)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Could not queue that: {exc}"

    seen = phone.last_seen()
    when = ("Your phone will pick it up next time it checks."
            if seen else
            "Note: your phone has never checked in yet - the Shortcut may "
            "not be set up. See NOTES/phone-bridge.md.")
    return f"Queued for your phone: {described}. {when}"


@tool(tier="act", speaks_for_itself=True)
async def phone_message(to: str, body: str) -> str:
    """Queue a text message to be sent from the user's phone.

    The phone sends it when it next checks in - this does NOT send it
    immediately, so never tell the user it has been sent.

    Args:
        to: Who to send it to, as it appears in their contacts
        body: The message text
    """
    return _queued("message", {"to": to, "body": body},
                   f'text to {to}: "{body[:60]}"')


@tool(tier="act", speaks_for_itself=True)
async def phone_play(query: str) -> str:
    """Queue music to play on the user's phone.

    Args:
        query: Song, artist or playlist, e.g. "tum hi ho"
    """
    return _queued("play", {"query": query}, f"play {query}")


@tool(tier="act", speaks_for_itself=True)
async def phone_open(name: str) -> str:
    """Queue an app to open on the user's phone.

    Args:
        name: The app, e.g. "Spotify", "Maps", "WhatsApp"
    """
    return _queued("open", {"name": name}, f"open {name}")


@tool(tier="act", speaks_for_itself=True)
async def phone_timer(minutes: int) -> str:
    """Queue a timer on the user's phone.

    Args:
        minutes: How many minutes
    """
    return _queued("timer", {"minutes": int(minutes)}, f"{minutes} minute timer")


@tool(tier="act", speaks_for_itself=True)
async def phone_speak(text: str) -> str:
    """Queue something for the phone to say out loud.

    Useful for a reminder you want to HEAR when you pick the phone up.

    Args:
        text: What to say
    """
    return _queued("speak", {"text": text}, f'say "{text[:60]}"')


@tool(tier="act", speaks_for_itself=True)
async def phone_note(text: str) -> str:
    """Queue a note to be added in the phone's Notes app.

    Args:
        text: What to write down
    """
    return _queued("note", {"text": text}, f'note: "{text[:60]}"')


@tool(tier="act", speaks_for_itself=True)
async def phone_reminder(text: str) -> str:
    """Queue a reminder to be added on the phone.

    Args:
        text: What to be reminded about
    """
    return _queued("reminder", {"text": text}, f'reminder: "{text[:60]}"')


@tool(tier="read")
async def phone_status() -> str:
    """Check the phone bridge: what's waiting, and whether the phone is checking in.

    Use this when the user asks whether something reached their phone.
    """
    waiting = phone.pending()
    seen = phone.last_seen()
    done = [c for c in phone.recent(8) if c["status"] == "done"]

    lines = []
    if seen:
        lines.append(f"Phone last checked in at {seen[:19].replace('T', ' ')} UTC.")
    else:
        lines.append("Your phone has NEVER checked in. The Shortcut is "
                     "probably not set up yet - see NOTES/phone-bridge.md.")

    if waiting:
        lines.append(f"{len(waiting)} waiting to be collected:")
        for c in waiting:
            lines.append(f"  {c['action']} ({c['status']})")
    else:
        lines.append("Nothing waiting.")

    if done:
        lines.append("Recently done:")
        for c in done[:3]:
            lines.append(f"  {c['action']} -> {(c['result'] or 'ok')[:50]}")
    return "\n".join(lines)


@tool(tier="read")
async def phone_battery() -> str:
    """Ask the phone for its battery level.

    The answer arrives when the phone next checks in, not immediately.
    """
    return _queued("battery", {}, "battery check")

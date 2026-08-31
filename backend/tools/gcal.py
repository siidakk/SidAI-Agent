"""
tools/gcal.py — your Google Calendar.

Calmer than Gmail: a calendar contains your own text, not strangers'. The
prompt-injection risk is much lower (though not zero — anyone can send you an
invite with a crafted description).

The interesting problem here is TIME, which is genuinely fiddly:

  - The model doesn't know what "now" is. It must call get_time first, or it
    will confidently invent a date. The tool descriptions say so explicitly.
  - Google wants RFC-3339 timestamps with an offset: 2026-08-27T14:00:00+05:30
  - "Tomorrow at 4" depends on the user's timezone, not the server's, not UTC.

We let the model do the date arithmetic and we validate the result, rather
than trying to parse English ourselves. Models are good at "next Tuesday";
regexes are not.
"""

from datetime import datetime, timedelta

import httpx

from .. import google_auth
from . import tool

API = "https://www.googleapis.com/calendar/v3"
TIMEOUT = 30.0


async def _request(method: str, path: str, **kwargs) -> dict:
    """
    Call the Calendar API, refreshing the token once if it's rejected.

    Same reasoning as gmail.py: trust the server's 401 over our own idea of
    whether the token is still good. Locally-valid-but-actually-dead tokens
    are the failure mode that produced "authentication error" with no
    obvious cause.
    """
    for attempt in (0, 1):
        token = google_auth.get_access_token(force_refresh=(attempt == 1))
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.request(
                method,
                f"{API}/{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
        if r.status_code in (200, 201, 204):
            return r.json() if r.content else {}
        if r.status_code == 401 and attempt == 0:
            continue
        raise RuntimeError(f"Calendar API {r.status_code}: {r.text[:200]}")


def _when(event: dict) -> str:
    """Format an event's time. All-day events have 'date', timed have 'dateTime'."""
    start = event.get("start", {})

    if "date" in start:
        return f"{start['date']} (all day)"

    raw = start.get("dateTime", "")
    try:
        return datetime.fromisoformat(raw).strftime("%a %d %b, %I:%M %p")
    except ValueError:
        return raw


@tool(tier="read")
async def list_events(days: int = 7) -> str:
    """List the user's upcoming calendar events.

    Args:
        days: How many days ahead to look. 1 for today, 7 for the week.
    """
    now = datetime.now().astimezone()
    try:
        data = await _request(
            "GET", "calendars/primary/events",
            params={
                "timeMin": now.isoformat(),
                "timeMax": (now + timedelta(days=max(days, 1))).isoformat(),
                "singleEvents": "true",       # expand repeating events
                "orderBy": "startTime",       # only allowed with singleEvents
                "maxResults": 20,
            },
        )
    except Exception as exc:
        return f"Could not read the calendar: {exc}"

    events = data.get("items", [])
    if not events:
        return f"Nothing scheduled in the next {days} day(s)."

    lines = [f"{len(events)} event(s) in the next {days} day(s):"]
    for e in events:
        line = f"\n- {_when(e)}  {e.get('summary', '(no title)')}"
        if e.get("location"):
            line += f"\n    at {e['location']}"
        lines.append(line)
    return "".join(lines)


@tool(tier="act")
async def create_event(title: str, start: str, end: str, location: str = "") -> str:
    """Add an event to the user's calendar.

    You MUST call get_time first to find out today's date — you cannot know it
    otherwise, and guessing produces events in the wrong year.

    Args:
        title: What the event is called
        start: Start time as RFC-3339 with offset, e.g. "2026-08-27T14:00:00+05:30"
        end: End time in the same format
        location: Optional place
    """
    # Validate before sending. Google's error for a malformed timestamp is
    # opaque; this one tells the model exactly how to fix its own input,
    # which it can then do on the next loop iteration.
    for label, value in (("start", start), ("end", end)):
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return (
                f"'{label}' is not a valid time: {value!r}. Use RFC-3339 with "
                f"a timezone offset, e.g. 2026-08-27T14:00:00+05:30. "
                f"Call get_time first if you don't know today's date."
            )

    if datetime.fromisoformat(end) <= datetime.fromisoformat(start):
        return "The end time must be after the start time."

    body = {
        "summary": title,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if location:
        body["location"] = location

    try:
        created = await _request("POST", "calendars/primary/events", json=body)
    except Exception as exc:
        return f"Could not create the event: {exc}"

    return (
        f"Added '{title}' on {_when(created)}. "
        f"{created.get('htmlLink', '')}"
    )


@tool(tier="danger")
async def delete_event(event_id: str) -> str:
    """Permanently delete a calendar event. Requires the user's approval.

    Args:
        event_id: The id of the event to delete
    """
    # `danger`, so tools.run() refuses before this body executes. Deletion is
    # irreversible, which is the whole definition of the tier.
    return "delete_event requires approval, which arrives in Phase 7."

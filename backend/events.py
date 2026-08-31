"""
events.py — a way for the server to talk to an already-open page.

THE PROBLEM THIS SOLVES
-----------------------
Saying "Hey Sid" used to open a brand new window every time, because the
only way we knew to start listening was to navigate to /?listen=1 — and
navigating means a new window.

To reuse the window that's already open, the server has to be able to *push*
something to it: "wake up, start listening". But HTTP is client-pull. The
browser asks; the server answers. There's no way for the server to ring the
browser's doorbell out of nowhere.

So the browser opens a connection and leaves it open. The server writes to
it whenever it has something to say. That's Server-Sent Events again — the
same mechanism as the chat stream in Phase 1, used the other way round:
there it carried one reply and closed, here it stays open for the session.

THE DESIGN: a tiny broadcast
----------------------------
Every connected page gets its own asyncio.Queue. `publish()` drops a message
into all of them. Each /api/events request sits reading its own queue and
forwards whatever arrives.

Queues rather than a shared list because each page consumes at its own pace,
and a slow one must not block the others or lose messages meant for it.

This is the foundation for Phase 9 too. "Your exam ends Friday, flights are
cheap" is the same mechanism — server decides something is worth saying,
pushes it to whatever is listening.
"""

import asyncio

# One queue per connected page. A set, so removing a disconnected page is
# instant and duplicates are impossible.
SUBSCRIBERS: set[asyncio.Queue] = set()

# Set during shutdown so open streams end themselves.
#
# WHY THIS EXISTS: an SSE handler is an infinite loop, and a graceful
# shutdown waits for open connections to finish. An infinite loop never
# finishes, so the server hung on "Waiting for connections to close"
# indefinitely - with a single page open, Sid could not be restarted.
#
# THIS FLAG IS NOT THE FIX, and the reason is worth knowing. Uvicorn waits
# for connections to close BEFORE it runs the lifespan shutdown, so setting
# this from lifespan sets it after the hang has already started. Measured:
# still stuck at 155 seconds.
#
# The actual fix is `--timeout-graceful-shutdown 3` on the uvicorn command
# (see Axon.pyw), which bounds the wait from outside.
#
# The flag stays because it makes the streams stop promptly once shutdown
# does begin, rather than being cut off mid-write. The lesson is the
# ordering: **know when your cleanup hook actually runs.** A shutdown hook
# that runs after the thing it was meant to prevent is decoration.
SHUTTING_DOWN = False


def begin_shutdown() -> None:
    """Tell every open stream to wind up, and nudge them awake."""
    global SHUTTING_DOWN
    SHUTTING_DOWN = True
    # The streams are blocked in `wait_for(queue.get(), timeout=10)`. Push
    # something so they wake now rather than up to 10 seconds from now.
    publish({"type": "shutdown"})


def publish(event: dict) -> int:
    """
    Send an event to every connected page. Returns how many got it.

    put_nowait, never await: publishing must not block. If a page has gone
    away without cleaning up, its queue would fill and awaiting would hang
    the caller forever — so the listener's wake POST would time out because
    of an unrelated dead browser tab.
    """
    delivered = 0
    for queue in list(SUBSCRIBERS):
        try:
            queue.put_nowait(event)
            delivered += 1
        except asyncio.QueueFull:
            SUBSCRIBERS.discard(queue)
    return delivered


def subscriber_count() -> int:
    """How many pages are currently listening. Used to decide whether the
    wake word can reuse an open window or has to open a new one."""
    return len(SUBSCRIBERS)

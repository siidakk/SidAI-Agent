"""
tools/pointer.py — Sid's hands, and the finger it points with.

Paired with vision.py: `find_on_screen` says where a thing is, these move
the mouse there and click it. Between them Sid can drive any application
on the machine, including ones with no API, no scripting and no
accessibility support — which is most of them.

WHY THE MOUSE MOVES VISIBLY
---------------------------
Windows can deliver a click without moving the cursor at all. This
deliberately doesn't: it glides the pointer over in a few frames so you
can **see what Sid is about to do** and yank the mouse away if it is about
to click the wrong thing.

An agent that clicks invisibly is an agent you cannot supervise. The
animation costs about a fifth of a second and buys the only interruption
window you get.

WHY CLICKING IS `act` AND NOT `danger`
--------------------------------------
A click is not inherently destructive — most clicks are navigation. Making
every one of them ask would mean twenty prompts to fill in one form, and
a permission you click through twenty times has stopped being a
permission.

The protection is elsewhere and is better: **Sid has to look before it can
click.** Coordinates come from `find_on_screen`, every call is in the
audit log with its position, and the mouse visibly travels there first.

`type_text` and `press_keys` already existed for the keyboard; this is the
other half.
"""

import asyncio
import ctypes
import ctypes.wintypes   # imported HERE, not inside a function - see _glide
import time

from . import tool

# SendInput event flags. Using mouse_event (the older API) is simpler but
# ignores DPI scaling on multi-monitor setups; SetCursorPos + mouse_event
# together behave correctly on the single-display case this targets.
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800


def _user32():
    user32 = ctypes.windll.user32
    # Without this the coordinates a 4K or scaled display reports are not
    # the coordinates the mouse uses, and every click lands slightly off.
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    return user32


def _bounds() -> tuple[int, int]:
    user32 = _user32()
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


async def _glide(x: int, y: int, steps: int = 14) -> None:
    """Move the cursor visibly, so a wrong move can be seen and stopped."""
    user32 = _user32()

    # `import ctypes.wintypes` INSIDE this function made `ctypes` a local
    # name, so the very first use of ctypes.byref below raised
    # UnboundLocalError - a real click failure caused entirely by where an
    # import sat. Any `import a.b` inside a function rebinds `a` locally
    # for the whole function, including lines above it.
    try:
        point = ctypes.wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        start_x, start_y = point.x, point.y
    except Exception:
        start_x, start_y = x, y

    for i in range(1, steps + 1):
        # ease-out: quick at first, settling at the end, which reads as
        # deliberate rather than teleporting
        t = 1 - (1 - i / steps) ** 3
        user32.SetCursorPos(int(start_x + (x - start_x) * t),
                            int(start_y + (y - start_y) * t))
        await asyncio.sleep(0.012)
    user32.SetCursorPos(x, y)


def _check(x: int, y: int) -> str | None:
    w, h = _bounds()
    if not (0 <= x < w and 0 <= y < h):
        return (f"Refused: ({x}, {y}) is outside the {w}x{h} screen. "
                f"Use find_on_screen to get a real position.")
    return None


async def _announce(x: int, y: int) -> None:
    """Move the on-screen ring to where the click is about to land."""
    try:
        from .. import events
        events.publish({"type": "point", "x": x, "y": y})
    except Exception:
        pass


async def _announce_done() -> None:
    try:
        from .. import events
        events.publish({"type": "point_clear"})
    except Exception:
        pass


@tool(tier="act")
async def click_at(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    """Click somewhere on screen. Get x and y from find_on_screen first.

    Never guess coordinates — always call find_on_screen to get them, or
    you will click the wrong thing.

    Args:
        x: Horizontal position in screen pixels
        y: Vertical position in screen pixels
        button: "left", "right" or "middle"
        clicks: 1 for a normal click, 2 to double-click
    """
    problem = _check(x, y)
    if problem:
        return problem

    down, up = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }.get(button.lower(), (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))

    user32 = _user32()
    await _announce(x, y)
    await _glide(x, y)
    await asyncio.sleep(0.08)

    for n in range(max(1, min(3, clicks))):
        user32.mouse_event(down, 0, 0, 0, 0)
        user32.mouse_event(up, 0, 0, 0, 0)
        if n + 1 < clicks:
            await asyncio.sleep(0.06)      # inside the double-click window

    await asyncio.sleep(0.25)
    await _announce_done()
    label = {1: "Clicked", 2: "Double-clicked"}.get(clicks, f"Clicked {clicks}x")
    return f"{label} {button} at ({x}, {y})."


@tool(tier="act")
async def move_mouse(x: int, y: int) -> str:
    """Move the mouse pointer without clicking.

    Useful to reveal a hover menu or a tooltip before reading the screen.

    Args:
        x: Horizontal position in screen pixels
        y: Vertical position in screen pixels
    """
    problem = _check(x, y)
    if problem:
        return problem
    await _glide(x, y)
    return f"Pointer moved to ({x}, {y})."


@tool(tier="act")
async def scroll_at(x: int, y: int, amount: int = -3) -> str:
    """Scroll the window under a given position.

    Args:
        x: Horizontal position in screen pixels
        y: Vertical position in screen pixels
        amount: Negative scrolls down, positive scrolls up. 3 is about one
                turn of a mouse wheel.
    """
    problem = _check(x, y)
    if problem:
        return problem
    user32 = _user32()
    await _glide(x, y, steps=8)
    for _ in range(abs(amount)):
        user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0,
                           120 if amount > 0 else -120, 0)
        await asyncio.sleep(0.03)
    return f"Scrolled {'up' if amount > 0 else 'down'} at ({x}, {y})."


@tool(tier="act")
async def drag_to(from_x: int, from_y: int, to_x: int, to_y: int) -> str:
    """Drag from one point to another — to move a slider, or a clip on a timeline.

    Args:
        from_x: Where to press the button down
        from_y: Where to press the button down
        to_x: Where to release it
        to_y: Where to release it
    """
    for x, y in ((from_x, from_y), (to_x, to_y)):
        problem = _check(x, y)
        if problem:
            return problem

    user32 = _user32()
    await _glide(from_x, from_y)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    await asyncio.sleep(0.12)
    await _glide(to_x, to_y, steps=22)     # slower: apps track drags per-frame
    await asyncio.sleep(0.12)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    return f"Dragged from ({from_x}, {from_y}) to ({to_x}, {to_y})."


@tool(tier="read")
async def point_at(x: int, y: int, label: str = "") -> str:
    """Show the user where something is by moving Sid's ring over it.

    Use this to TEACH rather than to do: when the user asks "where is X" or
    "how do I do this", point at the control and tell them, instead of
    clicking it for them.

    Args:
        x: Horizontal position in screen pixels
        y: Vertical position in screen pixels
        label: What you are pointing at, e.g. "the Captions tab"
    """
    problem = _check(x, y)
    if problem:
        return problem
    await _announce(x, y)
    return (f"Pointing at ({x}, {y})"
            + (f" — {label}." if label else ".")
            + " The ring is there now; tell the user what to do next.")


@tool(tier="read")
async def stop_pointing() -> str:
    """Send Sid's ring back to the top of the screen."""
    await _announce_done()
    return "Ring back home."

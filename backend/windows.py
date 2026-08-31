"""
windows.py — finding, focusing and closing real windows on screen.

WHY THIS EXISTS AS ITS OWN FILE
-------------------------------
Two separate bugs turned out to be the same bug, and both came from asking
PowerShell instead of asking Windows.

    Get-Process | Where-Object { $_.MainWindowTitle -eq 'Sid' }

That reads plausibly and is wrong in a way that only shows up later:
**`MainWindowTitle` reports ONE window per process.** Chrome runs every one
of its windows under a single process tree with one "main" window, so the
moment you have any ordinary Chrome window open, Sid's own window becomes
invisible to that query.

The consequences were:

  * Saying "Hey Sid" repeatedly opened a NEW window every time, because the
    check for "is one already open?" always answered no.
  * Playing a second song opened a second player, because there was no way to
    find the first one and close it.

`EnumWindows` asks the window manager to walk **every top-level window**,
which is the actual question in both cases. A window either exists or it
doesn't.

> **When a convenient API and the real question don't line up, the
> convenience is what has to go.**

Everything here is Windows-only and fails soft: on another OS, or if ctypes
can't load user32, these return empty results rather than raising. A missing
window-management nicety must never take down the assistant.
"""

import ctypes
import ctypes.wintypes as wintypes

# PostMessage(WM_CLOSE) asks a window to close the same way clicking its X
# does - the app gets to run its own shutdown. TerminateProcess would be the
# blunt alternative and can lose unsaved work in a shared browser process.
WM_CLOSE = 0x0010

SW_RESTORE = 9


def _user32():
    try:
        return ctypes.windll.user32
    except Exception:
        return None


def visible_windows() -> list[tuple[int, str]]:
    """
    Every visible top-level window, as (handle, title).

    Windows with no title are skipped: they are tool windows, tray hosts and
    invisible message-only windows, never anything a person would point at.
    """
    user32 = _user32()
    if user32 is None:
        return []

    found: list[tuple[int, str]] = []

    # WINFUNCTYPE, not CFUNCTYPE: the Win32 callback convention on 32-bit
    # differs, and getting it wrong corrupts the stack rather than failing
    # cleanly.
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value.strip():
                found.append((int(hwnd), buffer.value))
        except Exception:
            pass
        return True                       # keep enumerating

    try:
        user32.EnumWindows(callback_type(callback), 0)
    except Exception:
        return []
    return found


def find_windows(title: str, exact: bool = False) -> list[int]:
    """Handles of every window whose title matches."""
    needle = title.strip().lower()
    return [
        hwnd for hwnd, text in visible_windows()
        if (text.strip().lower() == needle if exact else needle in text.lower())
    ]


def window_exists(title: str, exact: bool = False) -> bool:
    return bool(find_windows(title, exact))


def close_window(hwnd: int) -> bool:
    """Ask one window to close. Returns whether the message was delivered."""
    user32 = _user32()
    if user32 is None:
        return False
    try:
        if not user32.IsWindow(hwnd):
            return False
        return bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))
    except Exception:
        return False


def focus_window_handle(hwnd: int) -> bool:
    """
    Bring one window to the front.

    Windows deliberately makes stealing focus hard - an app that yanks your
    attention mid-typing is obnoxious - so SetForegroundWindow may decline
    and flash the taskbar button instead. That refusal is the polite
    fallback, not a failure worth reporting.
    """
    user32 = _user32()
    if user32 is None:
        return False
    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        return False

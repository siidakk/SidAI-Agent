"""
overlay.py — the glowing ring. Sid, present on screen, always.

    py overlay.py            run it (no console: pythonw overlay.py)
    py overlay.py --debug    print state changes as they happen

WHAT THIS IS
------------
A small ring that floats at the top of your screen, above every window,
and changes as Sid does: dim when idle, bright and breathing when
listening, sweeping while it thinks, amber when it wants you.

It is also how you summon Sid: **press Ctrl+Alt+Space** and it wakes,
wherever you are. No wake word needed, nothing to click.

WHY IT IS A SEPARATE PROCESS
----------------------------
Same reasoning as listener.py. The overlay must survive the server
restarting, and it must be running before the server is - otherwise the
one thing that starts Sid would need Sid to already be started.

It talks to the server the same way the web page does, over the /api/events
stream, so it needs no special channel and no polling.

THREE WINDOWS TRICKS, AND WHY EACH IS NEEDED
--------------------------------------------
1. `-transparentcolor` makes one exact colour vanish. Paint the window
   that colour and only the ring remains - there is no real per-pixel
   alpha for a tkinter window on Windows, so this is how you fake it.

2. `WS_EX_TRANSPARENT` makes the window **click-through**. Without it, an
   always-on-top window sits over your screen swallowing every click in
   its rectangle, which would make the desktop unusable. This is the flag
   that turns a blocking window into an overlay.

3. `WS_EX_NOACTIVATE` stops it stealing focus. An overlay that takes focus
   when it appears would interrupt whatever you were typing.

Miss any one of the three and it stops being an overlay and starts being
an obstacle.
"""

import argparse
import ctypes
import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend import config  # noqa: E402

# The colour that becomes invisible. Deliberately a shade nothing in the
# ring uses - if the art contained this exact value, holes would appear
# in it.
CHROMA = "#010203"

RING = 74                 # px across
PAD = 26                  # room for the glow to bleed outwards
SIZE = RING + PAD * 2

# ---------------------------------------------------------------- hotkey
# A LIST, not a constant, and that is the whole lesson here.
#
# The first version hard-coded Ctrl+Alt+Space. On the very first machine it
# ran on, something else already owned that chord, so RegisterHotKey failed
# and the feature silently did nothing. There is no way for a program to
# know in advance which combinations a particular PC has spoken for.
#
# So: try them in order, take the first that Windows grants, and say which
# one won. Set AXON_HOTKEY in .env to force a specific one.
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 1, 2, 4, 8, 0x4000

HOTKEY_CHOICES = [
    ("Ctrl+Alt+Space",   MOD_CONTROL | MOD_ALT,   0x20),
    ("Ctrl+Shift+Space", MOD_CONTROL | MOD_SHIFT, 0x20),
    ("Ctrl+Alt+J",       MOD_CONTROL | MOD_ALT,   0x4A),   # J for Jarvis
    ("Ctrl+Alt+Q",       MOD_CONTROL | MOD_ALT,   0x51),
    ("Alt+`",            MOD_ALT,                 0xC0),
    ("F9",               0,                       0x78),
]
HOTKEY_ID = 0xC0DE
HOTKEY_NAME = "(not registered)"

# Palette, matching the web UI so the two read as one product.
COLORS = {
    "idle":      ("#17A8C4", "#0E6B7E"),
    "listening": ("#3ED8F0", "#1FA8C4"),
    "thinking":  ("#7C86FF", "#4750C9"),
    "speaking":  ("#3ED8F0", "#1FA8C4"),
    "needs_you": ("#FFB454", "#C97500"),
    "error":     ("#FF6B6B", "#B33B3B"),
}


def _log(message: str) -> None:
    try:
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        with open(logs / "overlay.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
    except Exception:
        pass


# ==========================================================================
#  Talking to Sid
# ==========================================================================

def post(path: str, timeout: float = 6.0) -> bool:
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{config.PORT}{path}", data=b"", method="POST")
        urllib.request.urlopen(req, timeout=timeout).close()
        return True
    except Exception:
        return False


def server_up() -> bool:
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{config.PORT}/api/health", timeout=3).close()
        return True
    except Exception:
        return False


def start_server_if_needed() -> None:
    """Wake Sid from cold. The whole point of a hotkey is that it always works."""
    if server_up():
        return
    _log("server not running - starting it")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("sid_launcher", ROOT / "Axon.pyw")
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        launcher.start_server()
        for _ in range(60):
            if server_up():
                return
            time.sleep(0.5)
    except Exception as exc:
        _log(f"could not start server: {exc}")


# ==========================================================================
#  The ring
# ==========================================================================

class Overlay:
    def __init__(self, debug: bool = False):
        self.debug = debug
        self.state = "idle"
        self.phase = 0.0
        self.events: queue.Queue = queue.Queue()

        self.root = tk.Tk()
        self.root.overrideredirect(True)           # no title bar, no border
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", CHROMA)
        self.root.configure(bg=CHROMA)

        screen_w = self.root.winfo_screenwidth()
        self.home = ((screen_w - SIZE) // 2, 8)    # top centre
        self.root.geometry(f"{SIZE}x{SIZE}+{self.home[0]}+{self.home[1]}")

        self.canvas = tk.Canvas(self.root, width=SIZE, height=SIZE,
                                bg=CHROMA, highlightthickness=0, bd=0)
        self.canvas.pack()

        self.root.update_idletasks()
        self._make_click_through()

        self.root.after(33, self._tick)

    def _make_click_through(self) -> None:
        """
        The flag that turns a window into an overlay.

        Without WS_EX_TRANSPARENT this sits on top of your screen eating
        every click inside its box. With it, the mouse passes straight
        through as if it weren't there.
        """
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOOLWINDOW = 0x00000080        # keeps it out of Alt-Tab

        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                style | WS_EX_LAYERED | WS_EX_TRANSPARENT
                      | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
            self.hwnd = hwnd
        except Exception as exc:
            _log(f"click-through failed: {exc}")
            self.hwnd = None

    # ---------------- drawing ----------------

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        bright, deep = COLORS.get(self.state, COLORS["idle"])
        cx = cy = SIZE / 2
        t = self.phase

        # Breathing is slow when idle and quicker when Sid is engaged, so
        # the state is readable out of the corner of your eye without
        # having to look at it directly.
        speed = {"idle": 0.55, "listening": 1.9, "thinking": 1.3,
                 "speaking": 2.4, "needs_you": 2.2, "error": 2.2}[self.state]
        pulse = (math.sin(t * speed) + 1) / 2

        # Outer glow: concentric rings fading out. Cheap fake bloom -
        # tkinter has no blur, so the glow is drawn rather than filtered.
        for i in range(5, 0, -1):
            r = RING / 2 + i * 3.4 + pulse * 2.5
            shade = self._blend(deep, "#05090F", 1 - i / 6.5)
            c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=shade, width=1)

        # The ring itself
        r = RING / 2
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=deep, width=2)

        # A rotating arc — the clearest way to say "working" without text.
        if self.state in ("thinking", "listening"):
            extent = 88 if self.state == "thinking" else 52
            c.create_arc(cx - r, cy - r, cx + r, cy + r,
                         start=(-t * 150) % 360, extent=extent,
                         style=tk.ARC, outline=bright, width=3)

        # Core
        cr = RING * 0.19 + pulse * RING * 0.045
        c.create_oval(cx - cr, cy - cr, cx + cr, cy + cr, fill=bright, outline="")
        inner = cr * 0.5
        c.create_oval(cx - inner, cy - inner + 1, cx + inner, cy + inner + 1,
                      fill=self._blend(bright, "#FFFFFF", 0.55), outline="")

        # An outward ripple when Sid wants attention. Motion in the corner
        # of the eye is what actually gets noticed.
        if self.state in ("needs_you", "speaking"):
            ripple = (t * 0.9) % 1.0
            rr = RING / 2 + ripple * PAD
            fade = self._blend(bright, "#05090F", ripple)
            c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline=fade, width=2)

    @staticmethod
    def _blend(a: str, b: str, amount: float) -> str:
        amount = max(0.0, min(1.0, amount))
        ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
        br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
        return "#%02x%02x%02x" % (
            round(ar + (br - ar) * amount),
            round(ag + (bg - ag) * amount),
            round(ab + (bb - ab) * amount))

    # ---------------- the loop ----------------

    def _tick(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "state":
                self.set_state(value)
            elif kind == "point":
                self.point_at(*value)
            elif kind == "home":
                self.go_home()

        self.phase += 0.033 * math.pi * 2
        self._draw()
        self.root.after(33, self._tick)

    def set_state(self, state: str) -> None:
        if state not in COLORS or state == self.state:
            return
        self.state = state
        if self.debug:
            print(f"  state -> {state}", flush=True)

    def point_at(self, x: int, y: int) -> None:
        """Move the ring over a screen coordinate, to show you where to look."""
        self.root.geometry(f"{SIZE}x{SIZE}+{int(x - SIZE/2)}+{int(y - SIZE/2)}")

    def go_home(self) -> None:
        self.root.geometry(f"{SIZE}x{SIZE}+{self.home[0]}+{self.home[1]}")

    def run(self) -> None:
        self.root.mainloop()


# ==========================================================================
#  Background threads
# ==========================================================================

def watch_events(overlay: Overlay) -> None:
    """
    Follow Sid's state over the same event stream the web page uses.

    Reconnects forever. The server restarting must not leave a dead ring
    on screen with no way back.
    """
    import urllib.error

    while True:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{config.PORT}/api/events", timeout=None) as stream:
                overlay.events.put(("state", "idle"))
                for raw in stream:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except Exception:
                        continue

                    kind = event.get("type")
                    if kind == "wake":
                        overlay.events.put(("state", "listening"))
                    elif kind == "state":
                        overlay.events.put(("state", event.get("value", "idle")))
                    elif kind == "point":
                        overlay.events.put(("point", (event.get("x", 0), event.get("y", 0))))
                        overlay.events.put(("state", "needs_you"))
                    elif kind == "point_clear":
                        overlay.events.put(("home", None))
                        overlay.events.put(("state", "idle"))
                    elif kind == "notification":
                        overlay.events.put(
                            ("state", "needs_you" if event.get("kind") == "approval"
                                      else "speaking"))
        except Exception:
            overlay.events.put(("state", "idle"))
        time.sleep(3)


def watch_hotkey(overlay: Overlay) -> None:
    """
    A global hotkey, via the OS rather than by reading the keyboard.

    RegisterHotKey asks Windows to deliver this one combination to us. That
    matters for a program that runs all day: the alternative - a global
    keyboard hook - sees EVERY keystroke you type, including passwords.
    This sees exactly one chord and nothing else.

    **Ask for the one key you need, never for the whole keyboard.**
    """
    global HOTKEY_NAME
    user32 = ctypes.windll.user32

    wanted = os.getenv("AXON_HOTKEY", "").strip().lower()
    order = HOTKEY_CHOICES
    if wanted:
        order = ([c for c in HOTKEY_CHOICES if c[0].lower() == wanted]
                 + [c for c in HOTKEY_CHOICES if c[0].lower() != wanted])

    for name, mods, vk in order:
        if user32.RegisterHotKey(None, HOTKEY_ID, mods | MOD_NOREPEAT, vk):
            HOTKEY_NAME = name
            break
    else:
        _log("every candidate hotkey was already taken")
        if overlay.debug:
            print("  no hotkey available - all candidates taken", flush=True)
        return

    _log(f"hotkey {HOTKEY_NAME} registered")
    if overlay.debug:
        print(f"  press {HOTKEY_NAME} to wake Sid", flush=True)

    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                    ("wParam", ctypes.c_void_p), ("lParam", ctypes.c_void_p),
                    ("time", ctypes.c_uint),
                    ("pt_x", ctypes.c_long), ("pt_y", ctypes.c_long)]

    msg = MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == 0x0312:                 # WM_HOTKEY
            overlay.events.put(("state", "listening"))
            threading.Thread(target=_do_wake, daemon=True).start()


def _do_wake() -> None:
    """Start Sid if needed, then tell it to listen."""
    start_server_if_needed()
    post("/api/wake")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sid's on-screen ring")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    _log("starting")
    overlay = Overlay(debug=args.debug)

    threading.Thread(target=watch_events, args=(overlay,), daemon=True).start()
    threading.Thread(target=watch_hotkey, args=(overlay,), daemon=True).start()

    if args.debug:
        time.sleep(1.0)   # let the hotkey thread claim one first
        print(f"Sid overlay running. {HOTKEY_NAME} to wake. Ctrl+C to stop.", flush=True)
    overlay.run()


if __name__ == "__main__":
    main()

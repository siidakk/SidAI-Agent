"""
overlay.py — Sid glows in from the edges of your screen, then gets out of the way.

    py overlay.py            run it (no console: pythonw overlay.py)
    py overlay.py --debug    print state changes, and flash the glow once
    py overlay.py --demo     cycle through every state so you can see them

WHAT YOU SEE
------------
Nothing, almost always. Press the hotkey and colour blooms inward from all
four edges of the screen — pink, violet, blue — the way Apple Intelligence
lights up an iPhone. It stays while Sid is listening or working, then fades
out and disappears.

**It is invisible unless Sid is doing something.** That is the whole design
change from the first version, which parked a ring at the top of the screen
permanently and was, correctly, annoying.

WHY NOT tkinter THIS TIME
-------------------------
The ring faked softness with concentric outlines, because a tkinter window
on Windows has no real per-pixel alpha - only `-transparentcolor`, which
makes ONE exact colour vanish and leaves a hard edge everywhere else.

A glow is nothing but soft edges. Faking it with hard-edged bands looks
like what it is. So this drops tkinter and talks to Windows directly:

    UpdateLayeredWindow  →  a window whose every pixel has its own alpha

That is the API behind every glassy Windows overlay, and the only way to
put a genuinely soft gradient over your desktop.

FOUR WINDOWS, NOT ONE
---------------------
One fullscreen layered window would mean pushing 1920x1080 of RGBA to the
GPU every frame - about 8 MB per frame, which is wasteful on a laptop with
half a gigabyte free. Four thin strips along the edges cover the same
visible area with a third of the pixels, and the middle of the screen -
where there is nothing to draw - costs nothing at all.

Measured: ~11 ms to generate all four edges, against a 50 ms budget at
20fps.

THE THREE FLAGS THAT MAKE IT AN OVERLAY
---------------------------------------
  WS_EX_LAYERED      per-pixel alpha at all
  WS_EX_TRANSPARENT  clicks pass straight through
  WS_EX_NOACTIVATE   never steals focus from what you were typing in

Miss the middle one and a full-screen border becomes a full-screen wall.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import queue
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend import config, settings  # noqa: E402

# How far the glow reaches in from each edge. This is the WINDOW depth -
# the bloom has to fade to nothing before it, or the light gets cut off in
# a straight line and the window edge becomes visible.
GLOW = 104

# Frames per second. 20 is plenty for something this soft - the eye reads
# a slow colour drift, not motion.
FPS = 20

# How long the glow lingers after Sid finishes before fading away.
LINGER = 2.5
FADE = 0.45

# Fade out after this long with no news, whatever else happens.
#
# WHY THIS EXISTS: the glow is turned ON by a wake event, and was meant to
# be turned OFF by an idle event. The server never sends one - so the first
# build lit up and stayed lit forever, which is precisely the always-on
# behaviour this whole rewrite existed to remove.
#
# Never let something visible depend solely on an event arriving. Give it
# its own way to switch off.
AUTO_DISMISS = 12.0

# ---------------------------------------------------------------- hotkey
# A LIST, not a constant. The first build hard-coded Ctrl+Alt+Space; on the
# very first machine it ran on, something already owned that chord, so
# RegisterHotKey failed and the feature silently did nothing.
#
# There is no way to know in advance which combinations a given PC has
# spoken for, so: try in order, take the first Windows grants, say which.
# Set AXON_HOTKEY in .env to force one.
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 1, 2, 4, 8, 0x4000

HOTKEY_CHOICES = [
    ("Ctrl+Shift+Space", MOD_CONTROL | MOD_SHIFT, 0x20),
    ("Ctrl+Alt+Space",   MOD_CONTROL | MOD_ALT,   0x20),
    ("Ctrl+Alt+S",       MOD_CONTROL | MOD_ALT,   0x53),
    ("Ctrl+Shift+S",     MOD_CONTROL | MOD_SHIFT, 0x53),
    ("Alt+`",            MOD_ALT,                 0xC0),
    ("F9",               0,                       0x78),
]
HOTKEY_ID = 0xC0DE
HOTKEY_NAME = "(none)"

# ------------------------------------------------------- push to talk
# ONE key, held down. Not a chord, and not a toggle.
#
# Silence detection is a heuristic and it is always wrong sometimes: it cut
# you off when you paused to think, and kept recording when a fan started.
# Holding a key removes the guess - the recording begins when you press and
# ends when you let go, which is the one thing that can be known for sure.
#
# Right Ctrl by default because holding it ALONE does nothing anywhere in
# Windows, so claiming it costs you nothing. Set AXON_PTT_KEY to change it.
PTT_KEYS = {
    "rctrl": 0xA3, "right ctrl": 0xA3,
    "ralt": 0xA5, "right alt": 0xA5,
    "rshift": 0xA1, "right shift": 0xA1,
    "f9": 0x78, "f8": 0x77, "f10": 0x79,
    "scrolllock": 0x91, "pause": 0x13,
    "num0": 0x60, "numpad0": 0x60,
    "numlock": 0x90,
}
PTT_NAME = os.getenv("AXON_PTT_KEY", "rctrl").strip().lower()
PTT_VK = PTT_KEYS.get(PTT_NAME, 0xA3)

# Apple-Intelligence-ish: warm pink through violet into blue and cyan.
# Sid's own cyan is in there so the two still read as one product.
PALETTES = {
    "listening": [(255, 111, 190), (255, 126, 138), (186, 112, 255),
                  (86, 140, 255), (52, 206, 238)],
    "thinking":  [(161, 107, 255), (99, 102, 241), (59, 130, 246), (124, 134, 255)],
    "speaking":  [(34, 211, 238), (62, 216, 240), (99, 179, 237), (161, 107, 255)],
    "needs_you": [(255, 180, 84), (255, 140, 60), (255, 200, 120), (255, 160, 70)],
    "error":     [(255, 107, 107), (220, 70, 90), (255, 130, 130), (200, 60, 80)],
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
    """Wake Sid from cold - the point of a hotkey is that it always works."""
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
#  A window with real per-pixel alpha
# ==========================================================================

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# ------------------------------------------------------------------------
# DECLARE THE SIGNATURES. This is not optional on 64-bit Windows.
#
# ctypes assumes every unknown function takes and returns a C `int` - 32
# bits. Handles and LPARAMs are 64 bits. Left undeclared, HWNDs come back
# truncated and every window message raises
#
#     ctypes.ArgumentError: argument 4: OverflowError: int too long to convert
#
# which is exactly what happened: four windows were created, none of them
# worked, and the message loop threw on every single event.
LRESULT = ctypes.c_longlong

user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.UpdateLayeredWindow.argtypes = [
    wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
    wt.HDC, ctypes.POINTER(wt.POINT), wt.DWORD,
    ctypes.c_void_p, wt.DWORD]
user32.UpdateLayeredWindow.restype = wt.BOOL

gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [
    wt.HDC, ctypes.c_void_p, wt.UINT,
    ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE

WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_POPUP = 0x80000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
ULW_ALPHA = 0x02
AC_SRC_OVER, AC_SRC_ALPHA = 0x00, 0x01


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


_CLASS_REGISTERED = False
_WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, ctypes.c_uint,
                              wt.WPARAM, wt.LPARAM)
_wndproc_ref = None          # must outlive the window or Python frees it


def _register_class() -> str:
    global _CLASS_REGISTERED, _wndproc_ref
    name = "SidGlowOverlay"
    if _CLASS_REGISTERED:
        return name

    class WNDCLASS(ctypes.Structure):
        _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", _WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                    ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                    ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

    def proc(hwnd, msg, wparam, lparam):
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    _wndproc_ref = _WNDPROC(proc)
    cls = WNDCLASS()
    cls.lpfnWndProc = _wndproc_ref
    cls.hInstance = kernel32.GetModuleHandleW(None)
    cls.lpszClassName = name
    user32.RegisterClassW(ctypes.byref(cls))
    _CLASS_REGISTERED = True
    return name


class GlowStrip:
    """One edge of the glow: a layered, click-through, never-focused window."""

    def __init__(self, x: int, y: int, w: int, h: int):
        self.x, self.y, self.w, self.h = x, y, w, h
        cls = _register_class()
        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
            | WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
            cls, None, WS_POPUP, x, y, w, h,
            None, None, kernel32.GetModuleHandleW(None), None)

        # WS_EX_TOPMOST at creation is honoured inconsistently - the first
        # build showed the bottom edge over the browser and lost the top
        # one behind it. SetWindowPos(HWND_TOPMOST) is the reliable way to
        # say "stay above everything".
        HWND_TOPMOST = -1
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
        user32.SetWindowPos(self.hwnd, wt.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self.visible = False
        self._surface = None

    def show(self, on: bool) -> None:
        if on != self.visible:
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE if on else SW_HIDE)
            self.visible = on

    def _ensure_surface(self):
        """
        Allocate the drawing surface once, not once per frame.

        THE SECOND HALF OF THE FRAME BUDGET. Creating a DC and a DIB
        section every frame for every strip held the loop at ~10fps even
        after the pixel maths came down to 24ms. Windows is doing real
        allocation work each time, and none of it changes between frames.

        Allocated once, the bits are wrapped in a numpy array and written
        in place - so a frame costs one memcpy and one blit.
        """
        if getattr(self, "_surface", None) is not None:
            return self._surface

        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = self.w
        info.bmiHeader.biHeight = self.h          # bottom-up, see paint()
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0          # BI_RGB

        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(mem_dc, ctypes.byref(info), 0,
                                        ctypes.byref(bits), None, 0)
        gdi32.SelectObject(mem_dc, bitmap)

        # A numpy view straight onto the bitmap's memory. Writing here IS
        # writing what Windows will blit.
        buffer = (ctypes.c_uint8 * (self.w * self.h * 4)).from_address(bits.value)
        view = np.frombuffer(buffer, dtype=np.uint8).reshape(self.h, self.w, 4)

        self._surface = (screen_dc, mem_dc, bitmap, view)
        return self._surface

    def paint(self, rgba: np.ndarray) -> None:
        """Push one frame to the screen. Expects bottom-up premultiplied BGRA."""
        screen_dc, mem_dc, _bitmap, view = self._ensure_surface()
        # `rgba` already arrives as bottom-up premultiplied BGRA, so this is
        # a straight copy into the bitmap Windows will blit.
        view[:] = rgba

        size = wt.SIZE(self.w, self.h)
        src = wt.POINT(0, 0)
        dst = wt.POINT(self.x, self.y)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)

        user32.UpdateLayeredWindow(
            self.hwnd, screen_dc, ctypes.byref(dst), ctypes.byref(size),
            mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)

    def destroy(self) -> None:
        surface = getattr(self, "_surface", None)
        if surface is not None:
            screen_dc, mem_dc, bitmap, _view = surface
            self._surface = None          # drop the numpy view FIRST
            try:
                gdi32.DeleteObject(bitmap)
                gdi32.DeleteDC(mem_dc)
                user32.ReleaseDC(None, screen_dc)
            except Exception:
                pass
        try:
            user32.DestroyWindow(self.hwnd)
        except Exception:
            pass


class Caption:
    """
    One word at the top of the screen, and never more than one.

    The brief was "single word updates, nothing else", and that is the
    right instinct: a caption on a screen edge is glanced at, not read. A
    sentence there is something you have to stop and parse, which defeats
    the point of not opening a window.
    """

    def __init__(self, screen_w: int):
        self.w, self.h = 560, 62
        self.win = GlowStrip((screen_w - self.w) // 2, 18, self.w, self.h)
        self.text = ""
        self._font = None

    def _get_font(self):
        if self._font is None:
            from PIL import ImageFont
            for name in ("segoeuisl.ttf", "segoeui.ttf", "arial.ttf"):
                try:
                    self._font = ImageFont.truetype(name, 26)
                    break
                except Exception:
                    continue
            if self._font is None:
                self._font = ImageFont.load_default()
        return self._font

    def set(self, text: str, alpha: float = 1.0) -> None:
        from PIL import Image, ImageDraw

        text = (text or "").strip()
        if not text or alpha <= 0.02:
            self.win.show(False)
            self.text = ""
            return

        image = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        font = self._get_font()

        box = draw.textbbox((0, 0), text, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        pad_x, pad_y = 26, 13
        bw, bh = tw + pad_x * 2, th + pad_y * 2
        x0, y0 = (self.w - bw) // 2, (self.h - bh) // 2

        a = int(190 * alpha)
        draw.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=bh // 2,
                               fill=(10, 14, 22, a))
        draw.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=bh // 2,
                               outline=(150, 190, 255, int(70 * alpha)), width=1)
        draw.text((x0 + pad_x - box[0], y0 + pad_y - box[1]), text,
                  font=font, fill=(235, 242, 250, int(255 * alpha)))

        self.win.show(True)
        self.win.paint(np.asarray(image))
        self.text = text

    def destroy(self) -> None:
        self.win.destroy()


# ==========================================================================
#  Making the light
# ==========================================================================

def _ramp(palette, pos: np.ndarray) -> np.ndarray:
    """Sample a looping colour ramp at each position in `pos` (0-1)."""
    stops = np.array(palette + [palette[0]], dtype=np.float32)      # wrap round
    scaled = (pos % 1.0) * (len(stops) - 1)
    i = np.floor(scaled).astype(int)
    f = (scaled - i)[..., None]
    return stops[i] * (1 - f) + stops[np.minimum(i + 1, len(stops) - 1)] * f


# ---------------------------------------------------------------- the look
# Matched against Apple's edge glow rather than invented. Three things make
# it read as "that effect" instead of "a coloured border":
#
#   1. A ROUNDED rectangle. Square corners are the single biggest tell -
#      a phone screen has radiused corners and the light follows them.
#   2. A THIN, BRIGHT core with a soft bloom, not one wide diffuse band.
#      The first build was 210px of haze; the real thing is a few pixels of
#      near-white colour with the glow falling away from it.
#   3. Colour that flows CONTINUOUSLY round the whole outline, so a hue
#      crossing a corner carries on rather than restarting.
CORNER_RADIUS = 46        # px, close to a phone's screen radius
CORE_WIDTH = 5.5          # px, the bright line itself
BLOOM_WIDTH = 58.0        # px, how far the soft light reaches
ORBIT_SECONDS = 4.2       # one full lap of the border

# How much of the palette is visible around the outline at once.
#
# THIS HAS TO BE A WHOLE NUMBER. Position runs 0..1 round the border and
# wraps at the left edge, mid-height. At exactly 1.15 the colour at
# position 1 was 0.15 of a palette further on than the colour at position
# 0, so the two ends did not meet - a hard seam, measured as a jump from
# RGB (156,78,157) to (119,77,172) across one row. Any integer closes the
# loop; 1 puts the whole palette round the screen once.
HUE_SPREAD = 1.0


# How finely the border is divided for the colour lookup. 512 steps round
# the whole outline is far more than the eye resolves, and it turns a
# per-pixel gradient evaluation into an array index.
LUT_SIZE = 512


def _fields(x0: int, y0: int, w: int, h: int,
            screen_w: int, screen_h: int) -> dict:
    """
    Precompute everything about one strip that never changes.

    THIS IS WHERE THE FRAME BUDGET WAS WON. Computing the falloffs and the
    colour ramp per pixel per frame cost **39 ms for one strip** - 158 ms
    for all four, which would have run at 6fps and stuttered visibly.

    Everything here depends only on the screen size, so it is built once:
    the two falloff curves become static arrays, and each pixel's position
    around the border becomes an INDEX. Per frame all that is left is
    building two small lookup tables and indexing into them.
    """
    ys, xs = np.mgrid[y0:y0 + h, x0:x0 + w].astype(np.float32)
    cx, cy = screen_w / 2.0, screen_h / 2.0

    # Signed distance to a rounded rectangle: 0 exactly on the outline,
    # negative inside. This is what makes the corners round for free -
    # there is no special case for them anywhere.
    qx = np.abs(xs - cx) - (screen_w / 2.0 - CORNER_RADIUS)
    qy = np.abs(ys - cy) - (screen_h / 2.0 - CORNER_RADIUS)
    outside = np.sqrt(np.maximum(qx, 0.0) ** 2 + np.maximum(qy, 0.0) ** 2)
    inside = np.minimum(np.maximum(qx, qy), 0.0)
    near = np.abs(outside + inside - CORNER_RADIUS)

    # Where each pixel sits around the outline, as an index. The angle from
    # centre is not arc-length, but it sweeps smoothly and continuously
    # through the corners, which is the only property that matters.
    angle = np.arctan2(ys - cy, xs - cx)
    position = (angle + np.pi) / (2 * np.pi)

    core = np.exp(-(near / CORE_WIDTH) ** 2)
    bloom = np.exp(-(near / BLOOM_WIDTH) ** 1.7)

    # Flipped vertically here, once, because a DIB is bottom-up. Doing it
    # per frame cost a full array copy per strip.
    return {
        "core": core.astype(np.float32),
        "weight_flipped": np.clip(bloom * 0.55 + core * 1.6, 0.0, 1.0
                                  ).astype(np.float32)[::-1].copy(),
        "index": np.clip((position * LUT_SIZE).astype(np.int32),
                         0, LUT_SIZE - 1)[::-1].copy(),
    }


def _lookups(palette, t: float) -> np.ndarray:
    """
    The two things that DO change each frame, computed 512 times instead of
    half a million.
    """
    pos = np.linspace(0.0, 1.0, LUT_SIZE, dtype=np.float32)

    head = (t / ORBIT_SECONDS) % 1.0
    d = np.abs(pos - head)
    d = np.minimum(d, 1.0 - d)                    # distance round a loop
    travelling = np.exp(-(d / 0.30) ** 2)

    # Never fully dark anywhere: the whole outline stays lit and the
    # travelling part rides on top. Apple's does not go black on three
    # sides while it is busy on the fourth.
    #
    # The bump is WIDE and SHALLOW on purpose. A narrow bright head reads
    # as a comet doing laps; Apple's reads as the whole border breathing,
    # with the emphasis drifting round it. Widening 0.17 -> 0.30 and
    # dropping the contrast is the entire difference between those two.
    along = (0.74 + 0.26 * travelling).astype(np.float32)

    colours = _ramp(palette, pos * HUE_SPREAD + t * 0.05).astype(np.float32)

    # Colour and brightness in ONE table, computed for 512 entries rather
    # than half a million pixels.
    #
    # Built as BGRA and ALREADY PREMULTIPLIED, which is exactly the byte
    # layout Windows wants. Doing the channel swap and the premultiply here,
    # on 512 rows, means the per-frame path is one multiply and one memcpy
    # instead of four channel operations over 600k pixels.
    #
    # Premultiply exactly ONCE. An earlier version did it here AND in
    # paint(), which squares the alpha and renders the whole rim far too
    # dark.
    a = np.clip(along, 0.0, 1.0)
    table = np.empty((LUT_SIZE, 4), dtype=np.float32)
    table[:, 0] = colours[:, 2] * a          # B
    table[:, 1] = colours[:, 1] * a          # G
    table[:, 2] = colours[:, 0] * a          # R
    table[:, 3] = a * 255.0                  # A
    return table


def strip_rgba(field: dict, table: np.ndarray, intensity: float) -> np.ndarray:
    """
    One strip of the rim light. Two array lookups and one multiply.

    `table` is a LUT_SIZE x 4 table of premultiplied BGRA-ready values built
    once per frame. Everything that varies with time lives in there, so the
    per-pixel work here is as close to nothing as numpy allows - which is
    the difference between 20fps and a slideshow.
    """
    out = table[field["index"]] * field["weight_flipped"][..., None] * intensity
    np.clip(out, 0.0, 255.0, out=out)
    return out.astype(np.uint8)


# ==========================================================================
#  The overlay
# ==========================================================================

class Glow:
    def __init__(self, debug: bool = False):
        self.debug = debug
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
        self.sw = user32.GetSystemMetrics(0)
        self.sh = user32.GetSystemMetrics(1)

        self.state = "hidden"
        self.intensity = 0.0        # what is actually drawn
        self.target = 0.0           # what it is heading towards
        self.hide_at = 0.0
        self.phase = 0.0
        self.last_news = 0.0
        self.events: queue.Queue = queue.Queue()

        # Thin, because the light is a rim rather than a haze. The top and
        # bottom strips run the FULL width so each of them contains two
        # whole rounded corners; the sides then only cover the straight
        # middle. Without that the corners would be split across two
        # windows and the arc would tear.
        g = min(GLOW, self.sh // 4)
        self.depth = g

        self.top = GlowStrip(0, 0, self.sw, g)
        self.bottom = GlowStrip(0, self.sh - g, self.sw, g)
        self.left = GlowStrip(0, g, g, self.sh - 2 * g)
        self.right = GlowStrip(self.sw - g, g, g, self.sh - 2 * g)
        self.strips = (self.top, self.bottom, self.left, self.right)

        # The distance field never changes while the screen size doesn't,
        # so build it once. That turns 15ms of maths per frame into a
        # one-off cost at startup.
        self.fields = {
            self.top:    _fields(0, 0, self.sw, g, self.sw, self.sh),
            self.bottom: _fields(0, self.sh - g, self.sw, g, self.sw, self.sh),
            self.left:   _fields(0, g, g, self.sh - 2 * g, self.sw, self.sh),
            self.right:  _fields(self.sw - g, g, g, self.sh - 2 * g,
                                 self.sw, self.sh),
        }
        self.caption = Caption(self.sw)
        self.word = ""          # the single word shown, if any

    # ---------------- state ----------------

    def wake(self, state: str = "listening") -> None:
        self.state = state
        self.target = 1.0
        self.hide_at = 0.0
        self.last_news = time.time()
        if self.debug:
            print(f"  glow on  ({state})", flush=True)

    def say(self, word: str) -> None:
        """Show one word. Empty hides the caption."""
        self.word = word or ""
        self.last_news = time.time()
        if self.debug and word:
            print(f"  caption: {word}", flush=True)

    def set_state(self, state: str) -> None:
        if state in PALETTES:
            if self.state != state and self.debug:
                print(f"  state -> {state}", flush=True)
            self.state = state
            self.target = 1.0
            self.hide_at = 0.0
            self.last_news = time.time()

    def dismiss(self) -> None:
        """Let it linger a moment, then fade. Snapping off looks like a crash."""
        if self.target > 0 and not self.hide_at:
            self.hide_at = time.time() + LINGER
            if self.debug:
                print("  glow fading out", flush=True)

    # ---------------- frame ----------------

    def step(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "wake":
                self.wake()
            elif kind == "word":
                self.say(value)
            elif kind == "state":
                self.set_state(value)
            elif kind == "dismiss":
                self.dismiss()

        # Nothing heard for a while? Put itself away. This is the backstop
        # that stops a missed "done" event leaving the screen lit all day.
        if (self.target > 0 and not self.hide_at and self.last_news
                and time.time() - self.last_news > AUTO_DISMISS):
            if self.debug:
                print(f"  nothing for {AUTO_DISMISS:.0f}s - fading out", flush=True)
            self.dismiss()

        if self.hide_at and time.time() >= self.hide_at:
            self.target = 0.0

        # ease towards the target so it blooms rather than blinks
        rate = (1.0 / FPS) / FADE
        if self.intensity < self.target:
            self.intensity = min(self.target, self.intensity + rate)
        elif self.intensity > self.target:
            self.intensity = max(self.target, self.intensity - rate)

        if self.intensity <= 0.001:
            for s in self.strips:
                s.show(False)
            self.caption.set("")
            self.state = "hidden"
            self.hide_at = 0.0
            return

        self.phase += 1.0 / FPS
        palette = PALETTES.get(self.state, PALETTES["listening"])

        # The caption fades with the glow, so they arrive and leave together
        # rather than the word hanging on over a dark screen.
        if self.caption.text != self.word or self.word:
            self.caption.set(self.word, self.intensity)
        # ease-in-out so the bloom itself feels lit rather than wiped on
        eased = self.intensity * self.intensity * (3 - 2 * self.intensity)

        # ONE coordinate system for all four edges: walk the perimeter
        # clockwise from the top-left corner. Each strip gets the slice of
        # it that belongs to that edge, so light crossing a corner carries
        # straight on instead of jumping.
        #
        #   top    left -> right      0            .. W
        #   right  top  -> bottom     W            .. W+H
        #   bottom right-> left       W+H          .. 2W+H
        #   left   bottom-> top       2W+H         .. 2W+2H
        # Built once per FRAME, not once per strip - the tables are the
        # same for all four, and they are what makes the light continuous
        # round the corners.
        table = _lookups(palette, self.phase)

        for strip in self.strips:
            strip.show(True)
            strip.paint(strip_rgba(self.fields[strip], table, eased))


# ==========================================================================
#  Background: follow Sid, and listen for the hotkey
# ==========================================================================

def watch_events(glow: Glow) -> None:
    """Follow Sid over the same stream the web page uses. Reconnects forever."""
    while True:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{config.PORT}/api/events", timeout=None) as stream:
                for raw in stream:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except Exception:
                        continue

                    kind = event.get("type")
                    if kind == "glow":
                        # Another process (the wake-word listener) driving
                        # the caption, so both doors into Sid look the same.
                        word = event.get("word", "")
                        glow.events.put(("wake", None))
                        glow.events.put(("word", word))
                        glow.events.put((
                            "state",
                            {"listening": "listening", "thinking": "thinking",
                             "working": "thinking", "speaking": "speaking"}
                            .get(word, "error")))
                        if word in ("done", "nothing", "failed", ""):
                            glow.events.put(("dismiss", None))
                    elif kind == "wake":
                        glow.events.put(("wake", None))
                    elif kind == "state":
                        value = event.get("value", "idle")
                        if value in ("idle", "done"):
                            glow.events.put(("dismiss", None))
                        else:
                            glow.events.put(("state", value))
                    elif kind == "point":
                        glow.events.put(("state", "needs_you"))
                    elif kind == "point_clear":
                        glow.events.put(("dismiss", None))
                    elif kind == "notification":
                        glow.events.put((
                            "state",
                            "needs_you" if event.get("kind") == "approval" else "speaking"))
                        glow.events.put(("dismiss", None))
        except Exception:
            pass
        time.sleep(3)


def claim_hotkey(debug: bool = False) -> bool:
    global HOTKEY_NAME
    wanted = os.getenv("AXON_HOTKEY", "").strip().lower()
    order = HOTKEY_CHOICES
    if wanted:
        order = ([c for c in HOTKEY_CHOICES if c[0].lower() == wanted]
                 + [c for c in HOTKEY_CHOICES if c[0].lower() != wanted])

    for name, mods, vk in order:
        if user32.RegisterHotKey(None, HOTKEY_ID, mods | MOD_NOREPEAT, vk):
            HOTKEY_NAME = name
            _log(f"hotkey {name} registered")
            if debug:
                print(f"  press {name} to summon Sid", flush=True)
            return True

    _log("every candidate hotkey was already taken")
    if debug:
        print("  no hotkey available - all candidates taken", flush=True)
    return False


def _held() -> bool:
    """
    Is the push-to-talk key down right now?

    GetAsyncKeyState is asked about ONE key and nothing else. That is the
    same principle as using RegisterHotKey rather than a keyboard hook: a
    program that runs all day should never be in a position to see what you
    are typing, even accidentally.
    """
    return bool(user32.GetAsyncKeyState(PTT_VK) & 0x8000)


def handsfree() -> bool:
    """
    Should voice answer with just the glow, or open the app?

    Read fresh on every press, never cached. It is one small file read, and
    a toggle that needs a restart to take effect is a toggle people stop
    trusting.
    """
    try:
        return bool(settings.get("handsfree", False))
    except Exception:
        return False


def ptt_on() -> bool:
    """Is the keyboard shortcut switched on at all?"""
    try:
        return bool(settings.get("ptt_enabled", True))
    except Exception:
        return True


def _open_the_app() -> None:
    """Open Sid's window, ready to listen - the same thing "Hey Sid" does."""
    start_server_if_needed()
    if window_is_open():
        post("/api/wake")                 # already there: just start listening
        focus_window()
        return
    from backend.tools.web import open_app_window
    open_app_window(f"http://127.0.0.1:{config.PORT}/?listen=1")


def window_is_open() -> bool:
    from backend import windows
    return windows.window_exists("Sid", exact=True)


def focus_window() -> None:
    from backend import windows
    for hwnd in windows.find_windows("Sid", exact=True):
        windows.focus_window_handle(hwnd)
        return


def _do_turn(glow) -> None:
    """
    A whole spoken exchange, driven by the key being held.

    The hotkey used to only POST /api/wake, whose job is telling an
    ALREADY-OPEN Sid page to switch its microphone on. With no page open -
    the normal case - the glow lit up and nothing else happened, because
    the ears lived in the browser.
    """
    start_server_if_needed()
    post("/api/wake")                     # harmless if no page is listening

    try:
        import voice_session
    except Exception as exc:
        _log(f"voice unavailable: {exc}")
        glow.events.put(("word", "no voice"))
        glow.events.put(("dismiss", None))
        return

    def state(word):
        glow.events.put(("word", word))
        # A quoted string is the transcript, not a state name - show the
        # words but keep the colour where it was, or every transcript
        # would flash the error palette.
        if not word.startswith('"'):
            glow.events.put(("state",
                             {"listening": "listening", "thinking": "thinking",
                              "working": "thinking", "speaking": "speaking"}
                             .get(word, "error")))

    try:
        result = voice_session.run_turn_held(_held, on_state=state)
    except Exception as exc:
        _log(f"voice turn failed: {exc}")
        glow.events.put(("word", "failed"))
        glow.events.put(("dismiss", None))
        return

    if result.get("ok"):
        _log(f"heard: {result.get('heard','')[:80]}")
        glow.events.put(("word", "done"))
    else:
        _log(f"turn ended: {result.get('why','')}")
        glow.events.put(("word", "nothing"))

    glow.events.put(("dismiss", None))


_MUTEX = None


def already_running() -> bool:
    """
    Refuse to be the second copy.

    Six instances stacked up during development - each start-up added one,
    they drew over each other, and only the first could hold the hotkey so
    the rest looked broken. A named mutex is the standard Windows answer:
    unique per session, and released by the OS when the process dies, so a
    crash cannot leave a stale lock behind the way a lock-file would.

    The subtlety that cost a try: GetLastError is per-thread and is
    overwritten by the NEXT api call, so it has to be read through a DLL
    opened with use_last_error=True and checked immediately.
    """
    global _MUTEX
    ERROR_ALREADY_EXISTS = 183

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
    k32.CreateMutexW.restype = wt.HANDLE

    handle = k32.CreateMutexW(None, False, "Global\\SidGlowOverlay")
    err = ctypes.get_last_error()          # read it NOW, before anything else
    if not handle:
        return False

    _MUTEX = handle                        # held for the life of the process
    return err == ERROR_ALREADY_EXISTS


_turn_running = threading.Event()


def _restart_after_cancel(glow) -> None:
    """Wait for the interrupted turn to let go, then start a fresh one."""
    for _ in range(30):                       # up to ~3s
        if not _turn_running.is_set():
            break
        time.sleep(0.1)
    if _held():                               # still holding? they meant it
        _run_turn_guarded(glow)
    else:
        glow.events.put(("dismiss", None))


def _run_turn_guarded(glow) -> None:
    _turn_running.set()
    try:
        _do_turn(glow)
    finally:
        _turn_running.clear()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sid's screen glow")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--demo", action="store_true",
                        help="cycle every state once, then exit")
    args = parser.parse_args()

    # --demo is allowed alongside a running copy; it is a one-off preview.
    if not args.demo and already_running():
        _log("another copy is already running - exiting")
        if args.debug:
            print("Sid's glow is already running.", flush=True)
        return

    _log(f"starting - push to talk on {PTT_NAME} (vk {PTT_VK:#x})")
    glow = Glow(debug=args.debug or args.demo)

    if args.demo:
        for state in ("listening", "thinking", "speaking", "needs_you", "error"):
            glow.set_state(state)
            for _ in range(int(FPS * 2.2)):
                glow.step()
                time.sleep(1.0 / FPS)
        glow.dismiss()
        for _ in range(int(FPS * (LINGER + FADE + 0.5))):
            glow.step()
            time.sleep(1.0 / FPS)
        for s in glow.strips:
            s.destroy()
        return

    claim_hotkey(args.debug)
    threading.Thread(target=watch_events, args=(glow,), daemon=True).start()

    if args.debug:
        print(f"Sid ready. Hold {PTT_NAME.upper()} to talk "
              f"(or press {HOTKEY_NAME}). Ctrl+C to stop.", flush=True)
        glow.wake()
        glow.dismiss()

    # One thread does both: pump Windows messages (that is how the hotkey
    # arrives) and render. Keeping them together avoids touching a window
    # from a thread that does not own it.
    msg = wt.MSG()
    frame_time = 1.0 / FPS
    was_down = False

    while True:
        # The old chord still works, for anyone who prefers a toggle.
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            if msg.message == 0x0312 and not _turn_running.is_set() and ptt_on():
                if handsfree():
                    glow.wake()
                    threading.Thread(target=_run_turn_guarded,
                                     args=(glow,), daemon=True).start()
                else:
                    threading.Thread(target=_open_the_app, daemon=True).start()
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # PUSH TO TALK. Polled rather than hooked: RegisterHotKey only ever
        # reports the press, and this needs the release too - that is what
        # ends the recording.
        down = _held()

        # PRESSING THE KEY MEANS "STOP AND LISTEN TO ME".
        #
        # It used to be ignored while a turn was running, so if Sid was
        # halfway through a long spoken answer you had to sit and wait for
        # it to finish before you could say anything. That is not how
        # talking to someone works - you interrupt, and they stop.
        if down and not was_down and _turn_running.is_set() and ptt_on():
            try:
                import voice_session
                if voice_session.cancel():
                    _log("interrupted mid-answer")
            except Exception:
                pass
            glow.events.put(("word", "listening"))
            glow.events.put(("state", "listening"))
            # The old thread notices it has been superseded and exits; give
            # it a moment to let go of the microphone before reopening it.
            threading.Thread(target=_restart_after_cancel, args=(glow,),
                             daemon=True).start()
            was_down = down
            glow.step()
            time.sleep(frame_time)
            continue

        if down and not was_down and not _turn_running.is_set() and ptt_on():
            # ONE SETTING, BOTH DOORS.
            #
            # "hands-free" governed only the wake word, so turning it off
            # stopped "Hey Sid" opening a window but left the key glowing
            # away regardless - a switch that controls half of the thing it
            # is named after. If it says hands-free mode, it has to mean it
            # for every way in.
            if handsfree():
                glow.wake()
                glow.say("listening")
                threading.Thread(target=_run_turn_guarded,
                                 args=(glow,), daemon=True).start()
            else:
                threading.Thread(target=_open_the_app, daemon=True).start()
        was_down = down

        # Sleep the REMAINDER of the frame, not a whole one on top of the
        # work. `sleep(frame_time)` after doing 23ms of rendering gives
        # 73ms frames - 13fps while claiming 20 - and it hides every
        # optimisation you make, because the fixed sleep dominates.
        started = time.perf_counter()
        glow.step()
        time.sleep(max(0.0, frame_time - (time.perf_counter() - started)))


if __name__ == "__main__":
    main()

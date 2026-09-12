"""
screen.py — Sid's eyes.

Capturing the screen is the easy half. The parts worth thinking about are
how much of it to send, and the fact that sending it anywhere at all is a
bigger decision than it looks.

⚠️ THE PRIVACY LINE THIS CROSSES
--------------------------------
Every other tool in this project reads one specific thing: a file you
named, a page you asked for, your own calendar. This one reads
**everything currently on your screen** - whatever window happens to be
open, whatever is in it - and sends it to a model.

That is a genuine change in what Sid is, and it deserves to be stated
rather than buried:

  * If the provider is **Gemini or Claude**, your screen leaves the
    laptop. A password manager, a private message, a bank page - if it is
    visible, it is in the image.
  * If the provider is **Ollama**, nothing leaves the machine, but the
    model is far weaker at reading small UI text.

Sid never captures on its own. Only a tool call captures, only when a
request needed it, and every capture lands in the audit log like any
other action. The honest summary: **treat "look at my screen" the way you
would treat screen-sharing with a stranger**, because mechanically that
is what it is.

WHY IT IS DOWNSCALED
--------------------
A raw 1920x1080 PNG is ~130 KB and costs real tokens. Scaled to 1280 wide
and saved as JPEG it is ~65 KB and reads identically to a model - UI text
survives comfortably at that size. Going smaller starts losing menu
labels, which is exactly the text that matters.
"""

import base64
import io
import time

# Scaled so that ordinary UI text is still legible. Measured: menu bars and
# button labels survive 1280; at 800 they start to smear.
MAX_WIDTH = 1280
JPEG_QUALITY = 72


def capture(region: tuple[int, int, int, int] | None = None) -> tuple[bytes, int, int, float]:
    """
    Grab the screen.

    Returns (jpeg_bytes, full_width, full_height, scale) where `scale` maps
    image coordinates back to real screen pixels. Anything that turns a
    model's answer into a click needs that number, so it travels with the
    image rather than being recomputed and getting out of step.
    """
    from PIL import ImageGrab

    shot = ImageGrab.grab(bbox=region)
    full_w, full_h = shot.size

    image = shot.convert("RGB")
    scale = 1.0
    if full_w > MAX_WIDTH:
        scale = MAX_WIDTH / full_w
        image = image.resize((MAX_WIDTH, round(full_h * scale)))

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue(), full_w, full_h, scale


def as_base64(jpeg: bytes) -> str:
    return base64.b64encode(jpeg).decode()


def screen_size() -> tuple[int, int]:
    import ctypes

    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)

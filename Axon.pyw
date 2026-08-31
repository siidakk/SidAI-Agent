"""
Axon.pyw — double-click this and Sid is running. No terminal, no commands.

WHY THE .pyw EXTENSION?
On Windows, .py files open with python.exe, which always creates a black
console window. .pyw files open with pythonw.exe, which doesn't. Same Python,
no window. That one letter is the whole difference between "a script" and
"an app".

WHAT IT DOES
  1. Is the server already running?  If yes, skip to step 3.
  2. Start uvicorn as a hidden background process.
  3. Wait until it actually answers (not just "we launched it").
  4. Open Sid in a Chrome app window - no tabs, no address bar, own icon.

Run it again later and it reuses the running server instead of starting a
second one. That means the desktop shortcut is safe to double-click twice.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend import config  # noqa: E402  (needs ROOT on the path first)

URL = f"http://127.0.0.1:{config.PORT}"
HEALTH = f"{URL}/api/health"

# Hides the console window of anything we spawn. Windows-only flag.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def port_is_open() -> bool:
    """Cheap check: is anything listening on our port at all?"""
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", config.PORT)) == 0


def server_responds() -> bool:
    """
    Stronger check: is it OUR server, and is it actually answering?

    Worth separating from port_is_open. A port can be held by a dying process,
    or by a completely different app. "Something is listening" is not the same
    as "Sid is ready" — and starting the browser too early shows an error page.
    """
    try:
        with urllib.request.urlopen(HEALTH, timeout=1.5) as response:
            return b'"ok"' in response.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def start_server() -> None:
    """Launch uvicorn detached, with no visible window."""
    # pythonw.exe instead of python.exe, for the same no-console reason.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = str(pythonw) if pythonw.exists() else sys.executable

    subprocess.Popen(
        [
            interpreter, "-m", "uvicorn", "backend.main:app",
            "--host", config.HOST,          # 0.0.0.0 so your phone can reach it
            "--port", str(config.PORT),
            # Don't wait forever for open connections on shutdown.
            #
            # /api/events is a stream that stays open for as long as the page
            # is open - by design. But "graceful shutdown" means "wait for
            # open connections to close", and that one never does, so with a
            # single tab open the server could not be stopped at all. It sat
            # on "Waiting for connections to close" indefinitely.
            #
            # Any server with long-lived connections needs this bound.
            "--timeout-graceful-shutdown", "3",
        ],
        cwd=str(ROOT),
        creationflags=NO_WINDOW,
        # Detach from this launcher so the server outlives it.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def open_window() -> None:
    """Open Sid as its own window rather than a browser tab."""
    from backend.tools.web import open_app_window
    open_app_window(URL)


def main() -> None:
    if not server_responds():
        if not port_is_open():
            start_server()

        # WAIT FOR READY, don't just sleep a fixed amount. A cold start with
        # Ollama can take a few seconds; a warm one is instant. Polling until
        # it answers handles both without guessing.
        for _ in range(60):              # up to ~30 seconds
            if server_responds():
                break
            time.sleep(0.5)
        else:
            # Still nothing. Show a real error instead of failing silently —
            # a launcher that does nothing is maddening to debug.
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                f"Sid's server didn't start.\n\n"
                f"Open a terminal in {ROOT} and run:\n"
                f"  py -m uvicorn backend.main:app --port {config.PORT}\n\n"
                f"to see the actual error.",
                "Sid", 0x10,
            )
            return

    open_window()


if __name__ == "__main__":
    main()

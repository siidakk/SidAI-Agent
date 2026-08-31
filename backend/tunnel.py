"""
tunnel.py — an HTTPS address for Sid, so the phone gets a working microphone.

WHY A TUNNEL IS NEEDED AT ALL
-----------------------------
Browsers refuse microphone access unless the page is a "secure context":
https:// or localhost. Your laptop at localhost qualifies. Your phone at
http://172.17.x.x does not — and it fails *silently*, which is the worst
kind of failure.

We can't easily give the laptop a real certificate for its LAN address, so
instead ngrok gives us a public https:// address that forwards to the local
server. Real certificate, works from anywhere, no browser warnings.

⚠️ THIS PUTS SID ON THE PUBLIC INTERNET
That is not a small thing. Sid reads your Gmail. The only reason it's
acceptable is `auth.py`: every request without the access key gets a 401,
verified from the public tunnel URL. The key travels inside the QR code,
which is exactly why the pairing endpoint is localhost-only.

Without that lock this feature would be reckless. With it, the tunnel is
just a door with a password on it.

WHY THE SERVER MANAGES IT
-------------------------
`py mobile.py --tunnel` already did this, but it meant opening a terminal
and leaving it running. Doing it here means one tap on the phone icon.

The cost is that the server now owns a child process, and child processes
need looking after: don't start two, notice if it dies, and shut it down
when the server does. That's most of what this file is.
"""

import asyncio
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

from . import config

# ngrok exposes a local API describing its own tunnels. Reading the URL from
# there is far more reliable than scraping its console output.
NGROK_API = "http://127.0.0.1:4040/api/tunnels"

# The child process we started, if any. Held so we can stop it later and so
# we never start a second one.
_process: subprocess.Popen | None = None


def find_ngrok() -> str | None:
    """Locate ngrok, whether it's on PATH or just sitting in a folder."""
    found = shutil.which("ngrok")
    if found:
        return found

    for candidate in (
        Path.home() / "Desktop" / "ngrok" / "ngrok.exe",
        Path.home() / "ngrok.exe",
        config.ROOT / "ngrok.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return None


def current_url() -> str | None:
    """
    The https URL of a running tunnel, or None.

    Asks ngrok itself rather than trusting our own bookkeeping — the tunnel
    may have been started by `py mobile.py --tunnel` in another terminal, or
    ours may have died. The same "believe the server, not your own notes"
    reasoning as the Google 401 retry.
    """
    try:
        with urllib.request.urlopen(NGROK_API, timeout=2) as response:
            for tunnel in json.load(response).get("tunnels", []):
                url = tunnel.get("public_url", "")
                if url.startswith("https://"):
                    return url
    except Exception:
        pass
    return None


def status() -> dict:
    url = current_url()
    return {
        "running": bool(url),
        "url": url,
        "available": find_ngrok() is not None,
    }


async def start(timeout: float = 25.0) -> dict:
    """
    Start a tunnel and wait for its URL. Safe to call when one already runs.

    Returns {"url": ...} or {"error": ...} — never raises, because this is
    called from a UI button and an exception there is just a blank panel.
    """
    global _process

    existing = current_url()
    if existing:
        return {"url": existing, "reused": True}

    exe = find_ngrok()
    if not exe:
        return {"error": (
            "ngrok isn't installed. Install it with:\n"
            "  winget install ngrok.ngrok\n"
            "then sign up (free) and run:\n"
            "  ngrok config add-authtoken <your token>"
        )}

    try:
        _process = subprocess.Popen(
            [exe, "http", str(config.PORT), "--log", "stdout"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return {"error": f"Could not start ngrok: {exc}"}

    # Poll for the URL. ngrok takes a few seconds to establish its session,
    # and there's no callback to wait on — the API simply starts answering.
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.6)

        url = current_url()
        if url:
            return {"url": url, "reused": False}

        # If the process died, stop waiting the full timeout for something
        # that is never going to appear.
        if _process.poll() is not None:
            break

    stop()
    return {"error": (
        "The tunnel didn't come up. The usual cause is a missing authtoken:\n"
        "  ngrok config add-authtoken <token from dashboard.ngrok.com>"
    )}


def stop() -> bool:
    """Shut down the tunnel we started. Leaves other people's alone."""
    global _process

    if _process is None:
        return False

    try:
        _process.terminate()
        _process.wait(timeout=5)
    except Exception:
        try:
            _process.kill()
        except Exception:
            pass

    _process = None
    return True

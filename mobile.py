"""
mobile.py — put Sid on your phone. Scan one QR code, done.

    py mobile.py           Wi-Fi only. Fast, private, no accounts.
    py mobile.py --tunnel  HTTPS from anywhere. Needed for voice on phones.

WHY THERE ARE TWO MODES
-----------------------
Browsers refuse microphone access unless the page is a "secure context" —
https:// or localhost. Your laptop at localhost qualifies. Your phone at
http://172.17.x.x does not, and it fails **silently**: the mic button simply
never works and nothing explains why.

So:

  Wi-Fi mode (http)    everything works EXCEPT the microphone.
                       Same network only. Nothing leaves your house.

  Tunnel mode (https)  everything works, including voice, from anywhere —
                       mobile data, college Wi-Fi, another city.
                       Traffic is routed through ngrok's servers.

Wi-Fi mode is the private one. Tunnel mode is the capable one. Pick per
occasion; that's why both exist.

⚠️ THE KEY IN THE QR CODE
The QR encodes your access key. Anyone who scans it gets into your Sid —
which now reads your Gmail. Don't put it in a screenshot, a group chat, or a
slide. Treat it exactly like a password, because it is one.
"""

import argparse
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend import auth, config  # noqa: E402

GREEN, YELLOW, RED, DIM, BOLD, OFF = (
    "\033[32m", "\033[33m", "\033[31m", "\033[90m", "\033[1m", "\033[0m"
)


def lan_ip() -> str:
    """
    Find the address this machine has on the local network.

    The trick: open a UDP socket "towards" a public address and ask which
    local interface the OS picked. Nothing is actually sent — UDP is
    connectionless — but the routing table gets consulted, which is what we
    want. Far more reliable than gethostbyname(), which often returns
    127.0.0.1 or a stale VPN address.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    finally:
        sock.close()


def server_running() -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{config.PORT}/api/health", timeout=2
        ) as r:
            return r.status == 200
    except Exception:
        return False


def start_server() -> bool:
    """Start Sid in the background if it isn't already up."""
    if server_running():
        return True

    print(f"{DIM}Starting Sid...{OFF}")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen(
        [str(pythonw if pythonw.exists() else sys.executable),
         "-m", "uvicorn", "backend.main:app",
         "--host", "0.0.0.0", "--port", str(config.PORT)],
        cwd=str(ROOT),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        if server_running():
            return True
        time.sleep(0.5)
    return False


def find_ngrok() -> str | None:
    import shutil

    found = shutil.which("ngrok")
    if found:
        return found
    for candidate in (
        Path.home() / "Desktop" / "ngrok" / "ngrok.exe",
        Path.home() / "ngrok.exe",
        ROOT / "ngrok.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return None


def start_tunnel() -> str | None:
    """
    Open an HTTPS tunnel to the local server and return its public URL.

    ngrok exposes its own little API on 127.0.0.1:4040, which is how we read
    back the generated URL instead of scraping its console output.
    """
    exe = find_ngrok()
    if not exe:
        print(f"{RED}ngrok not found.{OFF}")
        print("  Install it with:  winget install ngrok.ngrok")
        print("  Then sign up (free) and run:  ngrok config add-authtoken <token>")
        return None

    print(f"{DIM}Opening HTTPS tunnel...{OFF}")
    subprocess.Popen(
        [exe, "http", str(config.PORT), "--log", "stdout"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    for _ in range(30):
        time.sleep(1)
        try:
            with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
                import json
                for t in json.load(r).get("tunnels", []):
                    if t.get("public_url", "").startswith("https://"):
                        return t["public_url"]
        except Exception:
            continue

    print(f"{RED}Tunnel didn't come up.{OFF}")
    print("  Most likely you haven't added an authtoken yet:")
    print("  ngrok config add-authtoken <token from dashboard.ngrok.com>")
    return None


def show_qr(url: str) -> None:
    """Print a QR code as text. Phone cameras read it straight off the screen."""
    try:
        import segno
    except ImportError:
        print(f"{DIM}(no QR - run: py -m pip install segno){OFF}")
        return

    print()
    # border=1 keeps it small enough to fit a terminal; most scanners cope.
    segno.make(url, error="m").terminal(compact=True, border=2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tunnel", action="store_true",
                        help="HTTPS from anywhere (needed for voice on phones)")
    args = parser.parse_args()

    key = auth.ensure_key()

    if not start_server():
        print(f"{RED}Sid's server didn't start.{OFF} Run it manually to see the error:")
        print(f"  py -m uvicorn backend.main:app --port {config.PORT}")
        return

    if args.tunnel:
        base = start_tunnel()
        if not base:
            return
        secure = True
    else:
        base = f"http://{lan_ip()}:{config.PORT}"
        secure = False

    url = f"{base}/?key={key}"

    print()
    print(f"{BOLD}Scan this with your phone's camera{OFF}")
    show_qr(url)
    print(f"{DIM}or open:{OFF} {url}")
    print()

    if secure:
        print(f"{GREEN}HTTPS{OFF} - works from anywhere, and the microphone will work.")
        print(f"{DIM}Traffic routes through ngrok's servers. Keep this window open;{OFF}")
        print(f"{DIM}closing it takes the tunnel down.{OFF}")
    else:
        print(f"{YELLOW}Wi-Fi only{OFF} - phone must be on the same network as this laptop.")
        print(f"{DIM}The microphone will NOT work: browsers require https for mic{OFF}")
        print(f"{DIM}access. Everything else does. For voice: py mobile.py --tunnel{OFF}")

    print()
    print(f"{BOLD}Then add it to your home screen:{OFF}")
    print(f"  {DIM}Android (Chrome){OFF}  menu (three dots) -> Add to Home screen")
    print(f"  {DIM}iPhone (Safari){OFF}   Share button -> Add to Home Screen")
    print(f"  {DIM}                       must be Safari - Chrome on iOS cannot install PWAs{OFF}")
    print()
    print(f"{RED}The QR contains your password.{OFF} "
          f"{DIM}Anyone who scans it can read your email through Sid.{OFF}")
    print()

    if secure:
        try:
            print(f"{DIM}Ctrl+C to close the tunnel.{OFF}")
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nTunnel closed.")


if __name__ == "__main__":
    if os.name == "nt":
        os.system("")
    main()

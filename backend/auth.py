"""
auth.py — the lock on the front door.

WHY THIS EXISTS, AND WHY IT'S URGENT
------------------------------------
Sid binds to 0.0.0.0 so your phone can reach it. Until this file existed,
that meant **anyone on the same Wi-Fi could open it and read your Gmail.**
Not theoretically — a plain GET to /api/connections returned your address,
and a POST to /api/chat would happily search your inbox for them.

On your home network that's bad. On campus or hostel Wi-Fi it's an open door.

THE MODEL
---------
One shared secret, generated once and stored in .env.

  - Requests from **localhost** are trusted with no key. If someone is
    already running code on your laptop, a password in a file on that same
    laptop protects nothing.
  - Requests from **anywhere else** must present the key: either as a cookie
    (set automatically on first visit) or as ?key=... in the URL.

Visit `http://<laptop>:8321/?key=SECRET` once on your phone. The cookie is
stored for a year, so you never type it again — which is what makes the
QR code in mobile.py work as a one-scan setup.

WHAT THIS IS AND ISN'T
----------------------
It is a **bearer token**: whoever holds it gets in. That's the same model as
an API key or a session cookie, and it's the right level for a personal tool
on your own network.

It is NOT protection against someone who can read your traffic. Over plain
http:// the key crosses the network in the clear, so anyone sniffing the
Wi-Fi could copy it. That is exactly why mobile.py pushes you towards an
HTTPS tunnel — encryption isn't a nice-to-have once a password is involved.
"""

import secrets
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import config

COOKIE_NAME = "axon_key"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365          # a year

# Loopback addresses. Trusted without a key — see the module docstring.
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def ensure_key() -> str:
    """
    Return the access key, creating one on first run.

    token_urlsafe(24) is ~32 characters of cryptographically secure
    randomness. Do NOT reach for random.choice() here: that module is seeded
    predictably and is meant for simulations, not secrets. `secrets` exists
    precisely for this distinction.
    """
    if config.ACCESS_KEY:
        return config.ACCESS_KEY

    key = secrets.token_urlsafe(24)

    env_path = config.ROOT / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if not existing.endswith("\n") and existing:
        existing += "\n"

    env_path.write_text(
        existing
        + "\n# Password for reaching Sid from another device. Generated"
        + " automatically.\n"
        + f"AXON_KEY={key}\n",
        encoding="utf-8",
    )

    config.ACCESS_KEY = key                   # so this process sees it too
    return key


def is_authorised(request: Request) -> bool:
    client = request.client.host if request.client else ""

    if client in LOCAL_HOSTS:
        return True

    key = config.ACCESS_KEY
    if not key:                               # no key set: fail closed
        return False

    # secrets.compare_digest instead of == : it takes the same amount of time
    # whether the first character differs or the last. A plain == leaks, in
    # its timing, how much of a guess was correct — which is enough to
    # reconstruct a secret one character at a time.
    supplied = request.cookies.get(COOKIE_NAME) or request.query_params.get("key", "")
    return bool(supplied) and secrets.compare_digest(supplied, key)


async def middleware(request: Request, call_next):
    """
    Runs before every single request. Nothing is exempt.

    Deliberately including /manifest.webmanifest and the icons: an unlocked
    endpoint is an unlocked endpoint, and there's no reason a stranger needs
    to know what your app is called.
    """
    if not is_authorised(request):
        # An API call gets JSON; a browser gets something readable.
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"detail": "Not authorised. Open Sid with your access link."},
                status_code=401,
            )
        return HTMLResponse(_locked_page(), status_code=401)

    response = await call_next(request)

    # If they arrived with ?key=... and it was correct, remember it so the
    # link only ever has to be used once.
    supplied = request.query_params.get("key", "")
    if supplied and config.ACCESS_KEY and secrets.compare_digest(supplied, config.ACCESS_KEY):
        response.set_cookie(
            COOKIE_NAME,
            supplied,
            max_age=COOKIE_MAX_AGE,
            httponly=True,        # JavaScript can't read it, so XSS can't steal it
            samesite="lax",
        )

    return response


def _locked_page() -> str:
    return """<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sid - locked</title>
<style>
  body { background:#0d0f14; color:#e6e9ef; font-family:system-ui, sans-serif;
         display:grid; place-items:center; height:100vh; margin:0; text-align:center }
  div { max-width: 340px; padding: 24px }
  h1 { font-size:20px; margin:0 0 12px }
  p  { color:#8b93a5; font-size:14px; line-height:1.6 }
  code { background:#1e2430; padding:2px 6px; border-radius:4px; font-size:13px }
</style>
<div>
  <h1>Sid is locked</h1>
  <p>This device hasn't been paired yet.</p>
  <p>On the computer running Sid, use <code>py mobile.py</code>
     and scan the QR code it prints.</p>
</div>
"""

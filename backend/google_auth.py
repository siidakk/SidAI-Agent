"""
google_auth.py — connecting Sid to your Google account, safely.

WHAT OAUTH ACTUALLY IS
----------------------
The naive way to let a program read your email is to give it your password.
That's catastrophic: the program can then do *everything* — change your
password, delete your account, read everything forever — and you can't take
it back without changing your password everywhere.

OAuth exists to avoid that. The flow:

  1. Sid sends you to Google's own login page. **Sid never sees the page,
     never sees your password, never touches your 2FA.**
  2. Google asks YOU: "Sid wants to read your email and manage your
     calendar. Allow?"
  3. If you agree, Google hands Sid a *token* — not your password.
  4. That token is limited to the permissions you approved (its "scopes"),
     and you can revoke it at any time from your Google account page.

The key insight: **you are authorising a capability, not sharing an
identity.** That's the same principle as the tool tiers from Phase 2 — Sid
gets exactly the power you chose to lend, and no more.

TWO TOKENS, VERY DIFFERENT RISK
-------------------------------
  access token   short-lived (~1 hour), used on every API call
  refresh token  long-lived, used to get new access tokens forever

The refresh token is the crown jewel. That's why it goes in vault.py,
encrypted with your Windows login, rather than a plain JSON file.

WHY THE LOOPBACK FLOW
---------------------
Sid is a desktop app, so there's no server with a fixed public URL for
Google to redirect back to. Instead we start a tiny web server on a random
free port on 127.0.0.1, tell Google to redirect there, and shut it down the
moment the code arrives. Google calls this the "loopback" flow and it's the
recommended one for installed apps.
"""

from pathlib import Path

from . import config, vault

# What we're asking permission for. Each of these appears on the consent
# screen, and Google will only ever issue tokens valid for exactly these.
#
# READ-ONLY BY DEFAULT, DELIBERATELY. `gmail.readonly` cannot send, delete or
# modify anything - so even a badly-confused agent, or a prompt injection
# hidden inside an email, cannot email anyone as you.
#
# `gmail.compose` is included because drafting is genuinely useful and a draft
# is harmless: it sits in your Drafts folder until YOU press send. Note there
# is no `gmail.send` here at all. Phase 7 adds that, together with the
# approval flow that should always accompany it.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

# You download this from Google Cloud Console. See NOTES/phase-3.md §2.
CLIENT_SECRETS = config.ROOT / "credentials.json"

VAULT_KEY = "google"


def is_configured() -> bool:
    """Have you downloaded credentials.json yet?"""
    return CLIENT_SECRETS.exists()


def is_connected() -> bool:
    """Have you completed the consent flow?"""
    return vault.get(VAULT_KEY) is not None


def status() -> dict:
    """Summary for /api/connections. Never includes the tokens themselves."""
    if not is_configured():
        return {
            "connected": False,
            "detail": "No credentials.json - see NOTES/phase-3.md",
        }
    if not is_connected():
        return {"connected": False, "detail": "Not connected yet"}

    saved = vault.get(VAULT_KEY) or {}
    return {
        "connected": True,
        "detail": saved.get("email", "connected"),
        "scopes": len(saved.get("scopes", [])),
    }


def connect() -> str:
    """
    Run the consent flow. Opens your browser; blocks until you approve.

    Only ever called from the machine Sid runs on — it needs to open a
    browser and listen on localhost, neither of which works remotely.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not is_configured():
        raise RuntimeError(
            f"{CLIENT_SECRETS.name} is missing. Create an OAuth client in "
            "Google Cloud Console and save it as credentials.json in the "
            "Sid folder. Steps are in NOTES/phase-3.md."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), SCOPES)

    # port=0 asks the OS for any free port, so this never collides with the
    # Sid server or anything else you're running.
    try:
        credentials = flow.run_local_server(
            port=0,
            prompt="consent",          # force a refresh token even on re-connect
            access_type="offline",     # "offline" is what MAKES a refresh token
            success_message=(
                "Sid is connected. You can close this tab."
            ),
        )
    except Exception as exc:
        raise RuntimeError(_explain_oauth_failure(exc)) from exc

    email = _fetch_email(credentials.token)
    _store(credentials, email)
    return email or "connected"


def _project_id() -> str:
    """Read the project id out of credentials.json, for building console links."""
    import json

    try:
        data = json.loads(CLIENT_SECRETS.read_text())
        return (data.get("installed") or data.get("web") or {}).get("project_id", "")
    except Exception:
        return ""


def _explain_oauth_failure(exc: Exception) -> str:
    """
    Turn Google's OAuth errors into the actual thing you need to click.

    Google's messages are written for developers who already know how the
    consent screen works. "access_denied" in particular sounds like YOU
    refused, when it usually means your own account isn't on the test-user
    list. Worth translating - you will hit this again every time you change
    the consent screen.
    """
    text = str(exc)
    project = _project_id()
    suffix = f"?project={project}" if project else ""

    if "access_denied" in text:
        return f"""Google refused: your account isn't an approved tester yet.

A new OAuth app starts in Testing mode, where only accounts you explicitly
list may sign in - and your own is NOT on that list by default. This is not
about the permissions you were shown; you never got that far.

Fix it here:
  https://console.cloud.google.com/auth/audience{suffix}

  -> Test users -> + Add users
  -> add the exact Google account you are signing in with
  -> Save

Then run:  py connect.py google"""

    if "invalid_client" in text or "unauthorized_client" in text:
        return f"""Google rejected the client.

credentials.json is probably for the wrong application type. It must be an
OAuth client of type "Desktop app" - a Web application client will not work
with the loopback flow.

  https://console.cloud.google.com/auth/clients{suffix}"""

    if "invalid_scope" in text:
        return f"""Google rejected the permissions.

Make sure BOTH the Gmail API and the Google Calendar API are enabled for
this project:

  https://console.cloud.google.com/apis/library{suffix}"""

    return f"Google sign-in failed: {text[:300]}"


def disconnect() -> bool:
    """
    Forget the tokens.

    NOTE this only forgets them locally. To fully revoke Sid's access, visit
    myaccount.google.com/permissions. Worth knowing the difference: deleting
    your copy of a key is not the same as changing the lock.
    """
    return vault.delete(VAULT_KEY)


def _store(credentials, email: str | None) -> None:
    vault.put(VAULT_KEY, {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes or []),
        "email": email,

        # WHEN the access token dies. Leaving this out was a real bug.
        #
        # Without an expiry, Credentials.expired is False, so .valid is True,
        # so the refresh never fires - and we kept sending an access token
        # that had died an hour after you first connected. It worked
        # perfectly on the day it was set up and silently broke the next day,
        # which is the most annoying shape a bug can have.
        "expiry": credentials.expiry.isoformat() if credentials.expiry else None,
    })


def _fetch_email(access_token: str) -> str | None:
    """Ask Google who we just connected as, so the UI can show it."""
    import httpx

    try:
        r = httpx.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        return r.json().get("email") if r.status_code == 200 else None
    except Exception:
        return None


def get_access_token(force_refresh: bool = False) -> str:
    """
    Return a valid access token, refreshing it if it has expired.

    Every Gmail and Calendar tool calls this. It is the only place that knows
    how tokens are stored or renewed — the tools just ask for a token.

    Access tokens last about an hour, so refreshing is normal operation, not
    an error path. The refreshed token is written back to the vault so the
    next request doesn't have to refresh again.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    saved = vault.get(VAULT_KEY)
    if not saved:
        raise RuntimeError(
            "Google isn't connected. Click Connect in Sid, or run: "
            "py connect.py google"
        )

    # Put the expiry back, so the library can tell whether the token is dead.
    expiry = None
    if saved.get("expiry"):
        from datetime import datetime

        try:
            expiry = datetime.fromisoformat(saved["expiry"])
            # google-auth compares against a naive UTC datetime; a
            # timezone-aware one raises "can't compare offset-naive and
            # offset-aware datetimes" deep inside the library.
            if expiry.tzinfo is not None:
                expiry = expiry.replace(tzinfo=None)
        except ValueError:
            expiry = None

    credentials = Credentials(
        token=saved["token"],
        refresh_token=saved["refresh_token"],
        token_uri=saved["token_uri"],
        client_id=saved["client_id"],
        client_secret=saved["client_secret"],
        scopes=saved["scopes"],
        expiry=expiry,
    )

    if force_refresh or not credentials.valid:
        if not credentials.refresh_token:
            raise RuntimeError("No refresh token. Reconnect Google.")
        try:
            credentials.refresh(Request())
        except Exception as exc:
            raise RuntimeError(
                f"Google login expired and couldn't be renewed ({exc}). "
                "Reconnect: py connect.py google"
            )
        _store(credentials, saved.get("email"))

    return credentials.token

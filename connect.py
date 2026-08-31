"""
connect.py — link Sid to an outside account.

    py connect.py              show what's connected
    py connect.py google       connect Google (opens your browser)
    py connect.py google --off forget the stored tokens

FIRST TIME? You need a credentials.json from Google Cloud Console before this
will work. It takes about five minutes and the steps are in
NOTES/phase-3.md §2. There is no way around it — Google will not let a program
create OAuth clients on your behalf, which is exactly the point of OAuth.
"""

import os
import sys

from backend import google_auth, vault

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[90m", "\033[1m", "\033[0m"


def show() -> None:
    status = google_auth.status()
    mark = f"{GREEN}connected{OFF}" if status["connected"] else f"{RED}not connected{OFF}"

    print()
    print(f"{BOLD}Connections{OFF}")
    print(f"  google   {mark}  {DIM}{status['detail']}{OFF}")

    if status["connected"]:
        print(f"  {DIM}scopes granted: {status.get('scopes', 0)}{OFF}")

    print()
    print(f"{DIM}vault: {vault.VAULT_PATH}{OFF}")
    print(f"{DIM}stored entries: {', '.join(vault.keys()) or 'none'}{OFF}")

    if not status["connected"]:
        print()
        if not google_auth.is_configured():
            print("Next: create credentials.json — see NOTES/phase-3.md §2")
        else:
            print("Next: py connect.py google")
    print()


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    if not args:
        show()
        return

    service = args[0].lower()
    if service != "google":
        print(f"{RED}Unknown service '{service}'. Only 'google' so far.{OFF}")
        return

    if "--off" in sys.argv:
        if google_auth.disconnect():
            print(f"\n{GREEN}Disconnected.{OFF} Tokens deleted from the vault.")
            print(f"{DIM}To fully revoke Sid's access at Google's end, visit")
            print(f"https://myaccount.google.com/permissions{OFF}\n")
        else:
            print("\nGoogle wasn't connected.\n")
        return

    print("\nOpening your browser. Sign in and choose what to allow.")
    print(f"{DIM}Sid never sees your password — Google handles the login and")
    print(f"hands back a limited token.{OFF}\n")

    try:
        email = google_auth.connect()
    except Exception as exc:
        print(f"{RED}Failed:{OFF} {exc}\n")
        return

    print(f"{GREEN}Connected as {email}{OFF}")
    print(f"{DIM}Tokens encrypted with your Windows login and saved to")
    print(f"{vault.VAULT_PATH}{OFF}\n")


if __name__ == "__main__":
    if os.name == "nt":
        os.system("")
    main()

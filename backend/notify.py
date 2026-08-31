"""
notify.py — how Sid gets your attention when you didn't ask.

THE SHIFT THIS REPRESENTS
-------------------------
Every phase so far was reactive: you ask, Sid answers. Phase 9 flips it —
Sid decides something is worth telling you and interrupts. That is a much
bigger deal than it sounds, because **an assistant that can interrupt you
can also annoy you into uninstalling it.**

So the rule this file exists to enforce: notify when the answer changes what
you'd do next, and stay silent otherwise. A notification that says "your
scheduled check ran successfully" is pure noise.

THREE CHANNELS, DELIBERATELY
----------------------------
  toast    Windows notification. Works when you're at the laptop, even with
           Sid's window closed. Zero dependencies - Windows has a WinRT API
           and PowerShell can reach it.

  in-app   Pushed down the /api/events stream from Phase 6. Reaches any open
           tab, including your phone.

  log      Always. Even if nobody sees the notification, the audit trail
           records that Sid tried.

WHAT ABOUT PHONE PUSH?
----------------------
Real Web Push would reach your phone with Sid closed. It needs VAPID keys, a
subscription store, and - the blocker here - a STABLE origin. Push
subscriptions are tied to the origin that created them, and the free ngrok
URL changes every session, so every subscription would die on restart.

That's a domain-name problem, not a code problem. Noting it honestly rather
than shipping something that silently stops working after one restart.
"""

import subprocess

from . import events

# Windows shows the app name on the toast; there's no way to change it
# without registering a real AppUserModelID, so PowerShell it is.
_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] > $null

$template = @"
<toast activationType="protocol">
  <visual><binding template="ToastGeneric">
    <text>TITLE_HERE</text>
    <text>BODY_HERE</text>
  </binding></visual>
</toast>
"@

$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Sid').Show($toast)
"""


def _escape(text: str) -> str:
    """
    XML-escape, because the toast body is XML.

    A notification containing "AT&T" or "5 > 3" would otherwise produce
    malformed XML and simply never appear - a silent failure, which is the
    worst kind for something whose entire job is being noticed.
    """
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def toast(title: str, body: str) -> bool:
    """Show a Windows notification. Returns whether it worked."""
    script = (_TOAST_SCRIPT
              .replace("TITLE_HERE", _escape(title[:64]))
              .replace("BODY_HERE", _escape(body[:200])))
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except Exception:
        return False


def send(title: str, body: str, kind: str = "info", task_id: str | None = None) -> dict:
    """
    Notify on every available channel.

    Never raises. A notification failing must not take down the job that
    produced it - the work was the point, telling you about it was the
    garnish.
    """
    delivered = {"toast": False, "in_app": 0, "push": 0}

    try:
        delivered["toast"] = toast(title, body)
    except Exception:
        pass

    try:
        delivered["in_app"] = events.publish({
            "type": "notification",
            "title": title,
            "body": body,
            "kind": kind,
            "task_id": task_id,
        })
    except Exception:
        pass

    # Phase 10: the phone, with Sid closed. The only channel that reaches you
    # when you aren't looking at any screen Sid controls.
    #
    # Imported here rather than at the top so a missing pywebpush degrades the
    # notification instead of breaking the import of everything that notifies.
    try:
        from . import push
        delivered["push"] = push.send(title, body, kind, task_id).get("sent", 0)
    except Exception:
        pass

    return delivered

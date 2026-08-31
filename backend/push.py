"""
push.py — notifications that reach your phone with Sid closed.

THE DIFFERENCE FROM PHASE 9
---------------------------
`notify.py` had two channels. A Windows toast reaches you at the laptop. An
in-app card reaches any page that is **currently open**.

Neither reaches your phone in your pocket with the app closed. That needs
Web Push, which is a genuinely different mechanism: your phone holds an open
connection to a push service run by Google or Apple, and Sid hands that
service a message to deliver. Your phone doesn't need to be reachable, and
Sid doesn't need to know where your phone is.

HOW IT WORKS, IN FOUR STEPS
---------------------------
1. Sid generates a **VAPID key pair** once. The public half identifies Sid to
   the push service; the private half signs its requests. It is how the push
   service knows a message really came from Sid.

2. Your phone calls `pushManager.subscribe()` and gets back a **subscription**
   - an endpoint URL at Google's push service plus two encryption keys. It
   sends that to Sid, which stores it.

3. To notify you, Sid encrypts the message with the subscription's keys and
   POSTs it to that endpoint. **The push service cannot read it.** It is a
   courier carrying a sealed envelope.

4. Your phone's OS wakes the service worker, which decrypts the message and
   shows a notification.

THE HONEST PROBLEM: ORIGIN
--------------------------
A push subscription is bound to the **origin** that created it. Sid's phone
origin is a free ngrok URL, and that changes every time the tunnel restarts.
When it does, every stored subscription becomes rubbish: the browser at the
new origin has no idea about the old subscription, and pushing to it either
fails or silently reaches nothing.

Phase 9 called this a blocker and skipped push. That was half right. Push is
still worth having, because the tunnel commonly stays up for days, and within
one tunnel session push reaches your phone **with the app closed** - which
neither of the other two channels can do.

So this stores the origin alongside each subscription, and drops the ones
that no longer match. A dead subscription is deleted, not retried. The
alternative - a queue of undeliverable messages growing forever - is worse
than admitting the phone is gone.

If you want push that survives restarts, the fix is a stable domain
(a paid ngrok domain, or a Cloudflare tunnel), set as AXON_PUBLIC_ORIGIN.
That is a domain-name problem, not a code problem, and this file is ready for
it the day you have one.
"""

import json
import sqlite3
from datetime import datetime, timezone

from . import config

DB_PATH = config.ROOT / "data" / "push.db"

# The contact address inside the signed VAPID token.
#
# THIS VALUE IS LOAD-BEARING, which is not obvious. It was "mailto:sid@
# localhost" and Apple rejected every push with `403 BadJwtToken` - no hint
# that the address was the problem. Apple validates that the domain is a real
# one; `localhost` and `.local` are both refused. Google never cared.
#
# Probed against a real iPhone:
#     mailto:sid@localhost      403 BadJwtToken
#     mailto:noreply@sid.local  403 BadJwtToken
#     mailto:sid@example.com    delivered
#     https://example.com       delivered
#
# Nobody reads this address - it exists so a push service can contact the
# app's operator. Set AXON_CONTACT_EMAIL if you want it to be yours.
VAPID_CLAIM_EMAIL = "mailto:" + (
    __import__("os").getenv("AXON_CONTACT_EMAIL") or "sid@example.com")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                endpoint   TEXT PRIMARY KEY,
                keys       TEXT NOT NULL,      -- JSON: p256dh + auth
                origin     TEXT NOT NULL,      -- which URL created it
                label      TEXT,               -- "Android · Chrome"
                created_at TEXT NOT NULL,
                last_ok    TEXT,
                failures   INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS vapid (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                private_pem TEXT NOT NULL,
                public_key  TEXT NOT NULL      -- base64url, for the browser
            );
        """)


# ==========================================================================
#  The key pair
# ==========================================================================

def _keys() -> tuple[str, str]:
    """
    Return (private_pem, public_key_b64). Generates them on first use.

    Generated ONCE and stored. Regenerating them would invalidate every
    existing subscription, because the public key is baked into each one at
    subscribe time - so this must never be casually recreated.
    """
    init()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM vapid WHERE id = 1").fetchone()
        if row:
            return row["private_pem"], row["public_key"]

    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    # The browser wants the raw uncompressed point (65 bytes), base64url with
    # no padding. Not PEM, not DER - this exact shape, or subscribe() throws.
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = base64.urlsafe_b64encode(raw).decode().rstrip("=")

    with _connect() as conn:
        conn.execute("INSERT OR REPLACE INTO vapid (id, private_pem, public_key) "
                     "VALUES (1, ?, ?)", (private_pem, public_b64))
    return private_pem, public_b64


def public_key() -> str:
    """The key the browser needs to subscribe."""
    return _keys()[1]


def _signer():
    """
    The private key as pywebpush actually wants it.

    NOT as a PEM string, which is the obvious thing to pass and fails:
    pywebpush hands a bare string to `Vapid.from_string`, which strips
    newlines and base64-decodes - so a PEM's `-----BEGIN` header becomes an
    "ASN.1 parsing error: invalid length" with nothing pointing at the real
    cause. It only understands RAW or DER there.

    It does accept a Vapid object though, and `Vapid.from_pem` reads exactly
    what we stored. So build the object and hand that over.
    """
    from py_vapid import Vapid01

    return Vapid01.from_pem(_keys()[0].encode("utf-8"))


# ==========================================================================
#  Subscriptions
# ==========================================================================

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save(subscription: dict, origin: str, label: str = "") -> None:
    """Store (or refresh) one device's subscription."""
    init()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO subscriptions "
            "(endpoint, keys, origin, label, created_at, failures) "
            "VALUES (?,?,?,?,?,0)",
            (subscription["endpoint"], json.dumps(subscription.get("keys", {})),
             origin, label[:80], _now()),
        )


def remove(endpoint: str) -> bool:
    init()
    with _connect() as conn:
        return conn.execute("DELETE FROM subscriptions WHERE endpoint=?",
                            (endpoint,)).rowcount > 0


def all_subscriptions() -> list[dict]:
    init()
    with _connect() as conn:
        return [dict(r) for r in
                conn.execute("SELECT * FROM subscriptions ORDER BY created_at DESC")]


def current_origin() -> str:
    """
    The origin phones are currently reaching Sid on.

    A configured public origin wins - that's the stable-domain case. Failing
    that, the live tunnel URL. Failing that, nothing: there is no phone
    origin at all when no tunnel is up.
    """
    configured = getattr(config, "PUBLIC_ORIGIN", "") or ""
    if configured:
        return configured.rstrip("/")

    from . import tunnel
    return (tunnel.current_url() or "").rstrip("/")


# ==========================================================================
#  Sending
# ==========================================================================

def send(title: str, body: str, kind: str = "info",
         task_id: str | None = None) -> dict:
    """
    Push to every live subscription. Never raises.

    Returns {"sent": n, "dropped": n} so the caller can see whether it
    actually went anywhere - "we tried" is not the same as "it arrived", and
    a notification system that can't tell you which is which is not much of
    one.
    """
    init()
    subscriptions = all_subscriptions()
    if not subscriptions:
        return {"sent": 0, "dropped": 0, "detail": "no devices subscribed"}

    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return {"sent": 0, "dropped": 0, "detail": "pywebpush not installed"}

    try:
        signer = _signer()
    except Exception as exc:
        return {"sent": 0, "dropped": 0,
                "detail": f"VAPID key unusable: {type(exc).__name__}: {exc}"[:200]}

    origin = current_origin()
    payload = json.dumps({"title": title[:80], "body": body[:300],
                          "kind": kind, "task_id": task_id})

    sent = dropped = 0
    # WHY THE ERRORS ARE COLLECTED RATHER THAN SWALLOWED.
    #
    # The first version counted failures in the database and returned
    # {"sent": 0, "dropped": 0} - technically true and completely useless.
    # The real cause was a key-format error that no one could see, and it
    # took a direct call to pywebpush to find it.
    #
    # A delivery system that can't tell you WHY nothing arrived is barely
    # better than one that doesn't work.
    errors: list[str] = []
    for row in subscriptions:
        # A subscription made at a different origin cannot work. Drop it
        # rather than pushing into the void - see the header comment.
        if origin and row["origin"] and row["origin"] != origin:
            remove(row["endpoint"])
            dropped += 1
            continue

        try:
            webpush(
                subscription_info={"endpoint": row["endpoint"],
                                   "keys": json.loads(row["keys"])},
                data=payload,
                vapid_private_key=signer,
                vapid_claims={"sub": VAPID_CLAIM_EMAIL},
                timeout=10,
            )
            sent += 1
            with _connect() as conn:
                conn.execute("UPDATE subscriptions SET last_ok=?, failures=0 "
                             "WHERE endpoint=?", (_now(), row["endpoint"]))
        except WebPushException as exc:
            # 404/410 mean the push service has permanently forgotten this
            # subscription: the app was uninstalled, or the browser cleared
            # it. That is not a retryable error, it's a headstone.
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if status in (404, 410):
                remove(row["endpoint"])
                dropped += 1
            else:
                detail = getattr(response, "text", "") or str(exc)
                errors.append(f"{status or '?'}: {detail[:160]}")
                with _connect() as conn:
                    conn.execute("UPDATE subscriptions SET failures=failures+1 "
                                 "WHERE endpoint=?", (row["endpoint"],))
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")
            with _connect() as conn:
                conn.execute("UPDATE subscriptions SET failures=failures+1 "
                             "WHERE endpoint=?", (row["endpoint"],))

    result = {"sent": sent, "dropped": dropped}
    if errors:
        result["errors"] = errors[:3]
    return result

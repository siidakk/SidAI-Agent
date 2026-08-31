"""
vault.py — where Sid keeps things that would hurt you if leaked.

WHAT'S ABOUT TO BE STORED HERE
------------------------------
From Phase 3 onwards, Sid holds a Google **refresh token**. That is not a
minor secret. A refresh token can be exchanged for a fresh access token any
time, forever, until you revoke it. Whoever has it can read your entire inbox.
It is closer to a password than to a cookie.

Writing that to a plain JSON file — which is what almost every tutorial does —
means any program you ever run, any backup that syncs your folder, and anyone
who picks up your unlocked laptop, has your email.

THE FIX ON WINDOWS: DPAPI
-------------------------
Windows has a built-in service for exactly this: `CryptProtectData`. It
encrypts data with a key derived from **your Windows login**. Two properties
matter:

  1. You never manage a password or a key file. There is no "where do I put
     the key that protects the key" problem — Windows already solved it.
  2. The ciphertext is bound to your user account. Another user on the same
     machine, or someone who copies the file to a different PC, gets nothing.

That's the whole idea: don't invent a way to protect secrets. Ask the
operating system, which has a keyring designed for it — DPAPI on Windows,
Keychain on macOS, libsecret/gnome-keyring on Linux.
"""

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import platform
from pathlib import Path

from . import config

VAULT_PATH = config.ROOT / "data" / "vault.bin"

IS_WINDOWS = platform.system() == "Windows"


# --------------------------------------------------------------------------
#  DPAPI plumbing
# --------------------------------------------------------------------------
class _Blob(ctypes.Structure):
    """The DATA_BLOB struct the Windows crypto API passes data around in."""

    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _to_blob(data: bytes) -> _Blob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _from_blob(blob: _Blob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _encrypt(plaintext: bytes) -> bytes:
    if not IS_WINDOWS:
        return plaintext                       # see the warning in save()

    out = _Blob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(_to_blob(plaintext)),
        "sid",        # a description, visible in some Windows tooling
        None, None, None,
        0,
        ctypes.byref(out),
    )
    if not ok:
        raise OSError("DPAPI encryption failed")
    return _from_blob(out)


def _decrypt(ciphertext: bytes) -> bytes:
    if not IS_WINDOWS:
        return ciphertext

    out = _Blob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(_to_blob(ciphertext)),
        None, None, None, None,
        0,
        ctypes.byref(out),
    )
    if not ok:
        # Happens if the file was copied from another machine or another
        # Windows account. That is DPAPI working correctly, not a bug.
        raise OSError(
            "Could not decrypt the vault. It was created by a different "
            "Windows account or on a different machine. Delete data/vault.bin "
            "and reconnect."
        )
    return _from_blob(out)


# --------------------------------------------------------------------------
#  The store itself
# --------------------------------------------------------------------------
def load() -> dict:
    """Read the whole vault. Returns {} if there isn't one yet."""
    if not VAULT_PATH.exists():
        return {}
    try:
        return json.loads(_decrypt(VAULT_PATH.read_bytes()).decode("utf-8"))
    except Exception:
        return {}


def save(data: dict) -> None:
    """Write the whole vault, encrypted."""
    VAULT_PATH.parent.mkdir(parents=True, exist_ok=True)

    blob = _encrypt(json.dumps(data).encode("utf-8"))

    # Write to a temp file then replace. If the power dies mid-write you keep
    # the old vault instead of a half-written one that decrypts to garbage.
    # os.replace is atomic on Windows and POSIX alike.
    temp = VAULT_PATH.with_suffix(".tmp")
    temp.write_bytes(blob)
    os.replace(temp, VAULT_PATH)

    if not IS_WINDOWS:
        print("WARNING: not on Windows - tokens are stored UNENCRYPTED.")


def get(key: str) -> dict | None:
    """Fetch one entry, e.g. get('google')."""
    return load().get(key)


def put(key: str, value: dict) -> None:
    """Store one entry. Read-modify-write, so other entries survive."""
    data = load()
    data[key] = value
    save(data)


def delete(key: str) -> bool:
    """Forget one entry. Returns True if there was something to forget."""
    data = load()
    if key not in data:
        return False
    del data[key]
    save(data)
    return True


def keys() -> list[str]:
    """Which services are connected. Never returns the secrets themselves."""
    return sorted(load())

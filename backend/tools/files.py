"""
tools/files.py — letting Sid see your disk.

⚠️ THIS IS THE FIRST GENUINELY RISKY FILE IN THE PROJECT.

Everything here runs with YOUR full permissions. If a tool takes a path from
the model and opens it blindly, then anything that can influence the model can
read anything you can read — your SSH keys, your browser cookies, your .env.

The defence is a SANDBOX ROOT. Every path is resolved to an absolute path and
checked to be inside one allowed folder. Two rules do the work:

  1. Resolve FIRST, check SECOND. "C:/Users/Malika/../../Windows/System32"
     looks fine as text and escapes completely once resolved.
  2. Deny by default. Anything we can't prove is inside the root is refused.

Read `_safe_path` below carefully — that function is the whole security model.
"""

from pathlib import Path

from .. import config
from . import tool

# The one folder Sid may look inside. Everything else is off limits.
# Set to the project folder rather than your whole home directory: start
# narrow, widen deliberately. Widening is a decision; narrowing after a leak
# is damage control.
# Derived from where this file actually lives, never hardcoded. A literal
# folder name goes stale the moment the project is renamed or moved - which
# is exactly what happened when Axon became Sid, and it silently broke every
# file tool until someone checked.
SANDBOX = config.ROOT

# Never show these even inside the sandbox.
BLOCKED_NAMES = {".env", ".git", "__pycache__", ".venv"}

MAX_READ_BYTES = 20_000     # ~5000 tokens. Don't blow up the context window.


def _safe_path(user_path: str) -> Path:
    """
    Turn whatever the model sent into a real path inside SANDBOX, or raise.

    This is the security boundary of the whole file. Every tool below calls it
    before touching the disk.
    """
    # "." means the sandbox root itself.
    candidate = SANDBOX if user_path.strip() in ("", ".") else Path(user_path)

    if not candidate.is_absolute():
        candidate = SANDBOX / candidate

    # .resolve() collapses "..", follows symlinks, and normalises slashes.
    # It MUST happen before the check, not after.
    resolved = candidate.resolve()
    root = SANDBOX.resolve()

    # is_relative_to answers "is this genuinely inside that folder?"
    if resolved != root and not resolved.is_relative_to(root):
        raise PermissionError(
            f"'{user_path}' is outside the allowed folder ({root}). "
            f"Sid can only look inside its own project directory."
        )

    if any(part in BLOCKED_NAMES for part in resolved.parts):
        raise PermissionError(f"'{user_path}' is in a blocked location.")

    return resolved


@tool(tier="read")
def list_files(folder: str = ".") -> str:
    """List the files and folders inside a directory of the Sid project.

    Args:
        folder: Which folder to list, relative to the project root.
                Use "." for the project root itself, or e.g. "backend".
    """
    try:
        target = _safe_path(folder)
    except PermissionError as exc:
        return f"Refused: {exc}"

    if not target.exists():
        return f"No such folder: {folder}"
    if not target.is_dir():
        return f"{folder} is a file, not a folder. Use read_file instead."

    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if item.name in BLOCKED_NAMES:
            continue
        if item.is_dir():
            entries.append(f"  {item.name}/")
        else:
            entries.append(f"  {item.name}  ({item.stat().st_size:,} bytes)")

    if not entries:
        return f"{folder} is empty."
    return f"Contents of {folder}:\n" + "\n".join(entries)


@tool(tier="read")
def read_file(path: str) -> str:
    """Read the contents of a text file in the Sid project.

    Args:
        path: The file to read, relative to the project root,
              e.g. "backend/config.py" or "README.md"
    """
    try:
        target = _safe_path(path)
    except PermissionError as exc:
        return f"Refused: {exc}"

    if not target.exists():
        return f"No such file: {path}"
    if target.is_dir():
        return f"{path} is a folder. Use list_files instead."

    try:
        # errors="replace" so a binary file returns gibberish rather than
        # raising — the model can then see it picked a silly file.
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Could not read {path}: {exc}"

    if len(text) > MAX_READ_BYTES:
        text = text[:MAX_READ_BYTES] + f"\n\n[... truncated, file is {len(text):,} chars]"

    return f"--- {path} ---\n{text}"

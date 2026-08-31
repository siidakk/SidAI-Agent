"""
config.py  —  every setting Sid has, in one place.

WHY THIS FILE EXISTS
--------------------
Two rules that professional projects follow:

1. "Don't hardcode."  If you scatter the model name across 12 files, changing
   it means finding 12 files. Put it in one place instead.
2. "Never put secrets in code."  Your API keys must NOT live in a .py file,
   because .py files get committed to git and pushed to GitHub, and bots
   scan GitHub for leaked keys within seconds. Secrets live in .env,
   which git is told to ignore.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# __file__ is this file. .resolve() makes it an absolute path.
# .parent is /backend, .parent.parent is /Sid  <- the project root.
ROOT = Path(__file__).resolve().parent.parent

# Reads Axon/.env and copies every KEY=value line into the environment.
# After this line, os.getenv("...") works.
load_dotenv(ROOT / ".env")

# ======================================================================
#  WHICH BRAIN?
# ======================================================================
# One of: "ollama" (free, runs on your laptop)
#         "gemini" (free cloud tier, needs a free key)
#         "claude" (paid, best quality)
#
# Change this one line in .env to swap the entire AI backend.
# See backend/providers/ for how that's possible.
PROVIDER = os.getenv("AXON_PROVIDER", "gemini")

# ---- ollama: a model running on your own machine ---------------------
# Costs nothing, works offline, uses your RAM. Needs ~2.5 GB free for a 3B
# model — close Chrome if it complains about memory.
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")

# How long Ollama keeps the model - and its cached prompt - in RAM.
#
# This matters far more than it looks. When the model unloads, Ollama also
# throws away the processed prompt prefix, and rebuilding that costs ~46
# SECONDS on this laptop (see providers/ollama.py -> warmup).
#
#   "30m"  - default. Stays warm through a normal session.
#   "-1"   - never unload. Fastest, but holds ~2 GB forever.
#   "5m"   - frees RAM sooner, but you pay ~60s again after any 5-minute gap.
#
# On an 8 GB machine this is a genuine tradeoff: 2 GB held vs a minute of
# waiting. 30m is the compromise.
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")

# Prime the model at startup so the first question isn't the slow one.
# Set to 0 to skip it (e.g. if you're short on RAM and rarely use Ollama).
WARMUP = os.getenv("AXON_WARMUP", "1") != "0"

# ---- gemini: Google's free tier --------------------------------------
# Free key (no credit card): https://aistudio.google.com/apikey
# NOTE: on the free tier Google uses your data to improve their products.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

# ---- claude: paid, for when quality matters --------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")

# ======================================================================
#  SHARED SETTINGS
# ======================================================================

# A CEILING, not a target. The model stops when it's done; this just
# prevents a runaway response.
MAX_TOKENS = int(os.getenv("AXON_MAX_TOKENS", "4096"))

# The system prompt: standing instructions the model sees on every single
# request, before your message. This is where personality and rules live.
#
# Kept short and concrete on purpose — small local models follow three plain
# rules far better than they follow three paragraphs of nuance.
SYSTEM_PROMPT = """You are Sid, a personal AI assistant.

Be direct and concise. Skip filler like "Great question!" or "I'd be happy to
help" — just answer.

Reply in whatever language the user writes in. If they write in Hindi, answer
in Hindi. If they mix Hindi and English (Hinglish), mix them back. Keep the
same script they used - Devanagari if they used Devanagari, Roman if Roman.

Use your tools rather than guessing. You cannot know the time, the contents of
files, or anything recent without calling a tool first.

Two things you CAN do that assistants usually cannot. Never say you are unable
to do these, and never claim to have done them without calling the tool:

- Schedule work for later, including repeating schedules. "Every morning at
  8...", "remind me at 6", "keep checking every hour" -> call schedule_task.
- Browse the actual web with a real browser: open_page, read_page, click,
  fill. Use it for pages search cannot reach.
"""

# ======================================================================
#  SERVER
# ======================================================================
# "0.0.0.0" means "accept connections on every network interface".
# If it were "127.0.0.1" the server would only answer your own laptop, and
# your phone could not reach it. This one string is why Sid works on mobile.
# Password for reaching Sid from another device. Auto-generated on first
# run by backend/auth.py and written into .env. Requests from localhost never
# need it - see auth.py for why.
ACCESS_KEY = os.getenv("AXON_KEY", "")

HOST = os.getenv("AXON_HOST", "0.0.0.0")
PORT = int(os.getenv("AXON_PORT", "8321"))

# A permanent https:// address for Sid, if you have one.
#
# Push notifications are tied to the ORIGIN that created them, and the free
# ngrok URL changes every session - so every phone subscription dies on
# restart. Set this to a stable domain (a paid ngrok domain, a Cloudflare
# tunnel) and push survives restarts. Left empty, Sid falls back to whatever
# tunnel is currently up, and push works only for as long as it stays up.
#
# Empty is a legitimate setting, not a broken one. It just means push is
# session-scoped. See backend/push.py.
PUBLIC_ORIGIN = os.getenv("AXON_PUBLIC_ORIGIN", "").rstrip("/")

# Where the HTML/CSS/JS lives. The same Python server hands these out.
FRONTEND_DIR = ROOT / "frontend"


def persist(key: str, value: str) -> None:
    """
    Write a setting back into .env.

    WHY THIS EXISTS
    ---------------
    The header dropdown used to change the provider in memory only. So you
    could switch to the local model, everything would get ~20x slower, and
    .env would still say "gemini" - leaving no way to work out why. The
    running state and the file that supposedly describes it disagreed, and
    the file was the one you'd go and check.

    Any setting a user can change from the UI has to be written down
    somewhere they can read it. Otherwise "why is it behaving like this?"
    has no answer.
    """
    env_path = ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []

    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

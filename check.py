"""
check.py — test every provider without starting the server.

Run it any time you want to know what's working:

    py check.py            test all providers
    py check.py ollama     test just one
    py check.py --models   list the Gemini models your key can use
    py check.py --tools    list every tool the agent can call

For each provider it does two things: asks whether it's ready, and if it is,
sends one real message and times the reply. That second part matters — a
provider can look "ready" and still fail when it actually runs (Ollama can
run out of RAM; a cloud key can be expired or rate-limited).

This is a tiny version of something you'll build properly in Phase 11: an
EVAL HARNESS. The idea is the same — don't trust that it works, prove it.
"""

import asyncio
import os
import sys
import time

# Load .env before importing anything that reads config.
from backend import config, providers  # noqa: E402

PROMPT = "Say hello in exactly five words."

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[90m", "\033[1m", "\033[0m"


async def test(name: str) -> None:
    print(f"\n{BOLD}{name}{OFF}")

    module = providers.get(name)

    # --- step 1: is it even reachable? ---
    status = await module.check()
    if not status["ready"]:
        print(f"  {RED}not ready{OFF} — {status['detail']}")
        return
    print(f"  {DIM}ready: {status['detail']}{OFF}")

    # --- step 2: does it actually generate? ---
    print(f"  {DIM}asking: {PROMPT}{OFF}")
    start = time.time()
    reply, usage, timing = "", {}, {}

    try:
        async for event in module.stream_reply([{"role": "user", "content": PROMPT}]):
            if event["type"] == "text":
                reply += event["text"]
            elif event["type"] == "done":
                usage = event["usage"]
                timing = event.get("timing_ms", {})
    except Exception as exc:
        print(f"  {RED}FAILED{OFF} — {exc}")
        return

    elapsed = time.time() - start
    out = usage.get("output_tokens", 0)

    print(f"  {GREEN}works{OFF} - {reply.strip()[:120]}")
    line = f"  {DIM}{elapsed:.1f}s total | {usage.get('input_tokens', 0)} in / {out} out"

    # Ollama tells us how much of that was loading the model off disk, which
    # on a RAM-starved laptop is nearly all of it. Worth separating, or you'll
    # think the model is 50x slower than it really is.
    if timing:
        load_s = timing.get("load", 0) / 1000
        gen_s = timing.get("generate", 0) / 1000
        speed = f"{out / gen_s:.1f} tok/s" if gen_s and out else "?"
        line += f" | load {load_s:.1f}s + generate {gen_s:.1f}s ({speed})"
    elif elapsed and out:
        line += f" | {out / elapsed:.1f} tok/s"

    print(line + OFF)


async def list_gemini_models() -> None:
    """
    Ask Google which models this key can actually use.

    Worth having, because Google retires models on a schedule and any tutorial
    older than a few months will name one that no longer exists. Rather than
    guessing, ask.
    """
    import httpx

    if not config.GEMINI_API_KEY:
        print(f"{RED}No GEMINI_API_KEY in .env{OFF}")
        return

    r = httpx.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": config.GEMINI_API_KEY},
        params={"pageSize": 200}, timeout=30,
    )
    if r.status_code != 200:
        print(f"{RED}HTTP {r.status_code}{OFF} {r.text[:200]}")
        return

    skip = ("tts", "image", "vision", "embedding", "robotics", "live",
            "audio", "native", "veo", "lyria", "banana", "aqa", "deep-research")

    print()
    print(f"{BOLD}Chat models on your key{OFF}  "
          f"{DIM}(currently using: {config.GEMINI_MODEL}){OFF}")
    print()
    for m in r.json().get("models", []):
        name = m["name"].replace("models/", "")
        if any(k in name for k in skip):
            continue
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        mark = f"{GREEN} <- current{OFF}" if name == config.GEMINI_MODEL else ""
        print(f"  {name:<34} {DIM}context {m.get('inputTokenLimit', 0):>9,}{OFF}{mark}")

    print()
    print(f"{DIM}Note: a model can be listed here and still be retired for "
          f"new keys. If one 404s, try the next.{OFF}")
    print()


def list_tools() -> None:
    """Show the tool menu exactly as the model receives it."""
    from backend import tools

    colours = {"read": GREEN, "act": BOLD, "danger": RED}
    print()
    print(f"{BOLD}{len(tools.REGISTRY)} tools registered{OFF}")
    print()
    for t in tools.REGISTRY.values():
        args = ", ".join(t.parameters["properties"]) or "-"
        tint = colours.get(t.tier, DIM)
        print(f"  {tint}[{t.tier:<6}]{OFF} {t.name:<18} {DIM}({args}){OFF}")
        print(f"  {DIM}{'':<9}{t.description.splitlines()[0][:66]}{OFF}")
    print()


async def main() -> None:
    if "--tools" in sys.argv:
        list_tools()
        return

    if "--models" in sys.argv:
        await list_gemini_models()
        return

    # A name on the command line means "just that one".
    wanted = sys.argv[1:] or list(providers.REGISTRY)

    print(f"{BOLD}Sid provider check{OFF}")
    print(f"{DIM}active provider in .env: {config.PROVIDER}{OFF}")

    for name in wanted:
        if name not in providers.REGISTRY:
            print(f"\n{RED}unknown provider '{name}'{OFF} — "
                  f"valid: {', '.join(providers.REGISTRY)}")
            continue
        await test(name)

    print()


if __name__ == "__main__":
    # Windows terminals need this to show colour instead of escape gibberish.
    if os.name == "nt":
        os.system("")
    asyncio.run(main())

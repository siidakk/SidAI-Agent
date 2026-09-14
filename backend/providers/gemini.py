"""
providers/gemini.py — Google's free cloud tier.

  Cost:     ₹0 up to a daily request limit. No credit card required.
  Privacy:  ⚠️ on the FREE tier, Google uses your data to improve their
            products. Fine for Phase 1–2 (you're just chatting). Think hard
            before Phase 3, when Sid starts reading your actual email.
  Catch:    rate limits, and you need internet.

Get a key at https://aistudio.google.com/apikey (Google account, no card).

Like ollama.py, this is raw HTTP with httpx — no SDK. Compare the two files:
different URL, different JSON shape, different streaming format... and the
exact same `stream_reply()` output. That is what an abstraction buys you.
"""

import asyncio
import json
import uuid
from typing import AsyncIterator

import httpx

from .. import config, quota

BASE = "https://generativelanguage.googleapis.com/v1beta"
TIMEOUT = httpx.Timeout(120.0, connect=10.0)

# How many times to retry when Google says "busy". 3 tries = waits of 1s + 2s.
MAX_ATTEMPTS = 3

# THE MODEL LADDER
#
# Free-tier Gemini refuses with 429 far more often than people expect, and
# it used to take Sid down with it: 429 was raised straight to the user as
# "free-tier limit reached", mid-sentence, with nothing tried after it.
#
# These models have SEPARATE limits. So when one refuses, the honest move is
# not to report failure, it is to ask the other one - which is what everyone
# means by "a quota ran out, switch to the next".
#
# Probed rather than assumed. The 2.x models below are gone from the free
# API entirely (404, "no longer available"), so listing them would have
# bought a guaranteed-dead rung:
#
#     gemini-3.5-flash-lite   200
#     gemini-3.5-flash        200
#     gemini-2.5-flash        404
#     gemini-2.0-flash        404
FALLBACK_MODELS = ["gemini-3.5-flash-lite", "gemini-3.5-flash"]


def _ladder() -> list[str]:
    """Whatever .env asked for first, then the others, with no duplicates."""
    out = [config.GEMINI_MODEL]
    for name in FALLBACK_MODELS:
        if name not in out:
            out.append(name)
    return out


async def check() -> dict:
    if not config.GEMINI_API_KEY:
        return {
            "ready": False,
            "detail": "No GEMINI_API_KEY in .env — get one free at aistudio.google.com/apikey",
        }
    return {"ready": True, "detail": f"cloud - {config.GEMINI_MODEL}"}


def _tools_for_gemini(schemas: list[dict]) -> list[dict]:
    """
    Our neutral tool schema -> Gemini's dialect.

    Gemini wraps everything in a single "functionDeclarations" list, and calls
    the arguments schema "parameters". Ollama wanted {"type":"function",...};
    Claude wants "input_schema". Three spellings of one idea.
    """
    return [{
        "functionDeclarations": [
            {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["parameters"],
            }
            for s in schemas
        ]
    }]


def _to_gemini(messages: list[dict]) -> list[dict]:
    """
    Translate our message format into Gemini's.

    Ours:    {"role": "assistant", "content": "hi"}
    Gemini:  {"role": "model",     "parts": [{"text": "hi"}]}

    Tool calls and results are parts too, not separate roles:
      call:   {"role": "model", "parts": [{"functionCall": {name, args}}]}
      result: {"role": "user",  "parts": [{"functionResponse": {name, response}}]}

    Note Gemini has no "tool" role at all — a tool result is sent as a USER
    turn. Every provider invented its own vocabulary for the identical
    concept, and reconciling them is most of what a provider file does.
    """
    out = []

    for m in messages:
        if m["role"] == "tool":
            out.append({
                "role": "user",
                "parts": [{
                    "functionResponse": {
                        "name": m["name"],
                        "response": {"result": m["content"]},
                    }
                }],
            })

        elif m["role"] == "assistant" and m.get("tool_calls"):
            parts = []
            if m.get("content"):
                parts.append({"text": m["content"]})
            for c in m["tool_calls"]:
                part = {"functionCall": {"name": c["name"], "args": c["input"]}}
                # Replay the signature Gemini gave us, or it rejects the turn.
                signature = (c.get("meta") or {}).get("thoughtSignature")
                if signature:
                    part["thoughtSignature"] = signature
                parts.append(part)
            out.append({"role": "model", "parts": parts})

        else:
            role = "model" if m["role"] == "assistant" else "user"
            out.append({"role": role, "parts": [{"text": m.get("content", "")}]})

    return out


class _Overloaded(Exception):
    """Raised when Gemini is temporarily busy and the request is worth retrying."""


class _Refused(Exception):
    """
    Raised on 429: this MODEL is rate limited right now.

    Deliberately separate from _Overloaded, because the two want opposite
    responses. Overloaded means "the same model will work shortly, wait".
    Refused means "this model will not work shortly - ask a different one".
    Waiting on a refusal is the slowest possible way to get nowhere.
    """


async def stream_reply(
    messages: list[dict],
    tools: list[dict] | None = None,
    system: str | None = None,
    temperature: float | None = None,
) -> AsyncIterator[dict]:
    """
    Ask Gemini, retrying when its servers are temporarily busy.

    WHY A RETRY LOOP IS NOT OPTIONAL
    --------------------------------
    Free-tier Gemini returns 503 "high demand" at random — the exact same
    request succeeds a few seconds later. It is not your key and not your
    code. Every program that talks to a cloud service has to expect this.

    We wait 1s, then 2s, then 4s between attempts. That doubling is called
    EXPONENTIAL BACKOFF, and it's the standard everywhere: hammering an
    overloaded server instantly just makes the overload worse.

    The retry lives out here rather than inside `_attempt` for one important
    reason — `_attempt` raises _Overloaded *before* it yields any text. Once
    the user has seen half a reply, we can't secretly start over.
    """
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Get a free key at "
            "https://aistudio.google.com/apikey and put it in .env"
        )

    # Ask whichever model is not currently known to be refusing. If they all
    # are, take the first anyway: a rest is a guess about when a limit
    # resets, and being wrong about it must never be why Sid does nothing.
    ladder = _ladder()
    start = quota.first_available(ladder) or ladder[0]
    order = ladder[ladder.index(start):] + ladder[:ladder.index(start)]

    refused = []

    for model in order:
        for attempt in range(MAX_ATTEMPTS):
            try:
                async for event in _attempt(messages, tools, system,
                                            temperature, model=model):
                    yield event
                quota.revive(model)      # it worked - drop any old grudge
                return                   # finished cleanly, stop retrying

            except _Overloaded:
                # "Busy" means THIS model will work shortly, so wait for it.
                if attempt == MAX_ATTEMPTS - 1:
                    break                # out of patience; try the next model
                await asyncio.sleep(2 ** attempt)

            except _Refused:
                # "Rate limited" means this model will NOT work shortly.
                # Waiting is the slowest possible way to get nowhere, so
                # write it down and move straight to the next one.
                quota.rest(model, reason="429 rate limited")
                refused.append(model)
                break

    raise RuntimeError(
        "Every Gemini model is rate limited right now (" +
        ", ".join(refused or order) + "). Free-tier limits usually reset "
        "within a minute or two - this is not a problem with your key."
    )


async def _attempt(
    messages: list[dict],
    tools: list[dict] | None = None,
    system: str | None = None,
    temperature: float | None = None,
    model: str | None = None,
) -> AsyncIterator[dict]:
    """
    One single try. Reads Gemini's SSE stream and yields our own event dicts.

    Nice symmetry worth noticing: Gemini streams to us using SSE, and we
    stream to the browser using SSE (see NOTES/phase-1.md §7). Same format,
    two different hops.
    """
    # NOTE: the old ":streamGenerateContent" endpoint is retired. Streaming is
    # now plain ":generateContent" with alt=sse. If you find a tutorial using
    # streamGenerateContent, it's out of date — you'll get a 404.
    model = model or config.GEMINI_MODEL
    url = f"{BASE}/models/{model}:generateContent"
    payload = {
        "contents": _to_gemini(messages),
        "systemInstruction": {"parts": [{"text": system or config.SYSTEM_PROMPT}]},
    }
    if temperature is not None:
        # Temperature is how much randomness the model is allowed. Gemini
        # defaults to about 1.0, which is right for conversation and wrong
        # for planning: the SAME request would produce a correct plan twice
        # and a flat "sorry, I can't do that" the third time. Measured, not
        # theorised - the eval suite caught it.
        payload["generationConfig"] = {"temperature": temperature}

    if tools:
        payload["tools"] = _tools_for_gemini(tools)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        async with client.stream(
            "POST",
            url,
            params={"alt": "sse"},              # ask for SSE, not a JSON array
            headers={"x-goog-api-key": config.GEMINI_API_KEY},
            json=payload,
        ) as resp:

            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")
                # 5xx means "our fault, try again". 4xx means "your fault,
                # don't bother" — a bad key won't fix itself on retry.
                if resp.status_code in (500, 502, 503, 504):
                    raise _Overloaded()
                if resp.status_code == 429:
                    raise _Refused(model)
                raise RuntimeError(_friendly(resp.status_code, body))

            usage = {"input_tokens": 0, "output_tokens": 0}

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue

                data = json.loads(line[6:])

                # Text lives at candidates[0].content.parts[*].text — but any
                # of those can be missing on a given chunk, so we walk it
                # defensively rather than indexing straight in.
                for cand in data.get("candidates", []):
                    for part in cand.get("content", {}).get("parts", []):
                        # Gemini 3.x streams its internal reasoning as parts
                        # flagged thought:true. That's not the answer — skip it.
                        if part.get("thought"):
                            continue
                        if "text" in part:
                            yield {"type": "text", "text": part["text"]}

                        # Gemini has no id for calls, so we make one. The agent
                        # loop needs it to match each result to its call.
                        elif "functionCall" in part:
                            fc = part["functionCall"]
                            yield {
                                "type": "tool_call",
                                "id": f"call_{uuid.uuid4().hex[:8]}",
                                "name": fc.get("name", ""),
                                "input": fc.get("args", {}) or {},
                                # Gemini 3.x signs each tool call. If we don't
                                # hand the signature back verbatim, the next
                                # request is rejected with a 400. "meta" is our
                                # generic slot for provider-specific baggage
                                # like this - the agent loop just carries it
                                # along without needing to understand it.
                                "meta": {"thoughtSignature": part.get("thoughtSignature")},
                            }

                meta = data.get("usageMetadata")
                if meta:
                    usage = {
                        "input_tokens": meta.get("promptTokenCount", 0),
                        "output_tokens": meta.get("candidatesTokenCount", 0),
                    }

            yield {"type": "done", "usage": usage}


def _friendly(status: int, raw: str) -> str:
    try:
        msg = json.loads(raw)["error"]["message"]
    except Exception:
        msg = raw[:300]

    if status == 400 and "API key not valid" in msg:
        return "That Gemini API key isn't valid. Check GEMINI_API_KEY in .env"
    if status == 429:
        return (
            "Gemini free-tier limit reached for now. Wait a few minutes, or "
            "set GEMINI_MODEL=gemini-3.5-flash-lite in .env (bigger quota)."
        )
    if status == 503:
        return (
            f"Gemini says '{config.GEMINI_MODEL}' is overloaded right now. "
            "Try again in a minute, or set GEMINI_MODEL=gemini-3.5-flash."
        )
    if status == 404:
        # Google retires models regularly - a name from a tutorial written six
        # months ago may simply not exist any more. Don't guess a replacement;
        # tell the user how to see the real list.
        return (
            f"Model '{config.GEMINI_MODEL}' isn't usable on your key "
            "(it may have been retired). Run:  py check.py --models  "
            "to see what you can actually use."
        )
    return f"Gemini error {status}: {msg}"

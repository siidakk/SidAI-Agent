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

from .. import config

BASE = "https://generativelanguage.googleapis.com/v1beta"
TIMEOUT = httpx.Timeout(120.0, connect=10.0)

# How many times to retry when Google says "busy". 3 tries = waits of 1s + 2s.
MAX_ATTEMPTS = 3


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

    for attempt in range(MAX_ATTEMPTS):
        try:
            async for event in _attempt(messages, tools, system, temperature):
                yield event
            return                       # finished cleanly, stop retrying

        except _Overloaded:
            if attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(
                    f"Gemini's servers stayed busy after {MAX_ATTEMPTS} tries. "
                    "Wait a minute, or set GEMINI_MODEL=gemini-3.5-flash-lite "
                    "in .env — the smaller models are usually less contended."
                )
            await asyncio.sleep(2 ** attempt)


async def _attempt(
    messages: list[dict],
    tools: list[dict] | None = None,
    system: str | None = None,
    temperature: float | None = None,
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
    url = f"{BASE}/models/{config.GEMINI_MODEL}:generateContent"
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

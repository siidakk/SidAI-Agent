"""
providers/ollama.py — talk to a model running on YOUR OWN computer.

Ollama is a program that downloads AI models and runs them locally. It puts a
little web server on http://127.0.0.1:11434, and we send it JSON. That's it.

  Cost:     free, forever
  Privacy:  perfect — nothing leaves your laptop, works with Wi-Fi off
  Catch:    it uses YOUR RAM, and small models are noticeably less clever

Note there is no SDK here, no library, no API key. We're just POSTing JSON to
a URL with httpx. That is genuinely all an "API call" ever is.
"""

import json
import uuid
from typing import AsyncIterator

import httpx

from .. import config

# Local generation on a CPU is slow, so we allow a long read timeout.
# But connecting should be instant — if Ollama isn't running we want to know
# in 5 seconds, not 5 minutes.
TIMEOUT = httpx.Timeout(600.0, connect=5.0)


async def check() -> dict:
    """Is Ollama running, and is our model downloaded?"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{config.OLLAMA_HOST}/api/tags")
            r.raise_for_status()
            names = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return {
            "ready": False,
            "detail": "Ollama isn't running. Open the Ollama app, or run: ollama serve",
        }

    if config.OLLAMA_MODEL not in names:
        return {
            "ready": False,
            "detail": f"Model not downloaded. Run: ollama pull {config.OLLAMA_MODEL}",
        }

    return {
        "ready": True,
        "detail": f"local - {config.OLLAMA_MODEL}",
        "warming": not warm,
    }


# Set once the model has been primed. /api/health reports it so the UI can
# show "warming up" instead of pretending everything is instant.
warm = False


async def warmup(tool_schemas: list[dict] | None = None) -> None:
    """
    Prime the model before the user asks anything.

    WHY THIS EXISTS - the single biggest speed fix in the project.

    Ollama caches the processed prompt PREFIX (system prompt + tool list).
    Reading that prefix for the first time is brutally slow on a CPU:
    measured here, 780 tokens of tool schema took **46 seconds**. Every
    request after that reuses the cache and takes ~1 second.

    So the first question of the day cost 68s, and every one after it 5s.
    That's not "the model is slow" - that's one big one-off bill landing on
    whoever asks first.

    The fix is to pay it in the background at startup, while you're still
    looking at the UI, with a throwaway question and num_predict=1 (generate
    exactly one token - we want the prefix cached, not an answer).

    Measured: cold first question 68s -> warmed first question 7.0s.

    Two things keep the cache alive afterwards:
      - identical prefix every time (never vary the system prompt or tools)
      - a long OLLAMA_KEEP_ALIVE, or the model unloads and it all goes cold
    """
    global warm

    payload = {
        "model": config.OLLAMA_MODEL,
        "stream": False,
        "keep_alive": config.OLLAMA_KEEP_ALIVE,
        "messages": [
            {"role": "system", "content": config.SYSTEM_PROMPT},
            {"role": "user", "content": "hi"},
        ],
        "options": {"num_predict": 1},
    }
    if tool_schemas:
        payload["tools"] = _tools_for_ollama(tool_schemas)

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            await client.post(f"{config.OLLAMA_HOST}/api/chat", json=payload)
        warm = True
    except Exception:
        # Warming is an optimisation, never a requirement. If it fails the
        # app still works - the first question is just slow again.
        warm = False


def _tools_for_ollama(schemas: list[dict]) -> list[dict]:
    """
    Our neutral tool schema -> Ollama's dialect.

    Ollama copied OpenAI's shape: every tool is wrapped in
    {"type": "function", "function": {...}}. Gemini and Claude each chose
    something different. Same three facts, three spellings.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["parameters"],
            },
        }
        for s in schemas
    ]


def _to_ollama(messages: list[dict], system: str | None = None) -> list[dict]:
    """
    Our internal message format -> Ollama's.

    Our format (shared by all providers, defined in llm.py):
        {"role": "user",      "content": "..."}
        {"role": "assistant", "content": "...", "tool_calls": [{id, name, input}]}
        {"role": "tool",      "name": "...", "content": "the result"}
    """
    out = [{"role": "system", "content": system or config.SYSTEM_PROMPT}]

    for m in messages:
        if m["role"] == "tool":
            # Ollama wants tool output as a plain message with role "tool".
            out.append({"role": "tool", "content": m["content"]})

        elif m["role"] == "assistant" and m.get("tool_calls"):
            out.append({
                "role": "assistant",
                "content": m.get("content", ""),
                "tool_calls": [
                    {"function": {"name": c["name"], "arguments": c["input"]}}
                    for c in m["tool_calls"]
                ],
            })

        else:
            out.append({"role": m["role"], "content": m.get("content", "")})

    return out


async def stream_reply(
    messages: list[dict],
    tools: list[dict] | None = None,
    system: str | None = None,
    temperature: float | None = None,
) -> AsyncIterator[dict]:
    """
    Ollama's /api/chat streams NDJSON — one complete JSON object per line.
    (Not SSE. Every provider picks its own format; part of this file's job is
    to hide that difference from the rest of Sid.)

    Each line looks like:
        {"message":{"role":"assistant","content":"Hel"},"done":false}
    and the final line carries the token counts:
        {"done":true,"prompt_eval_count":26,"eval_count":198}
    """
    payload = {
        "model": config.OLLAMA_MODEL,
        "stream": True,
        "messages": _to_ollama(messages, system),
        # How long to hold the model in RAM afterwards. See config.py.
        "keep_alive": config.OLLAMA_KEEP_ALIVE,
    }
    if temperature is not None:
        # See the note in gemini.py: low temperature for planning, because a
        # plan should be the same plan every time you ask.
        payload["options"] = {"temperature": temperature}

    if tools:
        payload["tools"] = _tools_for_ollama(tools)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        async with client.stream(
            "POST", f"{config.OLLAMA_HOST}/api/chat", json=payload
        ) as resp:

            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")
                raise RuntimeError(_friendly(body))

            async for line in resp.aiter_lines():
                if not line.strip():
                    continue

                data = json.loads(line)

                # Ollama reports errors mid-stream as a normal 200 line.
                if "error" in data:
                    raise RuntimeError(_friendly(data["error"]))

                message = data.get("message", {})

                if message.get("content"):
                    yield {"type": "text", "text": message["content"]}

                # A tool call arrives complete, not streamed character by
                # character. Ollama gives no id, so we make one — the agent
                # loop needs it to match results back to calls.
                for call in message.get("tool_calls") or []:
                    fn = call.get("function", {})
                    args = fn.get("arguments", {})
                    if isinstance(args, str):        # some builds send JSON text
                        args = json.loads(args or "{}")
                    yield {
                        "type": "tool_call",
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": fn.get("name", ""),
                        "input": args,
                    }

                if data.get("done"):
                    # Ollama reports nanosecond timings. Splitting "load" from
                    # "generate" matters a lot here: on a RAM-starved laptop
                    # almost all the wait is loading the model, not thinking.
                    ns = 1_000_000
                    yield {
                        "type": "done",
                        "usage": {
                            "input_tokens": data.get("prompt_eval_count", 0),
                            "output_tokens": data.get("eval_count", 0),
                        },
                        "timing_ms": {
                            "load": data.get("load_duration", 0) // ns,
                            "generate": data.get("eval_duration", 0) // ns,
                        },
                    }


def _friendly(raw: str) -> str:
    """Turn Ollama's wall-of-text errors into something a human can act on."""
    low = raw.lower()
    if "out-of-memory" in low or "failed to allocate" in low:
        return (
            "Not enough free RAM to load the model. Close Chrome and any other "
            "heavy apps, then try again. (Or switch to the free cloud provider: "
            "set AXON_PROVIDER=gemini in .env)"
        )
    if "does not support tools" in low:
        return (
            f"'{config.OLLAMA_MODEL}' can't use tools. Try a model that can: "
            f"ollama pull llama3.2:3b  (or qwen2.5:3b)"
        )
    if "not found" in low:
        return f"Model not downloaded. Run:  ollama pull {config.OLLAMA_MODEL}"
    return f"Ollama error: {raw[:300]}"

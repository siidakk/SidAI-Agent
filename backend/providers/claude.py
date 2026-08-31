"""
providers/claude.py — Anthropic's Claude. The paid option.

  Cost:     ~₹1 per exchange. Needs credit on the account.
  Privacy:  paid API data is not used for training.
  Catch:    it costs money.

Keep this here even if you never use it. When Phase 5 arrives and a 3B local
model starts producing broken plans, flipping AXON_PROVIDER=claude is how you
find out whether the bug is your code or the model's brain. That diagnostic is
worth a few rupees.

NOTE — this is the one provider that uses an official SDK instead of raw
httpx. Compare it with ollama.py and gemini.py: less code here, but also less
visible. Both approaches are normal; use the SDK when a good one exists, raw
HTTP when it doesn't.
"""

from typing import AsyncIterator

from .. import config

_client = None


def _get_client():
    """Built on first use, so a missing key doesn't crash the whole server."""
    global _client
    if _client is None:
        import anthropic  # imported here so the package stays optional

        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Add it to .env, or use a free "
                "provider: set AXON_PROVIDER=ollama or gemini"
            )
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


async def check() -> dict:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return {"ready": False, "detail": "run: py -m pip install anthropic"}

    if not config.ANTHROPIC_API_KEY:
        return {"ready": False, "detail": "No ANTHROPIC_API_KEY in .env"}
    return {"ready": True, "detail": f"cloud - {config.ANTHROPIC_MODEL}"}


def _tools_for_claude(schemas: list[dict]) -> list[dict]:
    """
    Our neutral tool schema -> Anthropic's dialect.

    Third spelling of the same idea: Anthropic calls the arguments schema
    "input_schema". Ollama said "parameters" inside a "function" wrapper;
    Gemini said "parameters" inside "functionDeclarations".
    """
    return [
        {
            "name": s["name"],
            "description": s["description"],
            "input_schema": s["parameters"],
        }
        for s in schemas
    ]


def _to_claude(messages: list[dict]) -> list[dict]:
    """
    Our internal message format -> Anthropic's.

    Anthropic uses typed content BLOCKS rather than special roles:
      call:   assistant message containing a {"type": "tool_use"} block
      result: user message containing a {"type": "tool_result"} block

    Like Gemini, a tool result travels as a USER turn. Unlike Gemini,
    Anthropic issues real call ids and requires the result to quote the
    matching `tool_use_id` — so here we pass the id straight through instead
    of inventing one.
    """
    out = []

    for m in messages:
        if m["role"] == "tool":
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": m["content"],
                }],
            })

        elif m["role"] == "assistant" and m.get("tool_calls"):
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for c in m["tool_calls"]:
                blocks.append({
                    "type": "tool_use",
                    "id": c["id"],
                    "name": c["name"],
                    "input": c["input"],
                })
            out.append({"role": "assistant", "content": blocks})

        else:
            out.append({"role": m["role"], "content": m.get("content", "")})

    return out


async def stream_reply(
    messages: list[dict],
    tools: list[dict] | None = None,
    system: str | None = None,
    temperature: float | None = None,
) -> AsyncIterator[dict]:
    client = _get_client()

    kwargs = {
        "model": config.ANTHROPIC_MODEL,
        "max_tokens": config.MAX_TOKENS,
        "system": system or config.SYSTEM_PROMPT,   # a separate field, unlike Ollama
        "messages": _to_claude(messages),
    }
    if temperature is not None:
        # See the note in gemini.py.
        kwargs["temperature"] = temperature

    if tools:
        kwargs["tools"] = _tools_for_claude(tools)

    async with client.messages.stream(**kwargs) as stream:

        # text_stream gives us the words as they arrive, skipping the protocol
        # noise. Tool calls aren't in it — they're structured data, not text.
        async for chunk in stream.text_stream:
            yield {"type": "text", "text": chunk}

        # Once streaming ends, the SDK has assembled the complete message.
        # Tool calls live in there as typed content blocks.
        final = await stream.get_final_message()

        for block in final.content:
            if block.type == "tool_use":
                yield {
                    "type": "tool_call",
                    "id": block.id,          # Anthropic's real id, not ours
                    "name": block.name,
                    "input": block.input,
                }

        yield {
            "type": "done",
            "usage": {
                "input_tokens": final.usage.input_tokens,
                "output_tokens": final.usage.output_tokens,
            },
        }

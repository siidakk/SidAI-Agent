"""
providers/ — one file per AI backend, all with the same shape.

THE IDEA (this is the lesson of this folder)
--------------------------------------------
Every provider file exports exactly two functions:

    async def check()                    -> {"ready": bool, "detail": str}
    async def stream_reply(messages, tools, system, temperature)
                                     -> yields {"type": "text"|"done", ...}

Nothing else in Sid knows or cares which one is running. Ollama streams
NDJSON over localhost; Gemini streams SSE from Google; Claude uses an
official SDK. Three completely different mechanisms — one identical result.

That is called an INTERFACE (or a "contract"). It's why you can switch from a
free local model today to a paid frontier model in Phase 5 by editing one
line of .env, without touching main.py, app.js, or anything you build later.

Adding a fourth provider (Groq, OpenRouter, a model on your own server) means
writing ~60 lines here and adding one entry to the dict below. Nothing else.
"""

from . import claude, gemini, ollama

# name in .env  ->  the module that implements it
REGISTRY = {
    "ollama": ollama,
    "gemini": gemini,
    "claude": claude,
}


def get(name: str):
    """Look up a provider module by name, with a helpful error if it's wrong."""
    if name not in REGISTRY:
        raise RuntimeError(
            f"Unknown AXON_PROVIDER '{name}'. "
            f"Valid options: {', '.join(REGISTRY)}"
        )
    return REGISTRY[name]

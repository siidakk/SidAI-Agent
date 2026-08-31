"""
tools/ — the things Sid can actually DO.

THE BIG IDEA OF PHASE 2
-----------------------
Until now the model could only produce words. Now we hand it a menu of real
Python functions. When you ask something it can't answer from memory, it
doesn't guess — it replies "run `get_time()` and tell me the answer."

Our code runs the function, feeds the result back, and asks again. That loop
lives in `llm.py`. This folder is just the menu.

HOW THE MODEL KNOWS WHAT'S ON THE MENU
--------------------------------------
Every request includes a JSON description of each tool: its name, what it
does, and what arguments it takes. Writing that JSON by hand for every tool
would be miserable and would drift out of sync with the code. So we generate
it from the function itself — its name, its type hints, and its docstring.

Which means: **the docstring is not a comment. It is the prompt.** If the
model keeps calling a tool wrongly, the fix is almost always a clearer
docstring, not more code.
"""

import inspect
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

# Python type  ->  JSON Schema type. This is the whole translation table.
_JSON_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


@dataclass
class Tool:
    """One callable tool: the function, its description for the model, and how risky it is."""

    fn: Callable
    name: str
    description: str
    parameters: dict          # JSON Schema for the arguments
    tier: str                 # "read" | "act" | "danger"

    @property
    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.fn)


# Every @tool-decorated function ends up in here, keyed by name.
REGISTRY: dict[str, Tool] = {}


def _parse_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """
    Split a docstring into (description, {param_name: param_description}).

    We use the standard "Google style" format:

        Play a song on YouTube.

        Args:
            query: What to search for, e.g. "shape of you"

    Everything before `Args:` becomes the tool's description. Each indented
    `name: text` line under it describes one argument.
    """
    doc = inspect.cleandoc(doc or "")
    parts = re.split(r"\n\s*Args:\s*\n", doc, maxsplit=1)

    description = parts[0].strip()
    params: dict[str, str] = {}

    if len(parts) > 1:
        for line in parts[1].splitlines():
            match = re.match(r"\s*(\w+)\s*:\s*(.+)", line)
            if match:
                params[match.group(1)] = match.group(2).strip()

    return description, params


def tool(tier: str = "read") -> Callable:
    """
    Decorator that registers a function as a tool.

    Args:
        tier: How risky this tool is.
              "read"   - only looks at things. Always safe to run.
              "act"    - changes something outside Sid (opens a browser tab).
                         Reversible, so we allow it without asking.
              "danger" - irreversible or costs money. BLOCKED until Phase 7
                         builds the approval flow.

    The tier isn't enforced much yet — that's Phase 7's job. But tagging every
    tool from the start means that when we do build approvals, we don't have
    to go back and audit fifty functions to work out which ones are scary.
    """
    if tier not in ("read", "act", "danger"):
        raise ValueError(f"bad tier {tier!r}: use read, act or danger")

    def decorator(fn: Callable) -> Callable:
        description, param_docs = _parse_docstring(fn.__doc__)

        if not description:
            raise ValueError(
                f"Tool {fn.__name__} has no docstring. The model reads that "
                f"docstring to decide when to use the tool — it isn't optional."
            )

        properties: dict[str, Any] = {}
        required: list[str] = []

        for pname, param in inspect.signature(fn).parameters.items():
            ptype = _JSON_TYPES.get(param.annotation, "string")

            properties[pname] = {
                "type": ptype,
                "description": param_docs.get(pname, pname),
            }

            # No default value means the model MUST supply it.
            if param.default is inspect.Parameter.empty:
                required.append(pname)

        REGISTRY[fn.__name__] = Tool(
            fn=fn,
            name=fn.__name__,
            description=description,
            parameters={
                "type": "object",
                "properties": properties,
                "required": required,
            },
            tier=tier,
        )
        return fn

    return decorator


def schemas() -> list[dict]:
    """
    The tool menu, in the neutral shape our providers translate from.

    Each provider reshapes this into its own dialect — see the `_tools_for_*`
    functions in providers/. They all carry the same three facts: name, what
    it does, what arguments it takes.
    """
    return [
        {"name": t.name, "description": t.description, "parameters": t.parameters}
        for t in REGISTRY.values()
    ]


async def run(name: str, arguments: dict, approve=None, task_id=None) -> str:
    """
    Execute one tool call and return its result as a string.

    IMPORTANT: this NEVER raises. If a tool explodes, we return the error text
    so the model can read it and try something else. An agent that crashes on
    the first bad argument is useless; an agent that reads "no such folder"
    and corrects itself is the whole point.
    """
    spec = REGISTRY.get(name)

    if spec is None:
        # The model hallucinated a tool name. Tell it plainly what exists.
        return (
            f"Error: no tool called '{name}'. "
            f"Available tools: {', '.join(REGISTRY)}"
        )

    # DANGEROUS TOOLS STOP HERE AND ASK.
    #
    # `approve` is an async callback supplied by the agent loop. It shows the
    # user what is about to happen and waits for a tap. If nobody passed one
    # in - a script, a test, a background job - we refuse. Fail closed: the
    # absence of a way to ask is never permission.
    # Imported here rather than at module scope: settings and audit both
    # import config, and tools/ sits deep inside that chain. A top-level
    # import would be a cycle.
    from .. import audit, settings

    # ---- DRY RUN --------------------------------------------------------
    if spec.tier != "read" and settings.get("dry_run"):
        preview = f"[dry run] Would have called {name}({arguments}). Nothing ran."
        audit.record(name, spec.tier, arguments, preview,
                     approved="dry-run", task_id=task_id)
        return preview

    # ---- POLICY ---------------------------------------------------------
    # `danger` always asks. `act` asks too, if you've turned that on.
    needs_approval = spec.tier == "danger" or (
        spec.tier == "act" and settings.get("confirm_act")
    )

    if needs_approval:
        if approve is None:
            return (
                f"Error: '{name}' needs approval, and there is no one here to "
                f"ask. Not running it."
            )
        if not await approve(name, arguments):
            denied = (
                f"The user did not approve '{name}', so it was not run. "
                f"Do not try again unless they ask. Offer an alternative if "
                f"there is a safe one."
            )
            audit.record(name, spec.tier, arguments, denied, ok=False,
                         approved="denied", task_id=task_id)
            return denied

    started = time.time()
    ok = True

    try:
        result = spec.fn(**arguments)
        if spec.is_async:
            result = await result
        result = str(result)

    except TypeError as exc:
        # Usually means the model sent wrong or missing arguments.
        ok = False
        result = (f"Error calling {name}: {exc}. "
                  f"Expected: {spec.parameters['properties']}")
    except Exception as exc:
        ok = False
        result = f"Error in {name}: {type(exc).__name__}: {exc}"

    # EVERY call is logged, failures included. A log that records only
    # successes is worse than no log: it tells a comforting story instead of
    # what happened.
    audit.record(
        name, spec.tier, arguments, result, ok=ok,
        approved="granted" if needs_approval else None,
        task_id=task_id, ms=int((time.time() - started) * 1000),
    )
    return result


# Importing these modules is what actually runs the @tool decorators and
# fills REGISTRY. Add a new file here and its tools appear automatically.
from . import (basic, browser, computer, files, gcal,  # noqa: E402,F401
               gmail, media, memory_tools, schedule_tools, web)

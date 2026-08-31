"""
evals/run.py — does Sid still work?

    py evals/run.py                  run everything
    py evals/run.py --case schedule  run cases matching a word
    py evals/run.py --save           write the result to evals/history.jsonl

WHY THIS EXISTS
---------------
Every phase so far ended with me typing a few questions and reading the
answers. That works until it doesn't. Two things go wrong:

  1. You test what you just built, and never re-test what you built in
     Phase 3. So a prompt change in Phase 9 quietly breaks Gmail and nobody
     notices for a week.

  2. You remember the failures you saw. You do not remember the ones you
     didn't look for.

Every case in cases.json is a bug that actually happened. That is what makes
this suite worth running rather than reassuring:

> **An eval written from imagination tests what you already thought of. One
> written from your failures tests what actually breaks.**

WHAT THIS IS NOT
----------------
It is not a unit test suite, and it cannot be. The same question asked twice
gets differently-worded answers, so asserting on exact text would fail
constantly for no reason.

So it asserts on things that are actually stable:

    which TOOLS ran          (behaviour, not phrasing)
    how many STEPS           (did the plan stay sane)
    substrings that must or must not appear

"Must not contain 'not implemented'" is a real assertion. "Must equal 'The
time is 4:32 PM'" is not - it would break the moment the clock moved.

READING THE RESULT
------------------
A failure is information, not a verdict. Some cases will fail on a small
model and pass on a bigger one, and that is a genuine finding about which
model you can rely on - not a bug in the suite.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import config          # noqa: E402  (after sys.path)

BASE = f"http://127.0.0.1:{config.PORT}"
CASES = Path(__file__).parent / "cases.json"
HISTORY = Path(__file__).parent / "history.jsonl"


# ==========================================================================
#  Talking to a running Sid
# ==========================================================================

def ask(text: str, timeout: int = 180) -> dict:
    """
    Send one request and collect everything that came back.

    Against the REAL running server, not an imported function. An eval that
    bypasses the server would pass while the thing you actually use is
    broken - which is exactly how the browser tool looked healthy for an hour
    while failing on every real request.
    """
    body = json.dumps({
        "messages": [{"role": "user", "content": text}],
        "conversation": "eval",
    }).encode()
    request = urllib.request.Request(
        BASE + "/api/chat", data=body,
        headers={"Content-Type": "application/json"})

    tools: list[str] = []
    reply: list[str] = []
    error = ""
    started = time.time()

    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue

            kind = event.get("type")
            if kind in ("step_start", "tool_call"):
                tools.append(event.get("tool", ""))
            elif kind == "text":
                reply.append(event["text"])
            elif kind == "error":
                error = str(event.get("message", ""))

    return {
        "tools": tools,
        "answer": "".join(reply).strip(),
        "error": error,
        "seconds": round(time.time() - started, 1),
    }


# ==========================================================================
#  Checking one case
# ==========================================================================

def check(case: dict, result: dict) -> list[str]:
    """Return the reasons this case failed. Empty list means it passed."""
    problems = []
    tools = result["tools"]
    answer = result["answer"].lower()

    if result["error"]:
        problems.append(f"server error: {result['error'][:80]}")

    for required in case.get("tools", []):
        if required not in tools:
            problems.append(f"did not call {required} (called: {tools or 'nothing'})")

    for banned in case.get("forbid", []):
        if banned in tools:
            problems.append(f"called {banned}, which it should not have")

    for needle in case.get("must", []):
        if needle.lower() not in answer:
            problems.append(f"answer is missing {needle!r}")

    for needle in case.get("must_not", []):
        if needle.lower() in answer:
            problems.append(f"answer contains {needle!r}")

    limit = case.get("max_steps")
    if limit is not None and len(tools) > limit:
        problems.append(f"{len(tools)} steps, expected at most {limit}")

    # An empty answer is a failure even when nothing else complained. The
    # user is left staring at nothing, which is the worst outcome of all and
    # would otherwise pass every assertion above.
    if not answer and not case.get("allow_empty"):
        problems.append("empty answer")

    return problems


def cleanup(case: dict) -> None:
    """Undo anything a case created, so running it twice behaves the same."""
    if case.get("cleanup") != "triggers":
        return
    try:
        data = json.loads(urllib.request.urlopen(BASE + "/api/triggers",
                                                 timeout=15).read())
        for trigger in data["triggers"]:
            urllib.request.urlopen(urllib.request.Request(
                f"{BASE}/api/triggers/{trigger['id']}", method="DELETE"),
                timeout=15)
    except Exception:
        pass


# ==========================================================================
#  Running the lot
# ==========================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Run Sid's eval suite.")
    parser.add_argument("--case", help="only cases whose name contains this")
    parser.add_argument("--save", action="store_true",
                        help="append the result to evals/history.jsonl")
    args = parser.parse_args()

    try:
        health = json.loads(urllib.request.urlopen(BASE + "/api/health",
                                                   timeout=20).read())
    except Exception:
        print(f"Sid isn't running on {BASE}.")
        print("Start it first:  py -m uvicorn backend.main:app "
              f"--port {config.PORT}")
        return 2

    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    if args.case:
        cases = [c for c in cases if args.case.lower() in c["name"].lower()]
        if not cases:
            print(f"No case matches {args.case!r}.")
            return 2

    print(f"Sid: {health['provider']} · {health.get('detail','')} · "
          f"{health['tools']} tools")
    print(f"Running {len(cases)} case(s)\n")

    passed = failed = 0
    records = []

    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['name']}")
        print(f"        ask: {case['ask'][:66]}")

        try:
            result = ask(case["ask"])
        except Exception as exc:
            print(f"        FAIL  request blew up: {type(exc).__name__}: {exc}\n")
            failed += 1
            records.append({"name": case["name"], "ok": False,
                            "problems": [str(exc)[:120]]})
            continue

        problems = check(case, result)
        cleanup(case)

        tools = ", ".join(result["tools"]) or "none"
        if problems:
            failed += 1
            print(f"        FAIL  ({result['seconds']}s, tools: {tools})")
            for problem in problems:
                print(f"              - {problem}")
            print(f"              answer: {result['answer'][:100]!r}")
        else:
            passed += 1
            print(f"        pass  ({result['seconds']}s, tools: {tools})")
        print()

        records.append({"name": case["name"], "ok": not problems,
                        "problems": problems, "tools": result["tools"],
                        "seconds": result["seconds"],
                        "answer": result["answer"][:200]})

    total = passed + failed
    print("=" * 62)
    print(f"{passed}/{total} passed" + (f"  ·  {failed} FAILED" if failed else ""))

    if args.save:
        HISTORY.parent.mkdir(exist_ok=True)
        with HISTORY.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "at": datetime.now(timezone.utc).isoformat(),
                "provider": health["provider"],
                "model": health.get("detail", ""),
                "passed": passed, "failed": failed,
                "cases": records,
            }) + "\n")
        print(f"saved to {HISTORY}")

    # A non-zero exit code means a script or CI can act on this, rather than
    # a human having to read the output and decide.
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

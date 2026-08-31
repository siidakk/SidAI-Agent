"""
tools/basic.py — the simple ones. Start here.

`get_time` looks trivial, and it is. But it teaches the single most important
thing about tools: **the model genuinely does not know the time.** It was
trained months ago and frozen. Ask it the date and it will confidently invent
one. Give it this tool and it stops guessing.

That is the whole pattern. Tools exist to give the model access to things it
cannot possibly know: the current moment, your files, the live web, your
calendar.
"""

import ast
import operator
import platform
import shutil
from datetime import datetime

from . import tool


@tool(tier="read")
def get_time() -> str:
    """Get the current date and time on the user's computer.

    Use this whenever the user asks about the date, the time, what day it is,
    or anything involving "today", "tomorrow" or "now".
    """
    now = datetime.now().astimezone()
    return now.strftime("%A, %d %B %Y, %I:%M %p (%Z)")


# --------------------------------------------------------------------------
#  calculate
# --------------------------------------------------------------------------
# Only these operations are allowed. Anything else is rejected.
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _evaluate(node: ast.AST) -> float:
    """Walk the parsed expression tree, allowing only arithmetic."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_evaluate(node.left), _evaluate(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_evaluate(node.operand))
    raise ValueError("only + - * / // % ** and numbers are allowed")


@tool(tier="read")
def calculate(expression: str) -> str:
    """Do exact arithmetic. Language models are unreliable at maths, so use
    this for any calculation rather than working it out yourself.

    Args:
        expression: A maths expression, e.g. "1250 * 12" or "(45+55)/4"
    """
    # WHY NOT JUST USE eval()?
    # Because eval("__import__('os').system('del /f /s C:\\\\')") is a valid
    # Python expression, and the text we're evaluating came from a language
    # model, which got it from the internet. Never eval() untrusted input.
    #
    # Instead we PARSE the string into a syntax tree and walk it ourselves,
    # allowing only numbers and arithmetic operators. Anything else — function
    # calls, names, imports, attribute access — simply has no branch to match
    # and gets rejected.
    try:
        tree = ast.parse(expression, mode="eval")
        result = _evaluate(tree.body)
    except Exception as exc:
        return f"Could not calculate '{expression}': {exc}"

    # Show 12.0 as 12, but keep real decimals.
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return f"{expression} = {result}"


@tool(tier="read")
def system_info() -> str:
    """Get information about the user's computer: operating system, free disk
    space, and how much memory is available.

    Use this if the user asks about their laptop, storage, or memory.
    """
    total, used, free = shutil.disk_usage("C:/" if platform.system() == "Windows" else "/")

    lines = [
        f"OS: {platform.system()} {platform.release()}",
        f"Machine: {platform.machine()}",
        f"Disk: {free / 1e9:.0f} GB free of {total / 1e9:.0f} GB",
    ]

    # RAM has no standard library call, so we ask Windows directly. Wrapped in
    # try/except because a tool must never crash the agent — see tools/run().
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemStatus()
        status.dwLength = ctypes.sizeof(_MemStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        lines.append(
            f"RAM: {status.ullAvailPhys / 1e9:.1f} GB free of "
            f"{status.ullTotalPhys / 1e9:.1f} GB ({status.dwMemoryLoad}% used)"
        )
    except Exception:
        pass

    return "\n".join(lines)

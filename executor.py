"""
executor.py — Sandboxed code execution with safety checks and timing.
"""

from __future__ import annotations
import io
import re
import time
from contextlib import redirect_stdout
from typing import Any

import pandas as pd
import numpy as np

from utils import get_logger

log = get_logger("executor")

# ---------------------------------------------------------------------------
# Safety — block dangerous constructs.
# Uses function-call patterns (exec(), eval()) not bare words,
# so strings like "no exec/eval allowed" don't trigger false positives.
# ---------------------------------------------------------------------------

_BLOCKED = re.compile(
    r"\bimport\s+\w"            # import statement
    r"|\b__import__\s*\("       # __import__() dynamic import
    r"|\bopen\s*\("             # file open
    r"|\bexec\s*\("             # exec() call
    r"|\beval\s*\("             # eval() call
    r"|\bcompile\s*\("          # compile()
    r"|\bbreakpoint\s*\("       # breakpoint()
    r"|os\.\w"                  # os module access
    r"|sys\.\w"                 # sys module access
    r"|\bsubprocess\b"
    r"|\bshutil\b"
    r"|\bsocket\b"
    r"|\bpathlib\b"
    r"|\bbuiltins\b"
    r"|\b__\w+__\s*\("          # dunder calls like __class__()
)

_SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "filter": filter, "float": float, "int": int,
    "isinstance": isinstance, "len": len, "list": list, "map": map,
    "max": max, "min": min, "print": print, "range": range, "round": round,
    "set": set, "sorted": sorted, "str": str, "sum": sum, "tuple": tuple,
    "type": type, "zip": zip, "hasattr": hasattr, "getattr": getattr,
    "ValueError": ValueError, "KeyError": KeyError, "TypeError": TypeError,
}


def safe_execute(code: str, df: pd.DataFrame) -> tuple[str, str | None, float]:
    """
    Execute pandas code in a restricted sandbox.

    Returns:
        (output_string, error_string_or_None, elapsed_seconds)
    """
    m = _BLOCKED.search(code)
    if m:
        msg = f"Blocked pattern in generated code: '{m.group()}'"
        log.warning("executor: %s", msg)
        return "", msg, 0.0

    sandbox: dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        "pd": pd,
        "np": np,
        "df": df.copy(),
    }
    local_vars: dict = {}
    buf = io.StringIO()

    log.debug("executor: running %d-char code block", len(code))
    t0 = time.perf_counter()

    try:
        with redirect_stdout(buf):
            exec(code, sandbox, local_vars)  # noqa: S102
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        err = f"{type(exc).__name__}: {exc}"
        log.debug("executor: error after %.2fs — %s", elapsed, err)
        return "", err, elapsed

    elapsed = time.perf_counter() - t0
    log.debug("executor: success in %.2fs", elapsed)

    captured = buf.getvalue().strip()
    result   = local_vars.get("result")

    return format_result(result, captured), None, elapsed


def format_result(result: Any, captured_stdout: str = "") -> str:
    """Convert execution output to a clean, readable string."""
    if result is not None:
        if isinstance(result, pd.DataFrame):
            if result.empty:
                return "(empty DataFrame)"
            return result.to_string(index=True)
        if isinstance(result, pd.Series):
            if result.empty:
                return "(empty Series)"
            return result.to_string()
        if isinstance(result, dict):
            lines = [f"  {k}: {v}" for k, v in result.items()]
            return "\n".join(lines)
        return str(result)

    if captured_stdout:
        return captured_stdout

    return "(code ran successfully — no `result` variable was set)"

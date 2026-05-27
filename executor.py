"""
executor.py — Sandboxed code execution with safety checks and timing.

Two execution shapes:

  safe_execute(code, df=...)
      Legacy single-DataFrame mode. Injects `df`, `pd`, `np`.

  safe_execute(code, workspace=...)
      Workspace mode. Injects:
          workspace       → core.Workspace
          spreadsheets    → workspace.spreadsheets
          documents       → workspace.documents
          tables          → workspace.tables
          df              → active spreadsheet's DataFrame (if any)
          doc             → active document (if any)
          memory          → workspace.memory
          register_table  → workspace.register_dataframe_as_table  (helper)
          pd, np          → pandas, numpy

Safety: the `_BLOCKED` regex is the trust boundary. Mutations to disk
happen exclusively through workspace object methods (`obj.save()`,
`DocumentObject.replace_text() + .save()`), which back up before writing.
"""

from __future__ import annotations
import io
import re
import time
from contextlib import redirect_stdout
from typing import TYPE_CHECKING, Any

import pandas as pd
import numpy as np

from utils import get_logger

if TYPE_CHECKING:
    from core import Workspace

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
    "Exception": Exception, "RuntimeError": RuntimeError,
}


def _build_sandbox(
    *,
    df: pd.DataFrame | None,
    workspace: "Workspace | None",
) -> dict[str, Any]:
    """Compose the variables visible to executing code."""
    sandbox: dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        "pd": pd,
        "np": np,
    }

    # Legacy / convenience: bare df
    if df is not None:
        sandbox["df"] = df.copy()

    if workspace is not None:
        sandbox["workspace"]    = workspace
        sandbox["spreadsheets"] = workspace.spreadsheets
        sandbox["documents"]    = workspace.documents
        sandbox["tables"]       = workspace.tables
        sandbox["memory"]       = workspace.memory
        # Convenience handle for the active spreadsheet/document
        active_ss = workspace.active_spreadsheet
        if active_ss is not None and "df" not in sandbox:
            sandbox["df"] = active_ss.df.copy()
        active_doc = workspace.active_document
        if active_doc is not None:
            sandbox["doc"] = active_doc
        # Helper: stash a derived dataframe back in the workspace
        def _register_table(_df: pd.DataFrame, name: str, **kwargs) -> str:
            obj = workspace.register_dataframe_as_table(_df, name=name, **kwargs)
            return obj.name
        sandbox["register_table"] = _register_table

    return sandbox


def safe_execute(
    code: str,
    df: pd.DataFrame | None = None,
    *,
    workspace: "Workspace | None" = None,
) -> tuple[str, str | None, float]:
    """
    Execute LLM-generated Python in a restricted sandbox.

    Pass either `df` (legacy single-DataFrame mode) or `workspace`
    (universal workspace mode). Passing both is allowed — `df` takes
    precedence as the value of the `df` variable.

    Returns:
        (output_string, error_string_or_None, elapsed_seconds)
    """
    if df is None and workspace is None:
        raise ValueError("safe_execute: pass either df= or workspace=")

    m = _BLOCKED.search(code)
    if m:
        msg = f"Blocked pattern in generated code: '{m.group()}'"
        log.warning("executor: %s", msg)
        return "", msg, 0.0

    sandbox = _build_sandbox(df=df, workspace=workspace)
    local_vars: dict = {}
    buf = io.StringIO()

    log.debug("executor: running %d-char code block (workspace=%s)",
              len(code), workspace is not None)
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


def execute_for_result(
    code: str,
    *,
    df: pd.DataFrame | None = None,
    workspace: "Workspace | None" = None,
) -> Any:
    """
    Run code and return the raw `result` object (not stringified).

    Used by callers that need the actual Series/DataFrame back for
    further programmatic use (e.g. excel_writer.execute_column_mutation).
    Returns None on error or if `result` was not set.
    """
    m = _BLOCKED.search(code)
    if m:
        return None
    sandbox = _build_sandbox(df=df, workspace=workspace)
    local_vars: dict = {}
    try:
        with redirect_stdout(io.StringIO()):
            exec(code, sandbox, local_vars)  # noqa: S102
    except Exception:
        return None
    return local_vars.get("result")


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
        if isinstance(result, (list, tuple)) and len(result) > 20:
            head = ", ".join(repr(x) for x in result[:10])
            return f"[{head}, … +{len(result)-10} more]"
        return str(result)

    if captured_stdout:
        return captured_stdout

    return "(code ran successfully — no `result` variable was set)"

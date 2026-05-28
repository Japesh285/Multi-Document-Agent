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

# Dangerous patterns that are HARD-blocked (these are still a deal-breaker).
# Note: NO bare-import rule here — imports of pre-loaded modules
# (pandas/numpy/etc.) are stripped by `_sanitize_code`, and imports of
# anything else are caught by the dunder + os/sys/subprocess checks below.
_BLOCKED = re.compile(
    r"\b__import__\s*\("        # __import__() dynamic import
    r"|\bopen\s*\("             # file open
    r"|\bexec\s*\("             # exec() call
    r"|\beval\s*\("             # eval() call
    r"|\bcompile\s*\("          # compile()
    r"|\bbreakpoint\s*\("       # breakpoint()
    r"|os\.\w"                  # os module access
    r"|sys\.\w"                 # sys module access
    r"|\bsubprocess\b"
    r"|\bsocket\b"
    r"|\b__\w+__\s*\("          # dunder calls like __class__()
)

# Imports of modules that are already pre-loaded in the sandbox.
# These get silently stripped instead of failing the whole call.
_SAFE_IMPORTS = re.compile(
    r"^\s*(?:"
    r"from\s+(?:pandas|numpy|openpyxl|datetime|re|json|math|statistics|collections|itertools)"
    r"(?:\.\w+)*\s+import\s+[\w,\s\*]+"
    r"|import\s+(?:pandas|numpy|openpyxl|datetime|re|json|math|statistics|collections|itertools)"
    r"(?:\s+as\s+\w+)?"
    r")\s*$",
    re.MULTILINE,
)

# Disallowed but recoverable imports (anything not in the safe list).
# If we see `import requests` etc., we strip it AND remember that we did,
# so the error message tells the model to use workspace tools instead.
_OTHER_IMPORTS = re.compile(
    r"^\s*(?:from\s+\w+(?:\.\w+)*\s+import\s+[\w,\s\*]+"
    r"|import\s+\w+(?:\s+as\s+\w+)?)\s*$",
    re.MULTILINE,
)


def _sanitize_code(code: str) -> tuple[str, list[str]]:
    """
    Strip cooperative-with-LLM imports before running. The model often
    writes `import pandas as pd` even though it's pre-loaded — that should
    be silently removed, not cause the whole query to fail.

    Returns (cleaned_code, list_of_warnings). Warnings are informational only.
    """
    warnings: list[str] = []
    n_safe = len(_SAFE_IMPORTS.findall(code))
    if n_safe:
        code = _SAFE_IMPORTS.sub("", code)
        warnings.append(f"Stripped {n_safe} import(s) of pre-loaded modules")

    # Any remaining import line is non-standard — strip it but warn.
    # The model's code might break if it relied on that import; that
    # surfaces as a NameError which triggers the normal retry loop.
    remaining = _OTHER_IMPORTS.findall(code)
    if remaining:
        code = _OTHER_IMPORTS.sub("", code)
        warnings.append(f"Stripped {len(remaining)} non-standard import(s)")

    # Tidy up: collapse 3+ blank lines that import stripping may have left
    code = re.sub(r"\n{3,}", "\n\n", code).strip()
    return code, warnings

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
    # Common-use modules made available WITHOUT requiring an import.
    # If the model writes `re.search(...)`, it just works.
    import datetime as _dt    # noqa: PLC0415
    import re as _re_mod      # noqa: PLC0415
    import json as _json_mod  # noqa: PLC0415

    sandbox: dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        "pd":       pd,
        "np":       np,
        "re":       _re_mod,
        "json":     _json_mod,
        "datetime": _dt,
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
            # OCR convenience shortcuts — only useful when the active doc has OCR
            if getattr(active_doc, "is_ocr", False):
                sandbox["ocr_pages"]    = active_doc.ocr_pages
                sandbox["ocr_lines"]    = active_doc.ocr_lines
                sandbox["ocr_blocks"]   = active_doc.ocr_blocks
                sandbox["ocr_words"]    = active_doc.ocr_words
                sandbox["ocr_tables"]   = active_doc.ocr_tables
                sandbox["ocr_metadata"] = active_doc.ocr_metadata
        # Helper: stash a derived dataframe back in the workspace
        def _register_table(_df: pd.DataFrame, name: str, **kwargs) -> str:
            obj = workspace.register_dataframe_as_table(_df, name=name, **kwargs)
            return obj.name
        sandbox["register_table"] = _register_table
        # Helper: stash a structured artifact for cross-query use (Fix #3)
        def _set_artifact(name: str, value: Any) -> str:
            workspace.artifacts[name] = value
            workspace.metadata["last_artifact_name"] = name
            return name
        sandbox["set_artifact"]   = _set_artifact
        sandbox["artifacts"]      = workspace.artifacts  # readable in code too

    return sandbox


def safe_execute(
    code: str,
    df: pd.DataFrame | None = None,
    *,
    workspace: "Workspace | None" = None,
) -> tuple[str, str | None, float]:
    """
    Execute LLM-generated Python in a restricted sandbox.

    Cooperative pre-processing:
      1. Strips imports of pre-loaded modules (pandas/numpy/etc.) so the
         model doesn't get punished for habitual `import pandas as pd`.
      2. Hard-blocks dangerous patterns only (open/exec/eval/subprocess/dunders).
      3. Captures both `result` (string) and `artifact` (structured) into the
         workspace for cross-query persistence.

    Returns:
        (output_string, error_string_or_None, elapsed_seconds)
    """
    if df is None and workspace is None:
        raise ValueError("safe_execute: pass either df= or workspace=")

    # 1. Sanitize — strip harmless imports and tidy whitespace
    code, sanitize_warnings = _sanitize_code(code)
    if sanitize_warnings:
        log.debug("executor: sanitize: %s", "; ".join(sanitize_warnings))

    # 2. Hard-block only dangerous patterns
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

    # 3. Persist any structured artifact (Fix #3).
    #    Both forms are accepted: `artifact = {...}` or `set_artifact(name, ...)`.
    if workspace is not None:
        art = local_vars.get("artifact")
        if art is not None:
            # Name the artifact by the most-recent-query stem if not explicit
            name = "last_result"
            workspace.artifacts[name] = art
            workspace.metadata["last_artifact_name"] = name
            log.debug("executor: captured artifact[%r] type=%s",
                      name, type(art).__name__)

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
    code, _warns = _sanitize_code(code)
    if _BLOCKED.search(code):
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

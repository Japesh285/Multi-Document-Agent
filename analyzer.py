"""
analyzer.py — Per-step pandas code generation and execution.

Each step gets its own isolated LLM call with:
  - the schema
  - a compact summary of what previous steps computed
  - the specific step description

Handles retry internally (up to MAX_RETRIES) without surfacing
LangGraph edges for each attempt.
"""

from __future__ import annotations
import time

import pandas as pd

from schema   import SchemaInfo
from memory   import AnalysisMemory, StepResult
from executor import safe_execute
from llm      import call_chat, extract_code
from charts   import auto_chart
from utils    import get_logger

log = get_logger("analyzer")

MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_STEP_SYSTEM = """\
You are a pandas expert executing one step of a multi-step data analysis.

EXECUTION ENVIRONMENT (pre-loaded, do NOT import):
  df   → pandas DataFrame with betting data
  pd   → pandas
  np   → numpy

RULES (no exceptions):
  1. Use ONLY the exact column names listed in the schema.
  2. Store the final answer in a variable named  result
  3. Never write import statements.
  4. Never use open(), exec(), eval(), or file I/O.
  5. Return ONLY raw Python code — no markdown fences, no prose.\
"""

_STEP_USER = """\
SCHEMA  ({rows:,} rows × {cols} columns)
{column_block}

{semantic_context}PREVIOUS STEPS COMPUTED
{memory_ctx}

CURRENT STEP
{description}

Write pandas code to compute this step. Store answer in  result\
"""

_RETRY_USER = """\
Your code raised an error.

ERROR:
{error}

BROKEN CODE:
{code}

AVAILABLE COLUMNS (use EXACTLY these names):
{columns}

Rewrite the code to fix the error. Return ONLY raw Python code.\
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _semantic_context(schema: SchemaInfo) -> str:
    """Build a compact semantic context block from the schema profile."""
    prof = getattr(schema, "profile", None)
    if prof is None:
        return ""
    ctx = prof.semantic_context()
    # Append known categorical values
    extras: list[str] = []
    if schema.unique_sports:
        extras.append(f"  Known {schema.columns[0] if schema.columns else 'Sport'} values: {', '.join(schema.unique_sports[:12])}")
    if schema.unique_results:
        extras.append(f"  Known Result values: {', '.join(schema.unique_results)}")
    if extras:
        ctx = ctx.rstrip() + "\n" + "\n".join(extras) + "\n"
    return ctx + "\n" if ctx.strip() else ""


def _build_step_messages(
    step: dict,
    schema: SchemaInfo,
    memory: AnalysisMemory,
) -> list[dict]:
    user_content = _STEP_USER.format(
        rows=schema.shape[0],
        cols=schema.shape[1],
        column_block=schema.column_block,
        semantic_context=_semantic_context(schema),
        memory_ctx=memory.step_context_for_prompt(),
        description=step["description"],
    )
    return [
        {"role": "system", "content": _STEP_SYSTEM},
        {"role": "user",   "content": user_content},
    ]


def _build_retry_messages(
    step: dict,
    schema: SchemaInfo,
    memory: AnalysisMemory,
    broken_code: str,
    error: str,
) -> list[dict]:
    base = _build_step_messages(step, schema, memory)
    # Replace the original user message with step context,
    # then append the broken code + error as a follow-up
    base.append({"role": "assistant", "content": f"```python\n{broken_code}\n```"})
    base.append({
        "role": "user",
        "content": _RETRY_USER.format(
            error=error,
            code=broken_code,
            columns="\n".join(f"  {c!r}" for c in schema.columns),
        ),
    })
    return base


def _try_chart(result_obj, step_id: str, description: str) -> str | None:
    """
    Attempt chart generation from the raw Python object returned by exec().
    Falls back silently on any error.
    """
    import pandas as pd  # noqa: PLC0415 — needed for isinstance
    if isinstance(result_obj, (pd.DataFrame, pd.Series)):
        return auto_chart(result_obj, step_id, title=description)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_step(
    step:   dict,
    schema: SchemaInfo,
    memory: AnalysisMemory,
    df:     pd.DataFrame,
    *,
    stream: bool = False,
) -> StepResult:
    """
    Generate and execute pandas code for one analysis step.
    Retries up to MAX_RETRIES on execution errors.

    Returns a StepResult (may have .error set if all retries exhausted).
    """
    step_id     = step["id"]
    description = step["description"]

    log.debug("analyzer: step=%s  %r", step_id, description[:60])

    code    = ""
    error   = None
    output  = ""
    elapsed = 0.0
    messages: list[dict] = []

    t_total = time.perf_counter()

    for attempt in range(1, MAX_RETRIES + 1):

        if attempt == 1:
            messages = _build_step_messages(step, schema, memory)
            prefix   = f"  [{step_id}] "
        else:
            log.debug("analyzer: retry %d/%d for %s — %s", attempt, MAX_RETRIES, step_id, error)
            print(f"\n  Retry {attempt - 1}/{MAX_RETRIES - 1} → fixing: {error[:80]}")
            messages = _build_retry_messages(step, schema, memory, code, error)
            prefix   = f"  [{step_id}] retry {attempt - 1} "

        if stream:
            print(prefix, end="", flush=True)

        raw  = call_chat(messages, stream_to_stdout=stream)
        code = extract_code(raw)

        if stream:
            print()

        log.debug("analyzer: attempt %d code:\n%s", attempt, code[:300])

        output, error, elapsed = safe_execute(code, df)

        if error is None:
            break

    total_elapsed = time.perf_counter() - t_total

    # Attempt chart generation (silently skipped on failure)
    chart_path: str | None = None
    if error is None:
        # Re-execute in a minimal sandbox just to get the raw Python object for charting
        chart_path = _chart_from_code(code, df, step_id, description)

    result = StepResult(
        step_id=step_id,
        description=description,
        code=code,
        output=output,
        error=error,
        elapsed=total_elapsed,
        chart_path=chart_path,
    )

    status = "OK" if result.ok else "FAILED"
    log.debug("analyzer: %s %s  %.1fs", step_id, status, total_elapsed)
    return result


def _chart_from_code(code: str, df: pd.DataFrame, step_id: str, description: str) -> str | None:
    """
    Re-run the code to capture the raw `result` object (not just its string repr)
    so auto_chart() can work with the actual DataFrame/Series.
    """
    import io, re as _re  # noqa: PLC0415
    from contextlib import redirect_stdout
    import pandas as pd   # noqa: PLC0415
    import numpy as np    # noqa: PLC0415

    _BLOCKED = _re.compile(
        r"\bimport\s+\w|\b__import__\s*\(|\bopen\s*\(|\bexec\s*\(|\beval\s*\("
    )
    if _BLOCKED.search(code):
        return None

    sandbox   = {"__builtins__": {}, "pd": pd, "np": np, "df": df.copy()}
    local_vars: dict = {}
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            exec(code, sandbox, local_vars)  # noqa: S102
        raw_result = local_vars.get("result")
        return auto_chart(raw_result, step_id, title=description)
    except Exception:
        return None

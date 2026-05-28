"""
analyzer.py — single-LLM-call workspace-aware step execution.

Architecture:

    USER QUERY
       ↓
    build_workspace_system_prompt(workspace)   ← static snapshots in
       ↓                                         the system prompt
    llm.call_chat()                            ← one call
       ↓
    executor.safe_execute(code, workspace=ws)  ← sandbox
       ↓
    StepResult(output=str(result))             ← `result` is already
                                                 a human-readable string

On Python execution errors only, we retry up to MAX_RETRIES with the
error appended. No probe phase, no verifier, no formatter — qwen-coder
writes a formatted answer directly into `result` per the system prompt.
"""

from __future__ import annotations
import time
from typing import TYPE_CHECKING

import pandas as pd

from charts   import auto_chart
from executor import execute_for_result, safe_execute
from llm      import call_chat, extract_code
from memory   import AnalysisMemory, StepResult
from prompts  import build_workspace_retry_message, build_workspace_system_prompt
from utils    import get_logger

if TYPE_CHECKING:
    from core import Workspace

log = get_logger("analyzer")

MAX_RETRIES = 2     # one initial + one retry on Python error


# ---------------------------------------------------------------------------
# Per-step user message
# ---------------------------------------------------------------------------


_STEP_USER = """\
{prior_steps_block}{targets_hint}

USER REQUEST
"{description}"

Write Python that fulfils this request against the workspace objects.
Assign a HUMAN-READABLE STRING to `result` in the format the request
implies. Return ONLY raw Python code.\
"""


def _targets_hint(step: dict) -> str:
    targets = step.get("targets") or []
    if not targets:
        return ""
    return f"\nPrimary target object(s): {', '.join(targets)}\n"


def _build_step_messages(
    step: dict,
    workspace: "Workspace",
    memory: AnalysisMemory,
    query: str,
) -> list[dict]:
    prior = memory.step_context_for_prompt() if memory.results else ""
    prior_block = (f"PRIOR STEPS\n{prior}\n" if prior else "")
    user_content = _STEP_USER.format(
        prior_steps_block=prior_block,
        targets_hint=_targets_hint(step),
        description=step["description"],
    )
    return [
        {"role": "system", "content": build_workspace_system_prompt(workspace, query=query)},
        {"role": "user",   "content": user_content},
    ]


def _build_retry_messages(
    step: dict,
    workspace: "Workspace",
    memory: AnalysisMemory,
    query: str,
    broken_code: str,
    error: str,
) -> list[dict]:
    base = _build_step_messages(step, workspace, memory, query)
    base.append({"role": "assistant", "content": f"```python\n{broken_code}\n```"})
    base.append({"role": "user",
                 "content": build_workspace_retry_message(error, broken_code, workspace)})
    return base


# ---------------------------------------------------------------------------
# Charting (re-runs the code to capture the raw object for plotting)
# ---------------------------------------------------------------------------


def _chart_from_workspace(
    code: str,
    workspace: "Workspace",
    step_id: str,
    description: str,
) -> str | None:
    try:
        raw = execute_for_result(code, workspace=workspace)
    except Exception:
        return None
    if not isinstance(raw, (pd.DataFrame, pd.Series)):
        return None
    try:
        return auto_chart(raw, step_id, title=description)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_step(
    step:      dict,
    workspace: "Workspace",
    memory:    AnalysisMemory,
    query:     str = "",
    *,
    stream:    bool = False,
) -> StepResult:
    """
    Single LLM call per step. Retries up to MAX_RETRIES only on Python
    execution errors.
    """
    step_id     = step["id"]
    description = step["description"]
    log.debug("analyzer: step=%s  %r", step_id, description[:60])

    code:    str = ""
    error:   str | None = None
    output:  str = ""

    t_total = time.perf_counter()

    for attempt in range(1, MAX_RETRIES + 1):
        if attempt == 1:
            messages = _build_step_messages(step, workspace, memory, query)
            prefix   = f"  [{step_id}] "
        else:
            log.debug("analyzer: retry %d/%d  err=%s", attempt, MAX_RETRIES, error)
            if stream:
                print(f"\n  retry {attempt - 1}/{MAX_RETRIES - 1} → fixing: {str(error)[:80]}")
            messages = _build_retry_messages(step, workspace, memory, query, code, error or "")
            prefix   = f"  [{step_id}] retry {attempt - 1} "

        if stream:
            print(prefix, end="", flush=True)

        raw  = call_chat(messages, stream_to_stdout=stream)
        code = extract_code(raw)
        if stream:
            print()

        output, error, _exec_elapsed = safe_execute(code, workspace=workspace)
        if error is None:
            break

    total_elapsed = time.perf_counter() - t_total

    chart_path: str | None = None
    if error is None:
        chart_path = _chart_from_workspace(code, workspace, step_id, description)

    result = StepResult(
        step_id=step_id, description=description, code=code,
        output=output, error=error, elapsed=total_elapsed, chart_path=chart_path,
    )
    log.debug("analyzer: %s %s  %.1fs",
              step_id, "OK" if result.ok else "FAILED", total_elapsed)
    return result

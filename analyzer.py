"""
analyzer.py — Per-step code generation and execution against the workspace.

Each step gets its own LLM call with:
  - compiled workspace context (compact summary; never raw data)
  - prior-step memory
  - the specific step description

Handles retries internally (up to MAX_RETRIES) without surfacing each
attempt to the orchestration layer.
"""

from __future__ import annotations
import time
from typing import TYPE_CHECKING, Any

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

MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Step prompt
# ---------------------------------------------------------------------------

_STEP_USER = """\
{prior_steps_block}

CURRENT STEP
{description}{targets_hint}

Write Python that computes this step against the workspace.
Store your answer in  result\
"""


def _targets_hint(step: dict) -> str:
    targets = step.get("targets") or []
    if not targets:
        return ""
    return f"\nPrimary target object(s): {', '.join(targets)}"


def _build_step_messages(
    step: dict,
    workspace: "Workspace",
    memory: AnalysisMemory,
    query: str,
) -> list[dict]:
    system_content = build_workspace_system_prompt(workspace, query=query)
    user_content   = _STEP_USER.format(
        prior_steps_block=memory.step_context_for_prompt(),
        description=step["description"],
        targets_hint=_targets_hint(step),
    )
    return [
        {"role": "system", "content": system_content},
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
    base.append({
        "role":    "user",
        "content": build_workspace_retry_message(error, broken_code, workspace),
    })
    return base


# ---------------------------------------------------------------------------
# Charting
# ---------------------------------------------------------------------------

def _chart_from_workspace(
    code: str,
    workspace: "Workspace",
    step_id: str,
    description: str,
) -> str | None:
    """
    Re-run the step code via execute_for_result to capture the raw `result`
    object, then hand it to auto_chart. Silent on any failure.
    """
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
    Generate and execute code for one workspace-aware step.
    Retries up to MAX_RETRIES on execution errors.
    """
    step_id     = step["id"]
    description = step["description"]
    log.debug("analyzer: step=%s  %r", step_id, description[:60])

    code:    str = ""
    error:   str | None = None
    output:  str = ""
    messages: list[dict] = []

    t_total = time.perf_counter()

    for attempt in range(1, MAX_RETRIES + 1):
        if attempt == 1:
            messages = _build_step_messages(step, workspace, memory, query)
            prefix   = f"  [{step_id}] "
        else:
            log.debug("analyzer: retry %d/%d for %s — %s",
                      attempt, MAX_RETRIES, step_id, error)
            if stream:
                print(f"\n  Retry {attempt - 1}/{MAX_RETRIES - 1} → fixing: {str(error)[:80]}")
            messages = _build_retry_messages(step, workspace, memory, query, code, error or "")
            prefix   = f"  [{step_id}] retry {attempt - 1} "

        if stream:
            print(prefix, end="", flush=True)

        raw  = call_chat(messages, stream_to_stdout=stream)
        code = extract_code(raw)
        if stream:
            print()

        log.debug("analyzer: attempt %d code:\n%s", attempt, code[:300])
        output, error, _exec_elapsed = safe_execute(code, workspace=workspace)
        if error is None:
            break

    total_elapsed = time.perf_counter() - t_total
    chart_path: str | None = None
    if error is None:
        chart_path = _chart_from_workspace(code, workspace, step_id, description)

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

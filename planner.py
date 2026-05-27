"""
planner.py — Query planning phase.

Decomposes a natural-language request into 2-8 concrete, executable steps
grounded in the live workspace. Each step carries:
  - id, description
  - tool: "python" | "web_search" | "chart"
  - targets: list of workspace object names the step will touch

Cross-document operations naturally appear as steps whose `targets` span
multiple objects (e.g. ["crm", "contract__table_1"]).
"""

from __future__ import annotations
import json
import re
from typing import TYPE_CHECKING

from llm   import call_chat
from utils import get_logger

if TYPE_CHECKING:
    from core import Workspace

log = get_logger("planner")

MAX_STEPS = 8
MIN_STEPS = 2

_VALID_TOOLS = {"python", "pandas", "web_search", "chart"}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are a planner for a programmable workspace where multiple spreadsheets,
documents, and tables coexist as live Python objects. Break user requests
into specific, executable steps that orchestrate those objects.

Tools:
  python      → write & execute pandas/python-docx code against workspace objects
  web_search  → search the internet for live information
  chart       → request an explicit chart (charts are also auto-generated)\
"""

_USER = """\
{workspace_block}

USER REQUEST
"{query}"

Decompose into {min}–{max} steps. Return ONLY valid JSON:
{{
  "steps": [
    {{
      "id":          "snake_case_id",
      "description": "exactly what to compute or fetch (reference objects by name)",
      "tool":        "python",
      "targets":     ["object_name_1", "..."]
    }}
  ]
}}

Rules:
- ids are lowercase snake_case
- descriptions reference workspace objects by name (e.g. "join spreadsheets['crm'] with tables['contract__table_1']")
- targets must be a subset of object names from the workspace block above (use [] if none)
- order: read/filter → compute → join/merge → write/export → web enrichment last
- use "web_search" only when external/live data is needed
- max {max} steps\
"""


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_steps(raw: str, known_names: set[str]) -> list[dict]:
    """Extract [{id, description, tool, targets}] from LLM response."""
    raw = re.sub(r"```(?:json)?\n?", "", raw).strip()

    try:
        data  = json.loads(raw)
        steps = data.get("steps", [])
        valid: list[dict] = []
        for s in steps:
            if "id" not in s or "description" not in s:
                continue
            tool = str(s.get("tool", "python")).lower()
            if tool == "pandas":
                tool = "python"
            if tool not in _VALID_TOOLS:
                tool = "python"
            targets = s.get("targets") or []
            if not isinstance(targets, list):
                targets = [str(targets)]
            # Filter targets to known names; ignore hallucinated ones
            targets = [str(t) for t in targets if str(t) in known_names]
            valid.append({
                "id":          str(s["id"]),
                "description": str(s["description"]),
                "tool":        tool,
                "targets":     targets,
            })
        if valid:
            return valid[:MAX_STEPS]
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    # Line-based fallback
    log.warning("planner: JSON parse failed — using line fallback")
    lines    = [l.strip() for l in raw.splitlines() if l.strip()]
    fallback: list[dict] = []
    for i, line in enumerate(lines[:MAX_STEPS], 1):
        line = re.sub(r'^[\d\-\*\."\']+\s*', "", line).strip()
        if len(line) > 8:
            fallback.append({"id": f"step_{i}", "description": line,
                             "tool": "python", "targets": []})
    return fallback or [{"id": "full_analysis", "description": "Inspect the workspace",
                          "tool": "python", "targets": []}]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def plan_steps(query: str, workspace: "Workspace") -> list[dict]:
    """
    Plan a sequence of steps against the live workspace.

    Returns:
        [{"id": str, "description": str, "tool": str, "targets": list[str]}, ...]
    """
    from core import compile_context  # noqa: PLC0415  (avoid cycle on bare import)
    workspace_block = compile_context(workspace, query=query, include_memory=True)

    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user",   "content": _USER.format(
            workspace_block=workspace_block,
            query=query,
            min=MIN_STEPS,
            max=MAX_STEPS,
        )},
    ]

    log.debug("planner: query=%r objects=%d", query[:80], len(workspace.all_objects()))
    raw   = call_chat(messages, stream_to_stdout=False)
    known_names = {o.name for o in workspace.all_objects()}
    steps = _parse_steps(raw, known_names)

    log.debug("planner: %d steps — %s",
              len(steps), [(s["id"], s["tool"], s["targets"]) for s in steps])
    return steps

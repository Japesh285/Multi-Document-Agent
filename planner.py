"""
planner.py — Query planning phase.

Decomposes a natural-language request into 3-8 specific, executable
steps grounded in the real schema.

Each step now carries an optional `tool` field:
  "pandas"      — generate & execute pandas code (default)
  "web_search"  — call mcp_tools.search_web()
  "chart"       — explicitly request a chart (handled by analyzer)
"""

from __future__ import annotations
import json
import re

from schema import SchemaInfo
from llm    import call_chat
from utils  import get_logger

log = get_logger("planner")

MAX_STEPS = 8
MIN_STEPS = 2


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are a data analysis planner for sports betting data.
Decompose the user's request into specific, concrete steps
that can be executed as pandas code or web searches.\
"""

_USER = """\
DATAFRAME SCHEMA
{schema_block}

AVAILABLE TOOLS
  pandas      — query/aggregate the DataFrame (use for all data operations)
  web_search  — search the internet for news, odds, injuries, schedules

USER REQUEST
"{query}"

Break this into {min}–{max} steps. Return ONLY valid JSON:
{{
  "steps": [
    {{
      "id": "snake_case_id",
      "description": "exactly what to compute or search",
      "tool": "pandas"
    }}
  ]
}}

Rules:
- ids must be lowercase snake_case
- descriptions must reference exact column names from the schema
- order: totals → rates → breakdowns → rankings → web enrichment
- use "web_search" tool only when live data (injuries/news/odds) is needed
- do not add a "generate chart" step — charts are auto-generated
- max {max} steps\
"""


# ---------------------------------------------------------------------------
# Compact schema for planner prompt
# ---------------------------------------------------------------------------

def _compact_schema(schema: SchemaInfo) -> str:
    lines = [f"Shape : {schema.shape[0]:,} rows × {schema.shape[1]} columns", "Columns:"]
    for col in schema.columns:
        sem   = schema.semantics.get(col, "")
        dtype = schema.dtypes.get(col, "")
        tag   = f"  [{sem}]" if sem else ""
        lines.append(f"  {col!r:<24} {dtype}{tag}")
    if schema.unique_sports:
        lines.append(f"Sport values  : {', '.join(schema.unique_sports[:15])}")
    if schema.unique_results:
        lines.append(f"Result values : {', '.join(schema.unique_results)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_VALID_TOOLS = {"pandas", "web_search", "chart"}


def _parse_steps(raw: str) -> list[dict]:
    """Extract [{id, description, tool}] from LLM response. Tolerates minor formatting errors."""
    raw = re.sub(r"```(?:json)?\n?", "", raw).strip()

    try:
        data  = json.loads(raw)
        steps = data.get("steps", [])
        valid = []
        for s in steps:
            if "id" not in s or "description" not in s:
                continue
            tool = str(s.get("tool", "pandas")).lower()
            if tool not in _VALID_TOOLS:
                tool = "pandas"
            valid.append({
                "id":          str(s["id"]),
                "description": str(s["description"]),
                "tool":        tool,
            })
        if valid:
            return valid[:MAX_STEPS]
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    # Line-based fallback
    log.warning("planner: JSON parse failed — using line fallback")
    lines    = [l.strip() for l in raw.splitlines() if l.strip()]
    fallback = []
    for i, line in enumerate(lines[:MAX_STEPS], 1):
        line = re.sub(r'^[\d\-\*\."\']+\s*', "", line).strip()
        if len(line) > 8:
            fallback.append({"id": f"step_{i}", "description": line, "tool": "pandas"})
    return fallback or [{"id": "full_analysis", "description": "Analyse the dataset", "tool": "pandas"}]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def plan_steps(query: str, schema: SchemaInfo) -> list[dict]:
    """
    Decompose `query` into an ordered list of steps.

    Returns:
        [{"id": str, "description": str, "tool": "pandas"|"web_search"}, ...]
    """
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user",   "content": _USER.format(
            schema_block=_compact_schema(schema),
            query=query,
            min=MIN_STEPS,
            max=MAX_STEPS,
        )},
    ]

    log.debug("planner: query=%r", query[:80])
    raw   = call_chat(messages, stream_to_stdout=False)
    steps = _parse_steps(raw)

    log.debug("planner: %d steps — %s", len(steps), [(s["id"], s["tool"]) for s in steps])
    return steps

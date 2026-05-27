"""
core.context_compiler — workspace → LLM prompt.

Two responsibilities:

  compile_context(workspace, query=None, *, max_objects=12, include_memory=True)
      Produce a compact, structured summary of the workspace suitable for
      injection into a system prompt. Never includes row-level data.

  select_relevant(workspace, query, *, max_objects=6)
      Cheap keyword-based relevance ranking. Used by the planner/analyzer
      to bound prompt size when the workspace is large.
"""

from __future__ import annotations

import re
from typing import Iterable

from utils import get_logger

from .workspace_manager import Workspace
from .workspace_objects import (
    DocumentObject,
    SpreadsheetObject,
    TableObject,
    WorkspaceObject,
)

log = get_logger("context_compiler")


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def _score(obj: WorkspaceObject, query_tokens: set[str]) -> float:
    """Cheap overlap-based score. Higher = more relevant."""
    if not query_tokens:
        return 0.0
    obj_tokens: set[str] = {obj.name.lower()}
    obj_tokens.update(_tokens(obj.summary()))
    if isinstance(obj, SpreadsheetObject):
        obj_tokens.update(c.lower() for c in obj.columns)
        if obj.schema and obj.schema.domain:
            obj_tokens.add(obj.schema.domain.lower())
    elif isinstance(obj, TableObject):
        obj_tokens.update(c.lower() for c in obj.columns)
        obj_tokens.update(_tokens(obj.nearby_heading))
    elif isinstance(obj, DocumentObject):
        for h in obj.headings:
            obj_tokens.update(_tokens(h.get("text", "")))
        for s in obj.sections:
            obj_tokens.add(s.get("name", "").lower())

    overlap = query_tokens & obj_tokens
    return float(len(overlap))


def select_relevant(
    workspace: Workspace,
    query: str,
    *,
    max_objects: int = 6,
) -> list[WorkspaceObject]:
    """Return up to `max_objects` objects ranked by relevance to `query`."""
    qt = _tokens(query)
    objs = workspace.all_objects()
    if not qt:
        # Default: most-recently-touched objects
        ordered: list[WorkspaceObject] = []
        seen: set[str] = set()
        for name in workspace.memory.active_objects:
            obj = workspace.get(name)
            if obj is not None and obj.name not in seen:
                ordered.append(obj)
                seen.add(obj.name)
        for obj in objs:
            if obj.name not in seen:
                ordered.append(obj)
        return ordered[:max_objects]

    scored = sorted(objs, key=lambda o: _score(o, qt), reverse=True)
    # Always keep the most-recently-touched even if it scores zero
    if workspace.memory.most_recent_object:
        recent = workspace.get(workspace.memory.most_recent_object)
        if recent is not None and recent not in scored[:max_objects]:
            scored = [recent] + [o for o in scored if o is not recent]
    return scored[:max_objects]


# ---------------------------------------------------------------------------
# Context compilation
# ---------------------------------------------------------------------------


def _section(title: str, lines: list[str]) -> str:
    if not lines:
        return ""
    body = "\n".join(f"  {line}" for line in lines)
    return f"{title}:\n{body}"


def compile_context(
    workspace: Workspace,
    query: str | None = None,
    *,
    max_objects: int = 12,
    include_memory: bool = True,
) -> str:
    """
    Build the compact workspace block injected into LLM prompts.

    Never includes row-level data — only counts, columns, section names,
    and one-line summaries. Designed to stay under ~600 tokens for
    workspaces with up to a few dozen objects.
    """
    if workspace.is_empty():
        return "(workspace is empty)"

    chosen = (
        select_relevant(workspace, query, max_objects=max_objects)
        if query
        else workspace.all_objects()[:max_objects]
    )
    chosen_names = {o.name for o in chosen}

    # Group by kind, preserving the selected order
    sheet_lines: list[str] = []
    doc_lines:   list[str] = []
    tbl_lines:   list[str] = []
    for obj in chosen:
        if isinstance(obj, SpreadsheetObject):
            sheet_lines.append(obj.summary())
            hint = obj.shape_hint()
            if hint:
                sheet_lines.append(f"  → {hint}")
        elif isinstance(obj, DocumentObject):
            doc_lines.append(obj.summary())
            hint = obj.shape_hint()
            if hint:
                doc_lines.append(f"  → {hint}")
        elif isinstance(obj, TableObject):
            tbl_lines.append(obj.summary())

    parts: list[str] = ["=== WORKSPACE ==="]
    if sheet_lines:
        parts.append(_section(f"Spreadsheets ({len(workspace.spreadsheets)})", sheet_lines))
    if doc_lines:
        parts.append(_section(f"Documents ({len(workspace.documents)})", doc_lines))
    if tbl_lines:
        parts.append(_section(f"Tables ({len(workspace.tables)})", tbl_lines))

    truncated = len(workspace.all_objects()) - len(chosen)
    if truncated > 0:
        parts.append(f"(+{truncated} more object(s) not shown — workspace.get(name) still works)")

    # Active / focus context
    active_lines: list[str] = []
    if workspace.active_spreadsheet:
        active_lines.append(f"spreadsheet → {workspace.active_spreadsheet.name}")
    if workspace.active_document:
        active_lines.append(f"document    → {workspace.active_document.name}")
    if active_lines:
        parts.append(_section("Active", active_lines))

    # Recent results & mutations (helps with "those rows" / "that change")
    if include_memory:
        mem = workspace.memory
        if mem.last_result is not None:
            r = mem.last_result
            line = f"last query: {r.query[:60]!r} → {r.summary or r.result_kind}"
            if r.is_dataframe():
                df = r.result_obj
                line += f" [{df.shape[0]}×{df.shape[1]}]"
            parts.append(_section("Recent", [line]))
        if mem.mutations:
            mut_lines = [
                f"{m.action} → {m.object_name} ({m.detail})"
                for m in mem.recent_mutations(3)
            ]
            parts.append(_section("Recent mutations", mut_lines))

    return "\n\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Execution-environment hint (appended to system prompts)
# ---------------------------------------------------------------------------


def execution_environment_hint(workspace: Workspace) -> str:
    """
    Short, readable description of what's injected into the execution sandbox.
    Used by prompts.py to teach the LLM how to call workspace objects.
    """
    lines = [
        "Execution environment:",
        "  workspace                  → core.Workspace (registry of every object)",
        "  spreadsheets[name]         → SpreadsheetObject (.df, .columns, .save())",
        "  documents[name]            → DocumentObject (.paragraphs, .tables, .save())",
        "  tables[name]               → TableObject (.df)",
        "  df                         → active spreadsheet's DataFrame (when one is active)",
        "  pd, np                     → pandas, numpy",
        "  result                     → assign your final result here",
    ]
    if workspace.active_spreadsheet:
        lines.append(f"  Active spreadsheet: {workspace.active_spreadsheet.name}")
    if workspace.active_document:
        lines.append(f"  Active document:    {workspace.active_document.name}")
    return "\n".join(lines)

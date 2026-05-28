"""
core.context_compiler — compact workspace summary helpers.

This module used to drive prompt building. With the single-LLM-call
architecture, prompt assembly now lives in `prompts.build_workspace_system_prompt`
which reads obj.snapshot directly. This module is retained for:

  - `compile_context(workspace, query)` — used by the synthesizer and any
    place that wants a small textual summary
  - `select_relevant(workspace, query)` — used to pick which objects to
    feature when the workspace gets large

It no longer emits anything that calls an LLM.
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
# Relevance scoring (cheap keyword overlap)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def _score(obj: WorkspaceObject, query_tokens: set[str]) -> float:
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
    return float(len(query_tokens & obj_tokens))


def select_relevant(
    workspace: Workspace,
    query: str,
    *,
    max_objects: int = 6,
) -> list[WorkspaceObject]:
    """Up to `max_objects` workspace objects ranked by relevance to `query`."""
    qt = _tokens(query)
    objs = workspace.all_objects()
    if not qt:
        ordered: list[WorkspaceObject] = []
        seen: set[str] = set()
        for name in workspace.memory.active_objects:
            obj = workspace.get(name)
            if obj is not None and obj.name not in seen:
                ordered.append(obj); seen.add(obj.name)
        for obj in objs:
            if obj.name not in seen:
                ordered.append(obj)
        return ordered[:max_objects]

    scored = sorted(objs, key=lambda o: _score(o, qt), reverse=True)
    if workspace.memory.most_recent_object:
        recent = workspace.get(workspace.memory.most_recent_object)
        if recent is not None and recent not in scored[:max_objects]:
            scored = [recent] + [o for o in scored if o is not recent]
    return scored[:max_objects]


# ---------------------------------------------------------------------------
# Compact summary (used by synthesizer + UI back-compat)
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
    include_snapshots: bool = False,
    snapshots_budget_chars: int = 2500,
) -> str:
    """
    Compact text summary of the workspace — counts, columns, active
    pointers, optionally a slice of recent memory and snapshot text.

    Used by the synthesizer for report headers and by `server.py`'s
    schema endpoint. The main analyzer prompt does NOT call this — it
    uses prompts.build_workspace_system_prompt which reads snapshots
    directly off the objects.
    """
    if workspace.is_empty():
        return "(workspace is empty)"

    chosen = (
        select_relevant(workspace, query, max_objects=max_objects)
        if query
        else workspace.all_objects()[:max_objects]
    )

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
    if workspace.artifacts:
        art_lines = [f"{k!r} = {str(v)[:60]}" for k, v in workspace.artifacts.items()]
        parts.append(_section(f"Artifacts ({len(workspace.artifacts)})", art_lines))

    truncated = len(workspace.all_objects()) - len(chosen)
    if truncated > 0:
        parts.append(f"(+{truncated} more object(s) not shown — workspace.get(name) still works)")

    active_lines: list[str] = []
    if workspace.active_spreadsheet:
        active_lines.append(f"spreadsheet → {workspace.active_spreadsheet.name}")
    if workspace.active_document:
        active_lines.append(f"document    → {workspace.active_document.name}")
    if active_lines:
        parts.append(_section("Active", active_lines))

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

    if include_snapshots and snapshots_budget_chars > 0:
        budget = snapshots_budget_chars
        snip_lines: list[str] = []
        for obj in chosen:
            snip = getattr(obj, "snapshot", "") or ""
            if not snip.strip():
                continue
            header = f"── {obj.kind}:{obj.name} ──"
            block  = f"{header}\n{snip}"
            if len(block) > budget:
                block = block[: max(0, budget - 12)] + "\n…[truncated]"
                snip_lines.append(block); break
            snip_lines.append(block); budget -= len(block) + 2
        if snip_lines:
            parts.append("DATA SNAPSHOTS\n" + "\n\n".join(snip_lines))

    return "\n\n".join(p for p in parts if p)

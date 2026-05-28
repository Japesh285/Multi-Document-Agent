"""
app.py — Workspace-first orchestrator.

Architecture
============
  User query
      │
      ▼
  router.detect_intent()  → IntentResult
      │
      ▼  (plain-Python dispatch, no LangGraph at top level)
  ┌─────────────────────────────────────────────────────────────────────┐
  │ data_query          → single workspace-aware step                   │
  │ report_generation   → plan → LangGraph step-loop → synthesize       │
  │ excel_modification  → mutation code → validate → write → save       │
  │ document_modification → workspace doc mutation → save               │
  │ internet_research   → mcp_tools search → summarize                  │
  │ workspace_operation → cross-object orchestration via planner        │
  │ hybrid_analysis     → web search + workspace analysis + synthesize  │
  │ chart_generation    → focused workspace step + chart                │
  └─────────────────────────────────────────────────────────────────────┘
      │
      ▼
  AgentOutput  (consistent return across all handlers)

Modes
=====
  python app.py cli [--file PATH] [--format html|markdown] [--verbose]
  python app.py api --file PATH [--port N]
"""

from __future__ import annotations
import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

import pandas as pd
from langgraph.graph import StateGraph, END
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

# Local modules
from analyzer        import run_step
from charts          import auto_chart
from core            import (
    ReferenceResolver,
    Workspace,
    compile_context,
)
from entity_extractor import (
    Matchup, build_search_queries, extract_entities, extract_entities_from_text,
    infer_search_intent,
)
from excel_writer    import add_column, save_excel
from executor        import execute_for_result, safe_execute
from llm             import call_chat, extract_code
from loaders         import LoaderError, SUPPORTED_EXTENSIONS, is_supported
from mcp_tools       import (
    extract_teams_from_query, search_entities, search_sports_news,
    search_web, summarize_results,
)
from memory          import AnalysisMemory, StepResult, WebFinding
from planner         import plan_steps
from prompts         import (
    build_workspace_retry_message,
    build_workspace_system_prompt,
    normalize_query,
)
from router          import (
    IntentResult, detect_intent, is_date_verification, is_followup_reference,
)
from schema          import SchemaInfo
from synthesizer     import synthesize_report
from utils           import get_logger, print_section
from verification    import (
    VerificationResult, generate_verification_summary, verify_match_dates,
)

log = get_logger("app")

# ---------------------------------------------------------------------------
# Runtime globals
# ---------------------------------------------------------------------------

_workspace: Workspace = Workspace()
_stream:    bool      = False
_resolver:  ReferenceResolver = ReferenceResolver(_workspace)


def get_workspace() -> Workspace:
    return _workspace


def _reset_workspace() -> None:
    """Clear and re-initialize the global workspace. Used by tests/server."""
    global _workspace, _resolver
    _workspace = Workspace()
    _resolver  = ReferenceResolver(_workspace)


# ---------------------------------------------------------------------------
# AgentOutput — unchanged shape for server.py compatibility
# ---------------------------------------------------------------------------

@dataclass
class AgentOutput:
    query:         str
    intent:        str        = ""
    confidence:    float      = 0.0
    report:        str        = ""
    output_format: str        = "markdown"
    charts:        list[str]  = field(default_factory=list)
    excel_updates: list[dict] = field(default_factory=list)
    web_results:   list[dict] = field(default_factory=list)
    step_results:  list[dict] = field(default_factory=list)
    elapsed:       float      = 0.0
    success:       bool       = True
    error:         str | None = None
    workspace_snapshot: dict  = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize(query: str) -> str:
    """Apply domain-aware semantic aliases."""
    active = _workspace.active_spreadsheet
    domain = ""
    if active and active.schema:
        domain = active.schema.domain or ""
    return normalize_query(query, domain=domain)


def _record_dataframe_result(query: str, intent: str, code: str) -> None:
    """If the code's `result` is a DataFrame, push it into workspace memory."""
    obj = execute_for_result(code, workspace=_workspace)
    if isinstance(obj, pd.DataFrame):
        _workspace.memory.record_result(
            query=query, intent=intent, result_obj=obj,
            summary=f"{obj.shape[0]} rows × {obj.shape[1]} cols",
        )
    elif obj is not None:
        _workspace.memory.record_result(
            query=query, intent=intent, result_obj=obj,
            summary=str(type(obj).__name__),
        )


# ---------------------------------------------------------------------------
# LangGraph — report generation pipeline (plan → N steps → synthesize)
# ---------------------------------------------------------------------------

class ReportState(TypedDict):
    query:         str
    output_format: str
    plan:          list[dict]
    step_idx:      int
    step_results:  list[dict]
    web_findings:  list[dict]
    report:        str


def _lg_plan(state: ReportState) -> ReportState:
    t0    = time.perf_counter()
    steps = plan_steps(state["query"], _workspace)
    log.debug("node_plan: %d steps in %.1fs", len(steps), time.perf_counter() - t0)

    if _stream:
        print(f"\n  Plan ({len(steps)} steps):")
        for i, s in enumerate(steps, 1):
            tool_tag = f"[{s.get('tool','python')}]"
            tgts = s.get("targets") or []
            tgt_str = f" → {','.join(tgts)}" if tgts else ""
            print(f"    {i}. {s['description']} {tool_tag}{tgt_str}")
        print()

    return {**state, "plan": steps, "step_idx": 0}


def _lg_step(state: ReportState) -> ReportState:
    step    = state["plan"][state["step_idx"]]
    memory  = AnalysisMemory.from_dicts(state["query"], state["step_results"])
    for wf in state.get("web_findings", []):
        memory.add_web(WebFinding.from_dict(wf))

    total   = len(state["plan"])
    current = state["step_idx"] + 1

    # web_search step
    if step.get("tool") == "web_search":
        if _stream:
            print(f"  [{current}/{total}] [WEB] {step['description']}")
        results = search_web(step["description"])
        summary = summarize_results(results, step["description"])
        sr = StepResult(
            step_id=step["id"], description=step["description"],
            code="# web_search", output=summary, error=None, elapsed=0.0,
        )
        updated = list(state["step_results"]) + [sr.to_dict()]
        if _stream:
            print(f"  ✓ {step['id']}")
            print(f"    {summary[:120]} …\n")
        return {**state, "step_results": updated, "step_idx": state["step_idx"] + 1}

    # python step (workspace-aware)
    if _stream:
        print(f"  [{current}/{total}] {step['description']}")
        print("  ", end="", flush=True)

    result = run_step(step, _workspace, memory, query=state["query"], stream=_stream)

    if _stream:
        status = "✓" if result.ok else "✗"
        chart  = f"  → {result.chart_path}" if result.chart_path else ""
        print(f"\n  {status} {step['id']}  ({result.elapsed:.1f}s){chart}")
        if result.ok:
            for line in result.output.splitlines()[:3]:
                print(f"    {line}")
        print()

    updated = list(state["step_results"]) + [result.to_dict()]
    return {**state, "step_results": updated, "step_idx": state["step_idx"] + 1}


def _lg_synthesize(state: ReportState) -> ReportState:
    memory = AnalysisMemory.from_dicts(state["query"], state["step_results"])
    for wf in state.get("web_findings", []):
        memory.add_web(WebFinding.from_dict(wf))

    if _stream:
        print(f"  Synthesizing ({memory.success_count}/{len(memory.results)} steps OK) …\n")

    t0     = time.perf_counter()
    report = synthesize_report(
        state["query"],
        memory=memory,
        workspace=_workspace,
        stream=_stream,
        output_format=state.get("output_format", "markdown"),
        save_to_disk=True,
    )
    log.debug("node_synthesize: %d chars in %.1fs", len(report), time.perf_counter() - t0)
    return {**state, "report": report}


def _lg_route(state: ReportState) -> str:
    return "more" if state["step_idx"] < len(state["plan"]) else "synthesize"


_gb = StateGraph(ReportState)
_gb.add_node("plan",       _lg_plan)
_gb.add_node("step",       _lg_step)
_gb.add_node("synthesize", _lg_synthesize)
_gb.set_entry_point("plan")
_gb.add_edge("plan", "step")
_gb.add_conditional_edges("step", _lg_route, {"more": "step", "synthesize": "synthesize"})
_gb.add_edge("synthesize", END)
_report_agent = _gb.compile()


def _run_report_graph(
    query: str,
    output_format: str = "markdown",
    web_findings: list[dict] | None = None,
) -> ReportState:
    initial: ReportState = {
        "query":         _normalize(query),
        "output_format": output_format,
        "plan":          [],
        "step_idx":      0,
        "step_results":  [],
        "web_findings":  web_findings or [],
        "report":        "",
    }
    return _report_agent.invoke(initial)


# ---------------------------------------------------------------------------
# Single-step query (workspace-aware data_query handler shared helper)
# ---------------------------------------------------------------------------

def _run_single_step(query: str, *, step_id: str = "query") -> StepResult:
    """Plan-free single execution against the workspace."""
    memory = AnalysisMemory(query=query)
    step   = {"id": step_id, "description": _normalize(query),
              "tool": "python", "targets": []}
    if _stream:
        print("\n  ", end="", flush=True)
    result = run_step(step, _workspace, memory, query=query, stream=_stream)
    if _stream:
        status = "✓" if result.ok else "✗"
        print(f"\n  {status} ({result.elapsed:.1f}s)")
    return result


# ---------------------------------------------------------------------------
# Intent handlers
# ---------------------------------------------------------------------------

def handle_data_query(query: str) -> AgentOutput:
    """Single workspace-aware step for direct questions."""
    result = _run_single_step(query)
    if result.ok:
        _record_dataframe_result(query, "data_query", result.code)
    _workspace.memory.record_query(query)
    return AgentOutput(
        query=query,
        report=result.output if result.ok else f"Error: {result.error}",
        charts=[result.chart_path] if result.chart_path else [],
        step_results=[result.to_dict()],
        success=result.ok,
        error=result.error,
    )


def handle_report(query: str, output_format: str = "markdown") -> AgentOutput:
    """Plan → execute → synthesize via LangGraph."""
    state  = _run_report_graph(query, output_format)
    memory = AnalysisMemory.from_dicts(query, state["step_results"])
    _workspace.memory.record_query(query)
    return AgentOutput(
        query=query,
        report=state["report"],
        output_format=output_format,
        charts=memory.chart_paths,
        step_results=state["step_results"],
        success=memory.success_count > 0,
    )


def handle_chart(query: str) -> AgentOutput:
    """Focused single step → chart, minimal report wrapper."""
    result = _run_single_step(query, step_id="chart_data")
    charts = [result.chart_path] if result.chart_path else []
    body = result.output if result.ok else f"Error: {result.error}"
    report = body
    if charts:
        report += f"\n\nChart saved: {charts[0]}"
    _workspace.memory.record_query(query)
    return AgentOutput(
        query=query, report=report, charts=charts,
        step_results=[result.to_dict()],
        success=result.ok, error=result.error,
    )


# ── Excel column mutation ──────────────────────────────────────────────

_COLUMN_SYSTEM = """\
You are a pandas expert. Generate code that computes values for a new
DataFrame column against a workspace spreadsheet.

EXECUTION ENVIRONMENT (pre-loaded — do NOT import):
  workspace, spreadsheets, documents, tables, df, doc, pd, np, memory

IMPORTANT:
  - `result` must be a pd.Series whose length equals len(target_df).
  - Never write import statements, open(), exec(), eval(), or file I/O.\
"""

_COLUMN_USER = """\
{workspace_block}

Target spreadsheet  : spreadsheets['{target}']  ({rows} rows)
Target columns      : {columns}

Task: {description}

Write Python that:
  1. Reads from spreadsheets['{target}'].df and any other workspace objects
     the task needs.
  2. Computes values for the new column.
  3. Stores the result as a pd.Series in `result` with exactly {rows} entries.

Return ONLY raw Python code.\
"""


def _infer_column_name(query: str) -> str:
    import re  # noqa: PLC0415
    q = query.lower()
    patterns = [
        (r"risk\s*level",  "Risk Level"),
        (r"injury\s*note", "Injury Notes"),
        (r"odds\s*change", "Odds Change"),
        (r"match\s*date",  "Match Date"),
    ]
    for pattern, fixed in patterns:
        if re.search(pattern, q):
            return fixed
    m = re.search(
        r"(?:add|create|append)\s+(?:a\s+)?(?:column\s+called\s+)?['\"]?(\w[\w ]*)['\"]?", q
    )
    if m:
        return m.group(1).strip().title()
    return "New Column"


def handle_excel_modification(query: str) -> AgentOutput:
    """Generate mutation code → validate → write column → save spreadsheet."""
    # 1. Find the target spreadsheet (referenced or active)
    target_ss = None
    ref = _resolver.resolve(query)
    if ref.matched and ref.kind == "spreadsheet":
        target_ss = ref.object  # type: ignore[assignment]
    if target_ss is None:
        target_ss = _workspace.active_spreadsheet
    if target_ss is None:
        return AgentOutput(query=query, report="No spreadsheet available to modify.",
                           success=False, error="no target spreadsheet")

    col_name = _infer_column_name(query)
    workspace_block = compile_context(_workspace, query=query, include_memory=False)

    messages = [
        {"role": "system", "content": _COLUMN_SYSTEM},
        {"role": "user",   "content": _COLUMN_USER.format(
            workspace_block=workspace_block,
            target=target_ss.name,
            rows=len(target_ss.df),
            columns=", ".join(target_ss.columns),
            description=_normalize(query),
        )},
    ]

    if _stream:
        print(f"\n  Generating mutation code for spreadsheets[{target_ss.name!r}] …\n  ",
              end="", flush=True)

    raw  = call_chat(messages, stream_to_stdout=_stream)
    code = extract_code(raw)

    # 2. Execute under workspace mode to capture the Series
    error_str = None
    output, err, _ = safe_execute(code, workspace=_workspace)
    if err:
        error_str = err
        _workspace.memory.record_mutation(
            object_name=target_ss.name, object_kind="spreadsheet",
            action="add_column", detail=f"code error: {err}",
            success=False, error=err,
        )
        return AgentOutput(
            query=query, report=f"Failed to add column: {err}",
            step_results=[{
                "step_id": "excel_mutation", "description": query,
                "code": code, "output": output, "error": err,
                "elapsed": 0.0, "chart_path": None,
            }],
            success=False, error=err,
        )

    raw_result = execute_for_result(code, workspace=_workspace)
    if not isinstance(raw_result, pd.Series):
        msg = f"Code did not return a pd.Series — got {type(raw_result).__name__}"
        _workspace.memory.record_mutation(
            object_name=target_ss.name, object_kind="spreadsheet",
            action="add_column", detail=msg, success=False, error=msg,
        )
        return AgentOutput(query=query, report=msg, success=False, error=msg)

    # 3. Add column and save
    df_new, change = add_column(target_ss.df, col_name, raw_result, overwrite=True)
    if not change.success:
        _workspace.memory.record_mutation(
            object_name=target_ss.name, object_kind="spreadsheet",
            action="add_column", detail=change.error or "", success=False,
            error=change.error or "",
        )
        return AgentOutput(query=query, report=f"Failed to add column: {change.error}",
                           success=False, error=change.error)

    # Update the underlying loaded sheet then save
    target_ss.loaded.sheets[target_ss.active_sheet] = df_new  # type: ignore[union-attr]
    save_result = target_ss.save()

    if save_result.get("success"):
        report = (
            f"Column **'{col_name}'** added to spreadsheets[{target_ss.name!r}].\n"
            f"- Rows updated: {change.rows_affected:,}\n"
            f"- Saved to: {save_result.get('file', target_ss.source_path)}\n"
            f"- Backup: {save_result.get('backup_path', 'N/A')}"
        )
        _workspace.memory.record_mutation(
            object_name=target_ss.name, object_kind="spreadsheet",
            action="add_column",
            detail=f"col={col_name!r} rows={change.rows_affected}",
            success=True, workspace=_workspace,
        )
        updates = [change.to_dict()]
    else:
        report  = f"Failed to save: {save_result.get('error')}"
        updates = []

    return AgentOutput(
        query=query, report=report, excel_updates=updates,
        step_results=[{
            "step_id": "excel_mutation", "description": query,
            "code": code, "output": report, "error": error_str,
            "elapsed": 0.0, "chart_path": None,
        }],
        success=save_result.get("success", False),
        error=save_result.get("error") if not save_result.get("success") else None,
    )


# ── Document modification ──────────────────────────────────────────────

_DOC_SYSTEM = """\
You are a python-docx expert. Generate code that mutates a workspace
document and saves it.

EXECUTION ENVIRONMENT (pre-loaded — do NOT import):
  workspace, spreadsheets, documents, tables, df, doc, pd, np, memory

IMPORTANT:
  - Mutations: documents[name].replace_text(old, new),
                documents[name].add_paragraph(text),
                documents[name].add_heading(text, level=1),
                documents[name].add_table_from_df(df).
  - Save: documents[name].save() (always creates a backup first).
  - Never write import statements, open(), exec(), eval(), or file I/O.
  - Store a one-line summary in `result`.\
"""

_DOC_USER = """\
{workspace_block}

Target document : documents['{target}']
Sections        : {sections}

Task: {description}

Write Python that performs the mutation and saves the document.
Store a one-line summary of what changed in `result`.\
"""


def handle_document_modification(query: str) -> AgentOutput:
    """Generate doc-mutation code → execute → save."""
    target_doc = None
    ref = _resolver.resolve(query)
    if ref.matched and ref.kind == "document":
        target_doc = ref.object  # type: ignore[assignment]
    if target_doc is None:
        target_doc = _workspace.active_document
    if target_doc is None:
        return AgentOutput(query=query, report="No document available to modify.",
                           success=False, error="no target document")

    workspace_block = compile_context(_workspace, query=query, include_memory=False)
    sections = ", ".join(s["name"] for s in target_doc.sections) or "(none)"

    messages = [
        {"role": "system", "content": _DOC_SYSTEM},
        {"role": "user",   "content": _DOC_USER.format(
            workspace_block=workspace_block,
            target=target_doc.name,
            sections=sections,
            description=query,
        )},
    ]

    if _stream:
        print(f"\n  Generating document mutation for documents[{target_doc.name!r}] …\n  ",
              end="", flush=True)

    raw  = call_chat(messages, stream_to_stdout=_stream)
    code = extract_code(raw)
    output, err, elapsed = safe_execute(code, workspace=_workspace)

    if err:
        _workspace.memory.record_mutation(
            object_name=target_doc.name, object_kind="document",
            action="document_mutation", detail=f"code error: {err}",
            success=False, error=err,
        )
        return AgentOutput(query=query, report=f"Failed to modify document: {err}",
                           step_results=[{
                               "step_id": "doc_mutation", "description": query,
                               "code": code, "output": output, "error": err,
                               "elapsed": elapsed, "chart_path": None,
                           }],
                           success=False, error=err)

    _workspace.memory.record_mutation(
        object_name=target_doc.name, object_kind="document",
        action="document_mutation", detail=output[:120] if output else "saved",
        success=True, workspace=_workspace,
    )
    return AgentOutput(
        query=query,
        report=f"Document **{target_doc.name}** updated.\n\n{output}",
        step_results=[{
            "step_id": "doc_mutation", "description": query,
            "code": code, "output": output, "error": None,
            "elapsed": elapsed, "chart_path": None,
        }],
        excel_updates=[{
            "action": "document_mutation", "column": "",
            "rows_affected": 0, "detail": output[:120] if output else "",
            "success": True, "error": "",
        }],
        success=True,
    )


# ── Workspace operation (cross-object) ─────────────────────────────────

def handle_workspace_operation(query: str, output_format: str = "markdown") -> AgentOutput:
    """
    Multi-object orchestration: planner emits steps that may touch any
    combination of spreadsheets, documents, and tables.
    """
    state  = _run_report_graph(query, output_format)
    memory = AnalysisMemory.from_dicts(query, state["step_results"])
    _workspace.memory.record_query(query)
    return AgentOutput(
        query=query, report=state["report"],
        output_format=output_format,
        charts=memory.chart_paths,
        step_results=state["step_results"],
        success=memory.success_count > 0,
    )


# ── Internet research ──────────────────────────────────────────────────

def handle_web_search(query: str) -> AgentOutput:
    """Context-aware web search (uses workspace data for entity extraction)."""
    is_ref   = is_followup_reference(query)
    last_df  = _workspace.memory.last_dataframe_result
    schema   = None
    if _workspace.active_spreadsheet:
        schema = _workspace.active_spreadsheet.schema
    entities = None

    if is_ref and last_df is not None and not last_df.empty:
        if _stream:
            print(f"\n  Using context from last result ({len(last_df)} rows) …")
        entities = extract_entities(last_df, profile=schema.profile if schema else None)
    else:
        unique_sports = schema.unique_sports if schema else []
        entities = extract_entities_from_text(query, unique_sports)

    if is_date_verification(query) and (entities and not entities.is_empty()):
        if _stream:
            print("  Routing to date verification …")
        return handle_verification(query, entities)

    intent  = infer_search_intent(query)
    queries = (
        build_search_queries(entities, intent_hint=intent)
        if not entities.is_empty() else [query]
    )
    if _stream:
        print(f"\n  Searching ({len(queries)} queries, intent={intent}) …")
        for q in queries:
            print(f"    • {q}")
        print()

    entity_results = search_entities(queries, use_news=(intent == "injury"))
    all_results    = [r for rs in entity_results.values() for r in rs]
    summary        = summarize_results(all_results, context=query)
    report         = f"## Web Research: {query}\n\n{summary}"
    _workspace.memory.record_query(query)
    return AgentOutput(
        query=query, report=report, web_results=all_results[:6],
        step_results=[{
            "step_id": "web_search", "description": query,
            "code": "", "output": summary, "error": None,
            "elapsed": 0.0, "chart_path": None,
        }],
        success=bool(summary),
    )


def _artifact_to_matchups(artifact: dict) -> list:
    """
    Pull verifiable entities from a structured artifact deterministically.

    Recognised artifact shapes:
      {"type": "nba_matches" | "matches" | "fixtures" | "events" | ...,
       "items": [{"teams": "A vs B", "date": "YYYY-MM-DD"}, ...]}
      {"items": [{"selection": "...", "game_date": "..."}, ...]}

    Returns a list of Matchup-shaped entries the verifier can consume.
    """
    from entity_extractor import Matchup as _Mu  # noqa: PLC0415
    if not isinstance(artifact, dict):
        return []
    items = artifact.get("items") or []
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sel  = str(it.get("selection") or it.get("teams") or it.get("name") or "").strip()
        date = str(it.get("game_date") or it.get("date") or "").strip()
        if not sel:
            continue
        out.append(_Mu(
            selection=sel, sport=str(it.get("sport", "")),
            game_date=date, bet_type="", result="",
        ))
    return out


def handle_verification(query: str, entities=None) -> AgentOutput:
    """
    Verification flow that operates on **structured artifacts**.

    Priority for entity source (no LLM extraction when an artifact exists):
      1. workspace.artifacts['last_result']  — set by the previous query
      2. Last DataFrame result + active spreadsheet schema
      3. LLM-based text extraction (fallback only)
    """
    schema  = None
    if _workspace.active_spreadsheet:
        schema = _workspace.active_spreadsheet.schema

    # 1. Structured artifact path — deterministic, no LLM
    matchups: list = []
    artifact = _workspace.artifacts.get("last_result")
    if artifact is not None:
        matchups = _artifact_to_matchups(artifact)
        if matchups:
            log.info("verify: resolved %d entity(ies) from last_result artifact",
                     len(matchups))
            from entity_extractor import EntitySet  # noqa: PLC0415
            entities = EntitySet(matchups=matchups)

    # 2. DataFrame result fallback
    if not matchups and entities is None:
        last_df = _workspace.memory.last_dataframe_result
        if last_df is not None and not last_df.empty and schema is not None:
            entities = extract_entities(last_df, profile=schema.profile)
        else:
            unique_sports = schema.unique_sports if schema else []
            entities = extract_entities_from_text(query, unique_sports)

    if entities is None or entities.is_empty():
        return AgentOutput(
            query=query,
            report=("Could not find any entities to verify. Run a query that "
                    "produces a structured artifact first (e.g. 'list NBA matches')."),
            success=False, error="No entities",
        )

    matchups = matchups or entities.matchups
    if not matchups:
        from entity_extractor import Matchup as _Mu  # noqa: PLC0415
        dates = entities.dates
        for i, team in enumerate(entities.teams):
            matchups.append(_Mu(
                selection=team, sport="",
                game_date=dates[i] if i < len(dates) else "",
                bet_type="", result="",
            ))

    if _stream:
        print(f"\n  Verifying {len(matchups)} matchup(s) …")

    search_map: dict[str, list[dict]] = {}
    for matchup in matchups:
        from entity_extractor import EntitySet  # noqa: PLC0415
        qs = build_search_queries(EntitySet(matchups=[matchup]),
                                  intent_hint="fixture", max_queries=2)
        q = qs[0] if qs else matchup.selection
        if _stream:
            print(f"    ↳ {q}")
        search_map[matchup.selection] = search_web(q, max_results=4)

    vresults  = verify_match_dates(matchups, search_map)
    summary   = generate_verification_summary(vresults)
    report_md = summary.to_markdown()

    if _stream:
        print(f"\n  Verification complete: {summary.matched}/{summary.total} matched\n")

    excel_updates: list[dict] = []
    target_ss = _workspace.active_spreadsheet
    if target_ss is not None and summary.results:
        excel_updates = _write_verification_columns(vresults, target_ss)

    _workspace.memory.record_query(query)
    return AgentOutput(
        query=query, report=report_md,
        web_results=[r.to_dict() for r in vresults],
        excel_updates=excel_updates,
        step_results=[{
            "step_id": "verification", "description": query,
            "code": "", "output": report_md, "error": None,
            "elapsed": 0.0, "chart_path": None,
        }],
        success=summary.total > 0,
    )


def _write_verification_columns(vresults: list[VerificationResult], target_ss) -> list[dict]:
    df = target_ss.df
    if df.empty or "Selection" not in df.columns:
        return []
    result_map = {r.entity: r for r in vresults}
    df_new = df.copy()
    for col in ("Verified Date", "Date Match", "Verification Confidence"):
        if col not in df_new.columns:
            df_new[col] = None
    for idx, row in df_new.iterrows():
        sel = str(row.get("Selection", "")).strip()
        vr  = result_map.get(sel)
        if vr:
            df_new.at[idx, "Verified Date"]           = vr.web_date or ""
            df_new.at[idx, "Date Match"]              = vr.match
            df_new.at[idx, "Verification Confidence"] = round(vr.confidence, 3)
    target_ss.loaded.sheets[target_ss.active_sheet] = df_new
    save_result = target_ss.save()
    if save_result.get("success"):
        _workspace.memory.record_mutation(
            object_name=target_ss.name, object_kind="spreadsheet",
            action="add_verification_cols", detail=f"{len(vresults)} entities",
            success=True, workspace=_workspace,
        )
        return [{"action": "add_verification_cols", "rows": len(vresults)}]
    return []


def handle_hybrid(query: str, intent: IntentResult, output_format: str = "markdown") -> AgentOutput:
    """Web research → workspace analysis → optional excel write."""
    web_findings_dicts: list[dict] = []

    if intent.needs_web_search:
        if _stream:
            print("\n  Phase 1: Web research …")
        schema = None
        if _workspace.active_spreadsheet:
            schema = _workspace.active_spreadsheet.schema
        unique_sports = schema.unique_sports if schema else []
        teams   = extract_teams_from_query(query, unique_sports)
        results = []
        for team in (teams[:3] if teams else [query]):
            results.extend(search_sports_news(team))
        summary = summarize_results(results, context=query)
        wf = WebFinding(search_query=query, summary=summary, raw_count=len(results))
        web_findings_dicts = [wf.to_dict()]
        if _stream:
            print(f"  Found {len(results)} results\n")

    if _stream:
        print("  Phase 2: Analysis …")

    state  = _run_report_graph(query, output_format, web_findings=web_findings_dicts)
    memory = AnalysisMemory.from_dicts(query, state["step_results"])
    for wfd in web_findings_dicts:
        memory.add_web(WebFinding.from_dict(wfd))

    excel_updates: list[dict] = []
    if intent.needs_excel_write and _workspace.active_spreadsheet:
        if _stream:
            print("  Phase 3: Updating spreadsheet …")
        out = handle_excel_modification(query)
        excel_updates = out.excel_updates

    _workspace.memory.record_query(query)
    return AgentOutput(
        query=query, report=state["report"],
        output_format=output_format,
        charts=memory.chart_paths,
        excel_updates=excel_updates,
        step_results=state["step_results"],
        success=memory.success_count > 0,
    )


# ---------------------------------------------------------------------------
# Master router
# ---------------------------------------------------------------------------

def _is_document_modification_request(query: str) -> bool:
    if not _workspace.documents:
        return False
    import re  # noqa: PLC0415
    return bool(re.search(
        r"\b(rewrite|edit|modify|update|insert|add|replace|append|change)\b",
        query, re.I
    )) and bool(_resolver.resolve(query).object and
                _resolver.resolve(query).kind == "document")


def _is_workspace_operation(query: str) -> bool:
    refs = _resolver.resolve_all(query)
    distinct_kinds = {r.kind for r in refs if r.kind}
    return len(refs) >= 2 or len(distinct_kinds) >= 2


def process_query(query: str, output_format: str = "markdown") -> AgentOutput:
    """Single entry point used by CLI, API, and server."""
    t0 = time.perf_counter()

    # Use the active spreadsheet's schema for intent detection (back-compat
    # for the keyword-based router).
    schema = _workspace.active_spreadsheet.schema if _workspace.active_spreadsheet else None
    intent = detect_intent(query, schema)
    log.debug(
        "process_query: intent=%s conf=%.0f%% web=%s write=%s charts=%s",
        intent.intent, intent.confidence * 100,
        intent.needs_web_search, intent.needs_excel_write, intent.needs_charts,
    )

    if _stream:
        print(f"\n  Intent: {intent.intent}  ({intent.confidence:.0%} confidence  via {intent.method})")

    out: AgentOutput
    itype = intent.intent

    # Cross-document or document-targeted overrides come before the standard intents.
    if is_date_verification(query):
        out = handle_verification(query)
    elif _is_document_modification_request(query):
        out = handle_document_modification(query)
    elif _is_workspace_operation(query):
        out = handle_workspace_operation(query, output_format)
    elif itype == "data_query":
        out = handle_data_query(query)
    elif itype == "report_generation":
        out = handle_report(query, output_format)
    elif itype == "excel_modification":
        out = handle_excel_modification(query)
    elif itype == "document_modification":
        out = handle_document_modification(query)
    elif itype == "workspace_operation":
        out = handle_workspace_operation(query, output_format)
    elif itype == "internet_research":
        out = handle_web_search(query)
    elif itype == "hybrid_analysis":
        out = handle_hybrid(query, intent, output_format)
    elif itype == "chart_generation":
        out = handle_chart(query)
    else:
        out = handle_data_query(query)

    out.intent             = intent.intent
    out.confidence         = intent.confidence
    out.elapsed            = time.perf_counter() - t0
    out.workspace_snapshot = _workspace.inventory_dict()
    return out


# ---------------------------------------------------------------------------
# FastAPI (legacy)
# ---------------------------------------------------------------------------

fapi = FastAPI(title="Autonomous Workspace Agent", version="7.0")


class QueryRequest(BaseModel):
    query:         str
    output_format: str = "markdown"


class QueryResponse(BaseModel):
    query:         str
    intent:        str
    confidence:    float
    report:        str
    output_format: str
    charts:        list[str]
    excel_updates: list[dict]
    step_count:    int
    elapsed:       float
    success:       bool


@fapi.get("/info")
def api_info():
    return {"workspace": _workspace.inventory_dict()}


@fapi.post("/query", response_model=QueryResponse)
def api_query(req: QueryRequest):
    if not req.query.strip():
        raise HTTPException(400, "Query must not be empty.")
    try:
        out = process_query(req.query, req.output_format)
    except ConnectionError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        log.exception("api_query error")
        raise HTTPException(500, str(exc))
    return QueryResponse(
        query=out.query, intent=out.intent, confidence=out.confidence,
        report=out.report, output_format=out.output_format,
        charts=out.charts, excel_updates=out.excel_updates,
        step_count=len(out.step_results),
        elapsed=round(out.elapsed, 2), success=out.success,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_HELP = """\
Commands
  schema         show active spreadsheet's column types
  objects        list every workspace object
  load <path>    add another spreadsheet or document to the workspace
  use <name>     make an object active (or switch sheet on a spreadsheet)
  sheets         list sheets of the active workbook
  intent <q>     show what intent would be detected for a query
  html           toggle HTML output (default: markdown)
  exit           quit\
"""


def run_cli(file_path: str, output_format: str = "markdown") -> None:
    global _stream
    _stream = True

    if not file_path:
        file_path = input(
            "Enter path to a spreadsheet (.xlsx/.xls/.xlsm/.csv/.tsv) "
            "or document (.docx): "
        ).strip().strip('"\'')

    try:
        _load_path_into_workspace(file_path)
    except (FileNotFoundError, ValueError, LoaderError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    _print_workspace_status(file_path)
    print(f"Output  : {output_format}")
    print('Type "help" for commands.\n')

    fmt = output_format
    while True:
        try:
            raw_query = input("Query> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break
        if not raw_query:
            continue
        cmd = raw_query.lower()

        if cmd in ("exit", "quit", "q"):
            break
        if cmd == "help":
            print(_HELP)
            continue
        if cmd == "schema":
            ss = _workspace.active_spreadsheet
            if ss is None or ss.schema is None:
                print("  (no active spreadsheet)\n")
            else:
                print(f"\n{ss.schema.compact_summary()}\n")
            continue
        if cmd == "objects":
            print()
            for s in _workspace.spreadsheets.values():
                print(f"  spreadsheet  {s.summary()}")
            for d in _workspace.documents.values():
                print(f"  document     {d.summary()}")
            for t in _workspace.tables.values():
                print(f"  table        {t.summary()}")
            if _workspace.is_empty():
                print("  (workspace empty)")
            print()
            continue
        if cmd.startswith("load "):
            path = raw_query[5:].strip().strip('"\'')
            try:
                _load_path_into_workspace(path)
                print(f"  Loaded: {path}\n")
            except (FileNotFoundError, ValueError, LoaderError) as exc:
                print(f"  ERROR: {exc}\n")
            continue
        if cmd == "sheets":
            ss = _workspace.active_spreadsheet
            if ss is None or ss.loaded is None:
                print("  (no active spreadsheet)\n")
            else:
                for s in ss.loaded.sheet_info:
                    mark = " *" if s.is_primary else ""
                    print(f"  {s.name}{mark}  {s.rows} rows × {s.columns} cols")
                print()
            continue
        if cmd.startswith("use "):
            target = raw_query[4:].strip()
            obj = _workspace.get(target)
            if obj is not None:
                _workspace.memory.touch(obj.name)
                print(f"  Active → {obj.kind}:{obj.name}\n")
                continue
            ss = _workspace.active_spreadsheet
            if ss is not None and ss.loaded is not None and target in ss.loaded.sheets:
                ss.set_active_sheet(target)
                from schema import build_schema  # noqa: PLC0415
                ss.schema = build_schema(ss.df, ss.source_path, loaded=ss.loaded)
                print(f"  Active sheet → '{target}'\n")
                continue
            print(f"  Unknown object/sheet: {target}\n")
            continue
        if cmd == "html":
            fmt = "html" if fmt == "markdown" else "markdown"
            print(f"  Output format: {fmt}\n")
            continue
        if cmd.startswith("intent "):
            q = raw_query[7:].strip()
            schema = _workspace.active_spreadsheet.schema if _workspace.active_spreadsheet else None
            ir = detect_intent(q, schema)
            print(f"  intent={ir.intent}  conf={ir.confidence:.0%}  "
                  f"web={ir.needs_web_search}  write={ir.needs_excel_write}  "
                  f"charts={ir.needs_charts}  method={ir.method}\n")
            continue

        try:
            out = process_query(raw_query, fmt)
        except ConnectionError as exc:
            print(f"\n  ERROR: {exc}\n")
            continue
        except Exception as exc:
            log.exception("process_query failed")
            print(f"\n  ERROR: {exc}\n")
            continue

        label = (f"intent={out.intent}  {out.confidence:.0%}  "
                 f"{len(out.step_results)} steps  {out.elapsed:.1f}s")
        print_section(label, out.report)
        if out.charts:
            print(f"  Charts: {', '.join(out.charts)}")
        if out.excel_updates:
            print(f"  Updates: {len(out.excel_updates)} mutation(s)")
        print()


def _print_workspace_status(initial_path: str) -> None:
    print(f"\nLoaded  : {Path(initial_path).name}")
    print(f"Objects : {len(_workspace.spreadsheets)} spreadsheet(s), "
          f"{len(_workspace.documents)} document(s), {len(_workspace.tables)} table(s)")
    ss = _workspace.active_spreadsheet
    if ss is not None:
        print(f"Active  : spreadsheet:{ss.name}  [{ss.shape[0]:,} × {ss.shape[1]}]")
        if ss.schema and ss.schema.domain:
            print(f"Domain  : {ss.schema.domain}  ({ss.schema.domain_confidence:.0%})")
    if _workspace.active_document is not None:
        d = _workspace.active_document
        print(f"          document:{d.name}  ({len(d.paragraphs)} paragraphs)")


# ---------------------------------------------------------------------------
# Loading helpers (used by CLI + server)
# ---------------------------------------------------------------------------

def _load_path_into_workspace(file_path: str, *, name: str | None = None,
                              sheet: str | None = None) -> None:
    """
    Dispatch by extension:
        .xlsx/.xls/.xlsm/.csv/.tsv  → register_spreadsheet
        .docx                       → register_document
        .pdf / image extensions     → register_ocr (Tesseract via loaders.ocr)
    """
    from loaders.ocr import is_ocr_supported, SUPPORTED_OCR_EXTENSIONS  # noqa: PLC0415

    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")
    suffix = p.suffix.lower()
    if suffix == ".docx":
        _workspace.register_document_from_path(str(p), name=name)
    elif is_ocr_supported(str(p)):
        _workspace.register_ocr_from_path(str(p), name=name)
    elif is_supported(str(p)):
        _workspace.register_spreadsheet_from_path(str(p), name=name, sheet=sheet)
    else:
        supported = sorted(
            set(SUPPORTED_EXTENSIONS) | {".docx"} | set(SUPPORTED_OCR_EXTENSIONS)
        )
        raise ValueError(f"Unsupported file '{suffix}'. Supported: {supported}")


def init_app(file_path: str, sheet: str | None = None) -> None:
    """Initialize the workspace from a single file (CLI/API/server bootstrap)."""
    _load_path_into_workspace(file_path, sheet=sheet)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous Workspace Agent")
    sub    = parser.add_subparsers(dest="mode")

    cli_p = sub.add_parser("cli")
    cli_p.add_argument("--file",    "-f", default="")
    cli_p.add_argument("--format",        default="markdown", choices=["markdown", "html"])
    cli_p.add_argument("--verbose", "-v", action="store_true")

    api_p = sub.add_parser("api")
    api_p.add_argument("--file",    "-f", required=True)
    api_p.add_argument("--host",          default="0.0.0.0")
    api_p.add_argument("--port",    "-p", type=int, default=8000)
    api_p.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()
    if getattr(args, "verbose", False):
        import os, logging  # noqa: PLC0415
        os.environ["VERBOSE"] = "1"
        logging.getLogger().setLevel(logging.DEBUG)

    if args.mode == "cli":
        run_cli(args.file, getattr(args, "format", "markdown"))
    elif args.mode == "api":
        try:
            init_app(args.file)
        except (FileNotFoundError, ValueError, LoaderError) as exc:
            print(f"ERROR: {exc}")
            sys.exit(1)
        print(f"Loaded {Path(args.file).name}; workspace: "
              f"{len(_workspace.spreadsheets)} sheet(s), "
              f"{len(_workspace.documents)} doc(s), "
              f"{len(_workspace.tables)} table(s)")
        uvicorn.run("app:fapi", host=args.host, port=args.port, reload=False)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

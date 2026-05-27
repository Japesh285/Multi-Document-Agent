"""
app.py — Autonomous Spreadsheet Intelligence Agent

Architecture
============
  User Query
      │
      ▼
  router.detect_intent()   → {intent, needs_pandas, needs_web_search, …}
      │
      ▼  (Python routing — no LangGraph overhead for top-level dispatch)
  ┌───────────────────────────────────────────────────────────────┐
  │ data_query        → single-step pandas execution              │
  │ report_generation → plan → LangGraph step loop → synthesize   │
  │ excel_modification→ mutation code → validate → write → save   │
  │ internet_research → mcp_tools search → summarize              │
  │ hybrid_analysis   → web search + analysis + synthesize        │
  │ chart_generation  → focused pandas + chart                    │
  └───────────────────────────────────────────────────────────────┘
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
from schema       import SchemaInfo, load_dataframe, build_schema, load_spreadsheet
from loaders      import LoadedSpreadsheet, LoaderError
from prompts      import normalize_query
from router       import detect_intent, IntentResult
from planner      import plan_steps
from analyzer     import run_step
from synthesizer  import synthesize_report
from memory       import AnalysisMemory, StepResult, WebFinding, ExcelUpdate
from executor     import safe_execute
from charts       import auto_chart, win_loss_chart
from mcp_tools        import (search_web, search_sports_news, summarize_results,
                              extract_teams_from_query, search_entities)
from excel_writer     import execute_column_mutation, save_excel
from entity_extractor import (extract_entities, extract_entities_from_text,
                               build_search_queries, infer_search_intent)
from verification     import (verify_match_dates, generate_verification_summary,
                               VerificationResult)
from router           import is_followup_reference, is_date_verification
from utils            import get_logger, print_section

log = get_logger("app")

# ---------------------------------------------------------------------------
# Runtime globals
# ---------------------------------------------------------------------------

_df:           pd.DataFrame                = pd.DataFrame()
_schema:       SchemaInfo | None           = None
_stream:       bool                        = False
_excel_path:   str                         = ""
_workbook:     LoadedSpreadsheet | None    = None   # full parsed workbook (all sheets)
_active_sheet: str                         = ""     # currently selected sheet name

# Session context — persists across queries within one CLI/API session
@dataclass
class _SessionCtx:
    last_df_result:  pd.DataFrame | None = None   # last successful pandas result
    last_query:      str                  = ""
    history:         list[str]            = field(default_factory=list)

    def update(self, query: str, df_result: pd.DataFrame | None) -> None:
        self.last_query  = query
        if df_result is not None and not df_result.empty:
            self.last_df_result = df_result
        self.history.append(query)
        if len(self.history) > 20:
            self.history = self.history[-20:]

_session = _SessionCtx()


def _recover_df_result(code: str, df: pd.DataFrame) -> pd.DataFrame | None:
    """
    Re-execute generated code and return `result` if it's a DataFrame/Series.
    Used to populate session context after a successful data query.
    """
    import io  # noqa: PLC0415
    import re as _re  # noqa: PLC0415
    from contextlib import redirect_stdout  # noqa: PLC0415

    if _re.search(r"\bimport\s+\w|\bopen\s*\(|\bexec\s*\(", code):
        return None
    sandbox = {"__builtins__": {}, "pd": pd, "np": __import__("numpy"), "df": df.copy()}
    local_vars: dict = {}
    try:
        with redirect_stdout(io.StringIO()):
            exec(code, sandbox, local_vars)  # noqa: S102
        result = local_vars.get("result")
        if isinstance(result, pd.DataFrame):
            return result
        if isinstance(result, pd.Series):
            return result.to_frame()
    except Exception:
        pass
    return None


def _get_schema() -> SchemaInfo:
    if _schema is None:
        raise RuntimeError("Schema not initialised — call init_app() first.")
    return _schema


def _writer_kwargs() -> dict:
    """Build kwargs for excel_writer.save_excel based on the active workbook."""
    kwargs: dict = {}
    if _active_sheet:
        kwargs["sheet_name"] = _active_sheet
    if _workbook is not None:
        if _workbook.delimiter is not None:
            kwargs["delimiter"] = _workbook.delimiter
        if _workbook.encoding is not None:
            kwargs["encoding"] = _workbook.encoding
    return kwargs


def set_active_sheet(name: str) -> SchemaInfo:
    """Switch the active sheet for the loaded workbook and rebuild SchemaInfo."""
    global _df, _schema, _active_sheet
    if _workbook is None:
        raise RuntimeError("No workbook loaded — call init_app() first.")
    _workbook.set_active_sheet(name)
    _df           = _workbook.df
    _active_sheet = name
    _schema       = build_schema(_df, _excel_path, loaded=_workbook)
    log.info("app: active sheet → %r (%d rows × %d cols)",
             name, _schema.shape[0], _schema.shape[1])
    return _schema


# ---------------------------------------------------------------------------
# AgentOutput — consistent return type for all handlers
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


# ---------------------------------------------------------------------------
# LangGraph — report generation pipeline (plan → N steps → synthesize)
# ---------------------------------------------------------------------------

class ReportState(TypedDict):
    query:         str
    output_format: str
    plan:          list[dict]
    step_idx:      int
    step_results:  list[dict]
    web_findings:  list[dict]    # pre-populated by hybrid handler
    report:        str


def _lg_plan(state: ReportState) -> ReportState:
    schema = _get_schema()
    t0     = time.perf_counter()
    steps  = plan_steps(state["query"], schema)
    log.debug("node_plan: %d steps in %.1fs", len(steps), time.perf_counter() - t0)

    if _stream:
        print(f"\n  Plan ({len(steps)} steps):")
        for i, s in enumerate(steps, 1):
            tool_tag = f"[{s.get('tool','pandas')}]" if s.get("tool") != "pandas" else ""
            print(f"    {i}. {s['description']} {tool_tag}")
        print()

    return {**state, "plan": steps, "step_idx": 0}


def _lg_step(state: ReportState) -> ReportState:
    schema  = _get_schema()
    step    = state["plan"][state["step_idx"]]
    memory  = AnalysisMemory.from_dicts(state["query"], state["step_results"])

    # Restore any web findings already collected (hybrid mode)
    for wf in state.get("web_findings", []):
        memory.add_web(WebFinding.from_dict(wf))

    total   = len(state["plan"])
    current = state["step_idx"] + 1

    # ── web_search step ──────────────────────────────────────────────────────
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

    # ── pandas step ──────────────────────────────────────────────────────────
    if _stream:
        print(f"  [{current}/{total}] {step['description']}")
        print("  ", end="", flush=True)

    result = run_step(step, schema, memory, _df, stream=_stream)

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
    schema = _get_schema()
    memory = AnalysisMemory.from_dicts(state["query"], state["step_results"])

    for wf in state.get("web_findings", []):
        memory.add_web(WebFinding.from_dict(wf))

    if _stream:
        print(f"  Synthesizing ({memory.success_count}/{len(memory.results)} steps OK) …\n")

    t0     = time.perf_counter()
    report = synthesize_report(
        state["query"], schema, memory,
        stream=_stream,
        output_format=state.get("output_format", "markdown"),
        save_to_disk=True,
    )
    log.debug("node_synthesize: %d chars in %.1fs", len(report), time.perf_counter() - t0)
    return {**state, "report": report}


def _lg_route(state: ReportState) -> str:
    return "more" if state["step_idx"] < len(state["plan"]) else "synthesize"


# Compile graph once
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
        "query":         normalize_query(query),
        "output_format": output_format,
        "plan":          [],
        "step_idx":      0,
        "step_results":  [],
        "web_findings":  web_findings or [],
        "report":        "",
    }
    return _report_agent.invoke(initial)


# ---------------------------------------------------------------------------
# Intent handlers
# ---------------------------------------------------------------------------

# ── 1. Data query ────────────────────────────────────────────────────────────

def handle_data_query(query: str) -> AgentOutput:
    """Single-step pandas execution for direct data questions."""
    schema = _get_schema()
    memory = AnalysisMemory(query=query)
    step   = {"id": "query", "description": normalize_query(query), "tool": "pandas"}

    if _stream:
        print("\n  ", end="", flush=True)

    result = run_step(step, schema, memory, _df, stream=_stream)

    if _stream:
        status = "✓" if result.ok else "✗"
        print(f"\n  {status} ({result.elapsed:.1f}s)")

    if result.ok:
        recovered = _recover_df_result(result.code, _df)
        _session.update(query, recovered)

    return AgentOutput(
        query=query,
        report=result.output if result.ok else f"Error: {result.error}",
        charts=[result.chart_path] if result.chart_path else [],
        step_results=[result.to_dict()],
        success=result.ok,
        error=result.error,
    )


# ── 2. Report generation ─────────────────────────────────────────────────────

def handle_report(query: str, output_format: str = "markdown") -> AgentOutput:
    """Full plan → execute → synthesize pipeline via LangGraph."""
    state  = _run_report_graph(query, output_format)
    memory = AnalysisMemory.from_dicts(query, state["step_results"])
    return AgentOutput(
        query=query,
        report=state["report"],
        charts=memory.chart_paths,
        step_results=state["step_results"],
        success=memory.success_count > 0,
    )


# ── 3. Excel modification ────────────────────────────────────────────────────

_EXCEL_SYSTEM = """\
You are a pandas expert. Generate code that creates values for a new DataFrame column.
IMPORTANT: `result` must be a pd.Series with length equal to len(df).
Never use import statements. df, pd, np are pre-loaded.\
"""

_EXCEL_USER = """\
DataFrame schema ({rows:,} rows):
{schema_block}

Task: {description}

The user wants to add/update a column. Write pandas code that:
1. Computes the new column values from existing df columns
2. Stores the result as a pd.Series in `result`
3. The Series must have exactly {rows} values (one per row)

Return ONLY raw Python code.\
"""


def handle_excel_modification(query: str) -> AgentOutput:
    """Generate mutation code → validate → write column → save Excel."""
    global _df
    schema   = _get_schema()

    # Ask the LLM for the column name and mutation code
    messages = [
        {"role": "system", "content": _EXCEL_SYSTEM},
        {"role": "user",   "content": _EXCEL_USER.format(
            rows=schema.shape[0],
            schema_block=schema.column_block,
            description=normalize_query(query),
        )},
    ]

    if _stream:
        print("\n  Generating mutation code …\n  ", end="", flush=True)

    from llm import call_chat, extract_code  # noqa: PLC0415
    raw  = call_chat(messages, stream_to_stdout=_stream)
    code = extract_code(raw)

    # Infer column name from query keywords
    col_name = _infer_column_name(query)

    if _stream:
        print(f"\n  Adding column '{col_name}' …")

    df_new, change, save_result = execute_column_mutation(
        code, _df, col_name, _excel_path, overwrite=True, **_writer_kwargs(),
    )

    if change.success:
        _df = df_new  # update global
        report = (
            f"Column **'{col_name}'** added successfully.\n"
            f"- Rows updated: {change.rows_affected:,}\n"
            f"- Saved to: {save_result.get('file', _excel_path)}\n"
            f"- Backup: {save_result.get('backup_path', 'N/A')}"
        )
        updates = [change.to_dict()]
    else:
        report  = f"Failed to add column: {change.error}"
        updates = []

    return AgentOutput(
        query=query,
        report=report,
        excel_updates=updates,
        step_results=[{
            "step_id": "excel_mutation", "description": query,
            "code": code, "output": report,
            "error": change.error, "elapsed": 0.0, "chart_path": None,
        }],
        success=change.success,
        error=change.error,
    )


def _infer_column_name(query: str) -> str:
    """Extract a plausible column name from the query."""
    import re  # noqa: PLC0415
    q = query.lower()
    patterns = [
        (r"risk\s*level", "Risk Level"),
        (r"injury\s*note", "Injury Notes"),
        (r"odds\s*change", "Odds Change"),
        (r"match\s*date",  "Match Date"),
        (r"add\s+(?:a\s+)?(?:new\s+)?(?:column\s+)?['\"]?(\w[\w\s]*)['\"]?\s+column", None),
    ]
    for pattern, fixed in patterns:
        if re.search(pattern, q):
            if fixed:
                return fixed
    # Generic: take words after "add" / "create"
    m = re.search(r"(?:add|create|append)\s+(?:a\s+)?(?:column\s+called\s+)?['\"]?(\w[\w ]*)['\"]?", q)
    if m:
        return m.group(1).strip().title()
    return "New Column"


# ── 4. Internet research ─────────────────────────────────────────────────────

def handle_web_search(query: str) -> AgentOutput:
    """
    Context-aware web search.

    Flow:
      1. If query references prior result ("verify those") → extract entities from
         session's last_df_result.
      2. Otherwise extract entities from the query text via LLM fallback.
      3. Build structured search queries per entity.
      4. If a date-verification request → delegate to handle_verification().
      5. Else search + summarise.
    """
    schema = _get_schema()

    # ── Decide entity source ──────────────────────────────────────────────────
    is_ref    = is_followup_reference(query)
    last_df   = _session.last_df_result
    entities  = None

    if is_ref and last_df is not None and not last_df.empty:
        if _stream:
            print(f"\n  Using context from last result ({len(last_df)} rows) …")
        entities = extract_entities(last_df, profile=schema.profile)
        log.debug("web_search: followup ref — %d matchups from session", len(entities.matchups))
    else:
        entities = extract_entities_from_text(query, schema.unique_sports)
        log.debug("web_search: text extraction — %d teams", len(entities.teams))

    # ── Date verification shortcut ────────────────────────────────────────────
    if is_date_verification(query) and (entities and not entities.is_empty()):
        if _stream:
            print("  Routing to date verification …")
        return handle_verification(query, entities)

    # ── Build structured queries ──────────────────────────────────────────────
    intent  = infer_search_intent(query)
    queries = build_search_queries(entities, intent_hint=intent) if not entities.is_empty() else [query]

    if _stream:
        print(f"\n  Searching ({len(queries)} queries, intent={intent}) …")
        for q in queries:
            print(f"    • {q}")
        print()

    # ── Execute searches ──────────────────────────────────────────────────────
    entity_results = search_entities(queries, use_news=(intent == "injury"))
    all_results    = [r for rs in entity_results.values() for r in rs]

    summary = summarize_results(all_results, context=query)
    report  = f"## Web Research: {query}\n\n{summary}"

    _session.update(query, None)

    return AgentOutput(
        query=query,
        report=report,
        web_results=all_results[:6],
        step_results=[{
            "step_id": "web_search", "description": query,
            "code": "", "output": summary,
            "error": None, "elapsed": 0.0, "chart_path": None,
        }],
        success=bool(summary),
    )


def handle_verification(query: str, entities=None) -> AgentOutput:
    """
    Date verification pipeline.

    For each matchup extracted from the DataFrame context:
      1. Build targeted search queries.
      2. Search and retrieve web results.
      3. Parse dates from results.
      4. Compare against spreadsheet Game Date.
      5. Return a structured verification report.
    Optionally writes result columns back to Excel.
    """
    if entities is None:
        schema  = _get_schema()
        last_df = _session.last_df_result
        if last_df is not None and not last_df.empty:
            entities = extract_entities(last_df, profile=schema.profile)
        else:
            entities = extract_entities_from_text(query, schema.unique_sports)

    if entities.is_empty():
        return AgentOutput(
            query=query,
            report="Could not extract any entities to verify. Run a data query first.",
            success=False,
            error="No entities",
        )

    matchups = entities.matchups
    if not matchups:
        # Fallback: build synthetic matchups from team/date lists
        from entity_extractor import Matchup  # noqa: PLC0415
        dates = entities.dates
        for i, team in enumerate(entities.teams):
            matchups.append(Matchup(
                selection=team, sport="",
                game_date=dates[i] if i < len(dates) else "",
                bet_type="", result="",
            ))

    if _stream:
        print(f"\n  Verifying {len(matchups)} matchup(s) …")

    # ── Per-matchup searches ──────────────────────────────────────────────────
    search_map: dict[str, list[dict]] = {}
    for matchup in matchups:
        from entity_extractor import EntitySet  # noqa: PLC0415
        queries = build_search_queries(
            EntitySet(matchups=[matchup]),
            intent_hint="fixture",
            max_queries=2,
        )
        q = queries[0] if queries else matchup.selection
        if _stream:
            print(f"    ↳ {q}")
        results = search_web(q, max_results=4)
        search_map[matchup.selection] = results

    # ── Verify dates ──────────────────────────────────────────────────────────
    vresults  = verify_match_dates(matchups, search_map)
    summary   = generate_verification_summary(vresults)
    report_md = summary.to_markdown()

    if _stream:
        print(f"\n  Verification complete: {summary.matched}/{summary.total} matched\n")

    # ── Optionally write verification columns to Excel ────────────────────────
    excel_updates: list[dict] = []
    if _excel_path and summary.results:
        excel_updates = _write_verification_columns(vresults)

    return AgentOutput(
        query=query,
        report=report_md,
        web_results=[r.to_dict() for r in vresults],
        excel_updates=excel_updates,
        step_results=[{
            "step_id": "verification",
            "description": query,
            "code": "",
            "output": report_md,
            "error": None,
            "elapsed": 0.0,
            "chart_path": None,
        }],
        success=summary.total > 0,
    )


def _write_verification_columns(vresults: list[VerificationResult]) -> list[dict]:
    """
    Write 'Verified Date', 'Date Match', 'Verification Confidence' columns
    back to the global DataFrame and save to Excel.
    """
    global _df
    if _df.empty or "Selection" not in _df.columns:
        return []

    result_map = {r.entity: r for r in vresults}

    df_new = _df.copy()
    for col in ("Verified Date", "Date Match", "Verification Confidence"):
        if col not in df_new.columns:
            df_new[col] = None

    for idx, row in df_new.iterrows():
        sel = str(row.get("Selection", "")).strip()
        vr  = result_map.get(sel)
        if vr:
            df_new.at[idx, "Verified Date"]             = vr.web_date or ""
            df_new.at[idx, "Date Match"]                = vr.match
            df_new.at[idx, "Verification Confidence"]   = round(vr.confidence, 3)

    from excel_writer import save_excel  # noqa: PLC0415
    save_result = save_excel(df_new, _excel_path, **_writer_kwargs())
    if save_result.get("success"):
        _df = df_new
        log.info("app: wrote verification columns to Excel")
        return [{"action": "add_verification_cols", "rows": len(vresults)}]
    return []


# ── 5. Hybrid analysis ───────────────────────────────────────────────────────

def handle_hybrid(query: str, intent: IntentResult, output_format: str = "markdown") -> AgentOutput:
    """
    Combines web search + multi-step analysis + synthesis.
    Example: "check pending NBA bets and add latest injury news"
    """
    schema = _get_schema()
    web_findings_dicts: list[dict] = []

    # Step A — web research first (if needed)
    if intent.needs_web_search:
        if _stream:
            print("\n  Phase 1: Web research …")
        teams   = extract_teams_from_query(query, schema.unique_sports)
        results = []
        for team in (teams[:3] if teams else [query]):
            results.extend(search_sports_news(team))
        summary = summarize_results(results, context=query)
        wf = WebFinding(search_query=query, summary=summary, raw_count=len(results))
        web_findings_dicts = [wf.to_dict()]

        if _stream:
            print(f"  Found {len(results)} results\n")

    # Step B — full report pipeline with web context injected
    if _stream:
        print("  Phase 2: Analysis …")

    state  = _run_report_graph(query, output_format, web_findings=web_findings_dicts)
    memory = AnalysisMemory.from_dicts(query, state["step_results"])
    for wfd in web_findings_dicts:
        memory.add_web(WebFinding.from_dict(wfd))

    # Step C — Excel write if requested
    excel_updates: list[dict] = []
    if intent.needs_excel_write and _excel_path:
        if _stream:
            print("  Phase 3: Updating Excel …")
        out = handle_excel_modification(query)
        excel_updates = out.excel_updates

    return AgentOutput(
        query=query,
        report=state["report"],
        charts=memory.chart_paths,
        web_results=[],
        excel_updates=excel_updates,
        step_results=state["step_results"],
        success=memory.success_count > 0,
    )


# ── 6. Chart generation ──────────────────────────────────────────────────────

def handle_chart(query: str) -> AgentOutput:
    """Focused single-step → chart, minimal report."""
    schema = _get_schema()
    memory = AnalysisMemory(query=query)
    step   = {"id": "chart_data", "description": normalize_query(query), "tool": "pandas"}

    if _stream:
        print("\n  Generating chart data …\n  ", end="", flush=True)

    result = run_step(step, schema, memory, _df, stream=_stream)

    charts  = [result.chart_path] if result.chart_path else []
    report  = f"## Chart: {query}\n\n"
    report += result.output if result.ok else f"Error: {result.error}"
    if charts:
        report += f"\n\nChart saved: {charts[0]}"

    return AgentOutput(
        query=query,
        report=report,
        charts=charts,
        step_results=[result.to_dict()],
        success=result.ok,
        error=result.error,
    )


# ---------------------------------------------------------------------------
# Master router
# ---------------------------------------------------------------------------

def process_query(query: str, output_format: str = "markdown") -> AgentOutput:
    """
    Detect intent and route to the appropriate handler.
    This is the single entry point for both CLI and API.
    """
    schema = _get_schema()
    t0     = time.perf_counter()

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

    # Date-verification requests are routed to handle_verification regardless of intent
    if is_date_verification(query):
        out = handle_verification(query)
    elif itype == "data_query":
        out = handle_data_query(query)
    elif itype == "report_generation":
        out = handle_report(query, output_format)
    elif itype == "excel_modification":
        out = handle_excel_modification(query)
    elif itype == "internet_research":
        out = handle_web_search(query)
    elif itype == "hybrid_analysis":
        out = handle_hybrid(query, intent, output_format)
    elif itype == "chart_generation":
        out = handle_chart(query)
    else:
        out = handle_data_query(query)

    out.intent     = intent.intent
    out.confidence = intent.confidence
    out.elapsed    = time.perf_counter() - t0
    return out


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

fapi = FastAPI(title="Autonomous Spreadsheet Agent", version="6.0")


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
    s = _get_schema()
    return {
        "file":           Path(s.file_path).name,
        "shape":          {"rows": s.shape[0], "columns": s.shape[1]},
        "columns":        s.columns,
        "unique_sports":  s.unique_sports,
        "unique_results": s.unique_results,
    }


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
  schema        show column names and types
  sheets        list all sheets in the loaded workbook
  use <sheet>   switch active sheet
  intent <q>    show what intent would be detected for a query
  html          toggle HTML output (default: markdown)
  exit          quit\
"""


def run_cli(file_path: str, output_format: str = "markdown") -> None:
    global _df, _schema, _stream, _excel_path, _workbook, _active_sheet
    _stream = True

    if not file_path:
        file_path = input("Enter path to spreadsheet (.xlsx/.xls/.xlsm/.csv/.tsv): ").strip().strip('"\'')

    try:
        loaded = load_spreadsheet(file_path)
    except (FileNotFoundError, ValueError, LoaderError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    _workbook     = loaded
    _active_sheet = loaded.active_sheet
    _df           = loaded.df
    _excel_path   = file_path
    _schema       = build_schema(_df, file_path, loaded=loaded)
    schema        = _schema

    print(f"\nLoaded  : {Path(file_path).name}  [{loaded.file_type}]")
    print(f"Shape   : {schema.shape[0]:,} rows × {schema.shape[1]} columns")
    if loaded.is_multi_sheet():
        sheet_summary = ", ".join(
            f"{s.name}({s.rows})" for s in loaded.sheet_info if not s.is_empty
        )
        print(f"Sheets  : {len(loaded.sheet_info)} — active='{loaded.active_sheet}'  ({sheet_summary})")
    if loaded.encoding:
        print(f"Encoding: {loaded.encoding}  delim={loaded.delimiter!r}")
    if loaded.sampled:
        print(f"Sample  : loaded {loaded.sample_rows:,} / ~{loaded.total_rows_est:,} rows")
    if loaded.warnings:
        for w in loaded.warnings[:5]:
            print(f"  ⚠ {w}")
    print(f"Domain  : {schema.domain}  ({schema.domain_confidence:.0%} confidence)")
    print(f"Columns : {', '.join(schema.columns)}")
    if schema.unique_sports:
        print(f"Sports  : {', '.join(schema.unique_sports)}")
    if schema.unique_results:
        print(f"Results : {', '.join(schema.unique_results)}")
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
            print(f"\n{schema.compact_summary()}\n")
            continue
        if cmd == "sheets":
            if _workbook is None or not _workbook.sheet_info:
                print("  (no workbook loaded)\n")
            else:
                for s in _workbook.sheet_info:
                    mark = " *" if s.is_primary else ""
                    print(f"  {s.name}{mark}  {s.rows} rows × {s.columns} cols")
                print()
            continue
        if cmd.startswith("use "):
            target = raw_query[4:].strip()
            try:
                schema = set_active_sheet(target)
                print(f"  Active sheet → '{target}' ({schema.shape[0]:,} × {schema.shape[1]})\n")
            except (KeyError, RuntimeError) as exc:
                print(f"  ERROR: {exc}\n")
            continue
        if cmd == "html":
            fmt = "html" if fmt == "markdown" else "markdown"
            print(f"  Output format: {fmt}\n")
            continue
        if cmd.startswith("intent "):
            q = raw_query[7:].strip()
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
            print(f"  Excel: {len(out.excel_updates)} update(s)")
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def init_app(file_path: str, sheet: str | None = None) -> None:
    global _df, _schema, _excel_path, _workbook, _active_sheet
    loaded        = load_spreadsheet(file_path, sheet=sheet)
    _workbook     = loaded
    _active_sheet = loaded.active_sheet
    _df           = loaded.df
    _excel_path   = file_path
    _schema       = build_schema(_df, file_path, loaded=loaded)


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous Spreadsheet Agent")
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
        except (FileNotFoundError, ValueError) as exc:
            print(f"ERROR: {exc}")
            sys.exit(1)
        s = _get_schema()
        print(f"Loaded {Path(args.file).name}: {s.shape[0]:,} rows × {s.shape[1]} columns")
        uvicorn.run("app:fapi", host=args.host, port=args.port, reload=False)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()

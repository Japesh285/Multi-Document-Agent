"""
prompts.py — Workspace runtime system prompt + dynamic state interpolation.

Single-LLM-call architecture. The system prompt is the user-supplied
"AI Workspace Runtime" prompt verbatim, followed by a dynamically
generated WORKSPACE STATE + DATA SNAPSHOTS block built deterministically
in Python (no probe LLM).

Public:
    normalize_query(query, *, domain=None) -> str
    build_workspace_system_prompt(workspace, query=None) -> str
    build_workspace_retry_message(error, code, workspace) -> str
    build_system_prompt(schema)                   — back-compat shim
    build_retry_message(error, code, schema)      — back-compat shim
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core import Workspace
    from schema import SchemaInfo


# ---------------------------------------------------------------------------
# Domain-aware query normalization (sports-betting aliases stay opt-in)
# ---------------------------------------------------------------------------

_BETTING_ALIASES: list[tuple[str, str]] = [
    (r"\bleagues?\b",                       "Sport"),
    (r"\bsport[s ]?categor\w+",             "Sport"),
    (r"\bteams?\b",                         "Selection"),
    (r"\bplayers?\b",                       "Selection"),
    (r"\bpicks?\b",                         "Selection"),
    (r"\bsides?\b",                         "Selection"),
    (r"\bmatch[_ ]?results?\b",             "Result"),
    (r"\boutcomes?\b",                      "Result"),
    (r"\bodds\b",                           "Code"),
    (r"\blines?\b",                         "Code"),
    (r"\bspread\b",                         "Code"),
    (r"\bwagered\b",                        "Stake"),
    (r"\bamounts? (bet|wagered|risked)\b",  "Stake"),
    (r"\bticket[_ ]?(?:numbers?|#?s?)\b",   "Ticket #"),
    (r"\bbookmakers?\b",                    "Provider"),
    (r"\bsportsbooks?\b",                   "Provider"),
    (r"\bbooks?\b",                         "Provider"),
    (r"\bmatch ?dates?\b",                  "Game Date"),
    (r"\bgame ?dates?\b",                   "Game Date"),
    (r"\bmatch ?times?\b",                  "Game Time"),
]

_BETTING_COMPILED = [(re.compile(p, re.IGNORECASE), r) for p, r in _BETTING_ALIASES]


def normalize_query(query: str, *, domain: str = "") -> str:
    """Apply domain-appropriate semantic aliases to the user's query."""
    if not query:
        return query
    if domain in ("", "sports_betting"):
        for pattern, replacement in _BETTING_COMPILED:
            query = pattern.sub(replacement, query)
    return query


# ---------------------------------------------------------------------------
# THE system prompt — verbatim from the workspace architecture spec.
# Dynamic workspace state + data snapshots are appended at runtime.
# ---------------------------------------------------------------------------

_WORKSPACE_SYSTEM_BODY = """\
# AI Workspace Runtime System Prompt

You are an expert AI workspace operator running inside a structured
programmable execution environment.

Your job is to:
- understand user intent
- reason across multiple documents and workspace objects
- generate precise Python execution steps
- manipulate files safely
- return human-readable results

You are NOT a chatbot. You are operating inside a persistent AI workspace
runtime where documents, spreadsheets, extracted tables, reports,
artifacts, and execution history already exist as structured Python
objects. You must reason using the workspace state instead of guessing.

## CORE PRINCIPLES
1. Python owns reality.
2. The workspace is the source of truth.
3. Never hallucinate file structure or column names.
4. Use the provided schema snapshots.
5. Generate deterministic code.
6. Keep execution minimal and efficient.
7. Always produce human-readable results.
8. Never redefine already provided objects.
9. Never import modules unless explicitly required and unavailable.
10. Prefer workspace tools and existing abstractions over raw file operations.

## EXECUTION ENVIRONMENT
The following objects are already available:

  workspace                   Persistent registry of all loaded objects.
  spreadsheets[name]          SpreadsheetObject — .df, .columns, .save(),
                              .schema, .capabilities
  documents[name]             DocumentObject — .paragraphs, .tables,
                              .save(), .schema, .capabilities
  tables[name]                TableObject — .df, .schema
  artifacts                   workspace.artifacts dict — write generated
                              chart paths / report paths / derived dfs here
  df                          active spreadsheet's DataFrame
  doc                         active document
  pd, np                      pandas, numpy

## WORKSPACE INTELLIGENCE
The DATA SNAPSHOTS section contains:
- actual column names
- real categorical values
- numeric / date ranges
- sample rows
- detected domains

Treat snapshots as authoritative. Never invent columns, categories,
sheets, sections, table names, IDs, or ranges.

## DOCUMENT REASONING RULES
You may receive spreadsheets, PDFs, DOCX files, extracted tables, OCR
text, and web findings. Reason across all workspace objects together.

Examples:
- extract a table from a PDF and place it into Excel
- compare spreadsheet values against a report
- generate a financial summary
- create charts
- validate dates using web results
- merge multiple documents into one report

## CODE GENERATION RULES
Return ONLY executable Python code. Do NOT explain the code, include
markdown fences, include prose, or include comments unless necessary.

The final answer MUST always be assigned to:

  result

`result` must be a HUMAN-READABLE STRING in the format the user's
question implies. Examples:

  user: "how many losses"
       result = "8 losses."
  user: "show me losses"
       result = "8 losses: Cleveland Guardians (200), Yankees (150), LA (220), …"
  user: "average stake"
       result = "Average stake: 145.20."
  user: "extract pricing table from contract and add to crm"
       result = "Added 3 pricing rows to spreadsheets['crm']. Saved to crm.xlsx."

Never assign a raw DataFrame to `result`. Compose a short sentence.
For large tables, state the count and list the top few entries.

## STRUCTURED ARTIFACT (for follow-up queries)

When your answer involves a list of entities (matches, clients, sections,
files), ALSO assign a structured dict to a variable named `artifact`.
This is how follow-up queries like "verify those dates" or "email those
clients" pick up the entities you just produced — they look at
workspace.artifacts['last_result'].

Format `artifact` as a dict with a `type` and an `items` list:

  user: "show NBA match dates"
       result = "Found 3 NBA matches: Lakers vs Nuggets (2026-05-12), …"
       artifact = {
           "type": "nba_matches",
           "items": [
               {"teams": "Lakers vs Nuggets", "date": "2026-05-12"},
               {"teams": "Celtics vs Heat",   "date": "2026-05-13"},
               {"teams": "Bucks vs 76ers",    "date": "2026-05-14"},
           ],
       }

  user: "list active clients"
       result = "3 active clients: Acme, Globex, Initech."
       artifact = {
           "type": "clients",
           "items": [
               {"name": "Acme",    "revenue": 10000},
               {"name": "Globex",  "revenue": 7500},
               {"name": "Initech", "revenue": 2200},
           ],
       }

For numeric / scalar answers (count, mean, sum) `artifact` is OPTIONAL.
Don't fabricate one if it doesn't fit naturally.

## FILE MODIFICATION RULES
When modifying files:
- preserve structure whenever possible
- avoid destructive overwrites
- use workspace object methods (`spreadsheets[x].save()`,
  `documents[x].save()`) — not raw file I/O
- save changes explicitly

## DATAFRAME RULES

A spreadsheet in this runtime is a **pandas DataFrame**, not an Excel UI grid.
NEVER address columns by Excel letters (A, B, C, K). ALWAYS use the
exact column names from the DATA SNAPSHOTS section.

  Bad:   df["K"]            df.iloc[:, 10]
  Good:  df["Sport"]        df["Result"]

When filtering text columns:
- use case-insensitive matching
- handle nulls safely
- avoid chained indexing
- preserve original data unless modification is requested

Preferred:
  df[df["Result"].astype(str).str.strip().str.lower() == "l"]

Avoid:
  df[df["Result"] == "Loss"]    (wrong value)
  df.K                          (Excel letter)
  df.iloc[:, 10]                (positional — fragile)

## PERFORMANCE
You are running on a local inference runtime. Minimize unnecessary
exploration, repeated scans, excessive loops, massive outputs. Prefer
vectorized pandas operations and direct schema-guided filtering.

## TOOL USAGE
Prefer structured workspace operations over raw Python.
Forbidden: exec(), eval(), subprocess, unsafe filesystem access.

## REPORT GENERATION
When users ask for reports: generate clean summaries, compute statistics,
create charts if appropriate, structure findings clearly, save outputs
into workspace artifacts. Reports should feel professional and readable.

## INTERNET ENRICHMENT
If web findings are available: use them for validation and enrichment,
cross-reference workspace data, never overwrite existing data blindly.

## HUMAN RESPONSE STYLE
The user receives normal human-readable answers.

Good:
  "I found 24 losses and added a summary sheet called 'Loss Analysis'."

Bad:
  "{'status': 'success'}"

Always prioritize clarity.

## FAILURE HANDLING
If execution is impossible: fail gracefully, explain what is missing,
do not hallucinate success. If data is ambiguous: state the ambiguity
clearly and proceed with the most likely interpretation only if strongly
supported by snapshots.

## FINAL RULE
You are an intelligent operator inside a deterministic workspace runtime.
Reason intelligently. Execute precisely. Return useful human-readable
results.
"""


# ---------------------------------------------------------------------------
# Dynamic workspace state — injected after the static system prompt
# ---------------------------------------------------------------------------


def _summarize_object(obj) -> str:
    return obj.summary() if hasattr(obj, "summary") else f"{obj.kind}:{obj.name}"


def _workspace_state_block(workspace: "Workspace") -> str:
    """Compact per-object inventory + active markers."""
    lines: list[str] = ["WORKSPACE STATE", "================"]

    if workspace.spreadsheets:
        lines.append(f"Spreadsheets ({len(workspace.spreadsheets)}):")
        for o in workspace.spreadsheets.values():
            lines.append(f"  spreadsheets[{o.name!r}]  → {_summarize_object(o)}")
            lines.append(f"    capabilities: {o.capabilities}")
    if workspace.documents:
        lines.append(f"Documents ({len(workspace.documents)}):")
        for o in workspace.documents.values():
            lines.append(f"  documents[{o.name!r}]  → {_summarize_object(o)}")
            lines.append(f"    capabilities: {o.capabilities}")
    if workspace.tables:
        lines.append(f"Tables ({len(workspace.tables)}):")
        for o in workspace.tables.values():
            lines.append(f"  tables[{o.name!r}]  → {_summarize_object(o)}")
    if workspace.artifacts:
        lines.append(f"Artifacts ({len(workspace.artifacts)}):")
        for k, v in workspace.artifacts.items():
            lines.append(f"  artifacts[{k!r}] = {str(v)[:80]}")

    active_lines: list[str] = []
    if workspace.active_spreadsheet:
        active_lines.append(f"spreadsheet → {workspace.active_spreadsheet.name}")
    if workspace.active_document:
        active_lines.append(f"document    → {workspace.active_document.name}")
    if active_lines:
        lines.append("Active:")
        for l in active_lines:
            lines.append(f"  {l}")

    return "\n".join(lines)


def _snapshots_block(workspace: "Workspace", *, max_chars: int = 6000) -> str:
    """Concatenate per-object static snapshots, budgeted."""
    objs = workspace.all_objects()
    if not objs:
        return "DATA SNAPSHOTS\n==============\n(workspace is empty)"
    lines: list[str] = ["DATA SNAPSHOTS", "=============="]
    remaining = max_chars
    for obj in objs:
        snip = getattr(obj, "snapshot", "") or ""
        if not snip.strip():
            continue
        header = f"── {obj.kind}:{obj.name} ──"
        block  = f"{header}\n{snip}"
        if len(block) > remaining:
            block = block[: max(0, remaining - 12)] + "\n…[truncated]"
            lines.append(block)
            break
        lines.append(block)
        remaining -= len(block) + 2
    return "\n\n".join(lines)


def _memory_block(workspace: "Workspace") -> str:
    mem = workspace.memory
    if mem.last_result is None and not mem.mutations:
        return ""
    parts: list[str] = ["EXECUTION HISTORY", "================="]
    if mem.last_result is not None:
        r = mem.last_result
        line = f"Last query: {r.query[:80]!r} → {r.summary or r.result_kind}"
        if r.is_dataframe():
            line += f" [{r.result_obj.shape[0]}×{r.result_obj.shape[1]}]"
        parts.append(line)
    if mem.mutations:
        parts.append("Recent mutations:")
        for m in mem.recent_mutations(3):
            parts.append(f"  {m.action} → {m.object_name}  ({m.detail})")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public assemblers
# ---------------------------------------------------------------------------


def build_workspace_system_prompt(workspace: "Workspace", query: str | None = None) -> str:
    """
    Build the full system prompt: static rules + live workspace state +
    static data snapshots + execution history.

    `query` is accepted for API compatibility but doesn't change the
    prompt — the LLM gets the full workspace state regardless, then the
    user message tells it the question.
    """
    parts: list[str] = [
        _WORKSPACE_SYSTEM_BODY.rstrip(),
        _workspace_state_block(workspace),
        _snapshots_block(workspace),
    ]
    mem = _memory_block(workspace)
    if mem:
        parts.append(mem)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Retry prompt (workspace-aware)
# ---------------------------------------------------------------------------


_RETRY_TEMPLATE = """\
Your previous code raised an error.

ERROR:
{error}

YOUR CODE:
{code}

Re-read the WORKSPACE STATE and DATA SNAPSHOTS above. Use the exact
column names and values from the snapshots. Fix the code and reassign
the human-readable string to `result`.

Return ONLY raw Python code — no prose, no markdown fences.\
"""


def build_workspace_retry_message(error: str, code: str, workspace: "Workspace") -> str:
    return _RETRY_TEMPLATE.format(error=error, code=code)


# ---------------------------------------------------------------------------
# Back-compat — schema-only callers
# ---------------------------------------------------------------------------


_LEGACY_SYSTEM_TEMPLATE = """\
You are a pandas expert.

DATAFRAME SCHEMA  ({rows:,} rows × {cols} columns)
{column_block}

KNOWN VALUES
  Categorical 1: {sports}
  Categorical 2: {results}

SAMPLE DATA (3 rows for context — do NOT hardcode these values)
{sample_block}

RULES:
  1. Use ONLY the exact column names listed above.
  2. Store a human-readable string answer in  result
  3. Never write import statements.
  4. Never use open(), exec(), eval(), or any file I/O.
  5. Return ONLY raw Python code.\
"""


def build_system_prompt(schema: "SchemaInfo") -> str:
    return _LEGACY_SYSTEM_TEMPLATE.format(
        rows=schema.shape[0],
        cols=schema.shape[1],
        column_block=schema.column_block,
        sports=", ".join(schema.unique_sports) or "N/A",
        results=", ".join(schema.unique_results) or "N/A",
        sample_block=schema.sample_block,
    )


_LEGACY_RETRY_TEMPLATE = """\
Your previous code failed.

ERROR:
{error}

YOUR CODE:
{code}

AVAILABLE COLUMNS:
{columns}

Fix it. Return ONLY raw Python code.\
"""


def build_retry_message(error: str, code: str, schema: "SchemaInfo") -> str:
    col_list = "\n".join(f"  {c!r}" for c in schema.columns)
    return _LEGACY_RETRY_TEMPLATE.format(error=error, code=code, columns=col_list)

"""
prompts.py — System prompt generation and query semantic normalization.

Workspace-first: prompts are built from a `Workspace` (not a single schema)
so the LLM sees every available object — spreadsheets, documents, tables —
plus active-object hints, recent-result memory, and the execution-env API.

A small back-compat helper (`build_system_prompt_from_schema`) is kept so
single-spreadsheet call sites that haven't been migrated still work.
"""

from __future__ import annotations
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core import Workspace
    from schema import SchemaInfo


# ---------------------------------------------------------------------------
# Semantic alias normalization
# ---------------------------------------------------------------------------
#
# Aliases are domain-specific. The default (general) alias list is empty —
# we no longer assume sports-betting columns. The historical betting aliases
# are kept under `_BETTING_ALIASES` and applied only when the active
# spreadsheet's domain is "sports_betting".

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
    """
    Apply domain-appropriate semantic aliases to the user's query.

    For backward compatibility callers may omit `domain`, in which case
    betting aliases are applied (preserves prior behavior on existing
    single-spreadsheet flows).
    """
    if not query:
        return query
    if domain in ("", "sports_betting"):
        for pattern, replacement in _BETTING_COMPILED:
            query = pattern.sub(replacement, query)
    return query


# ---------------------------------------------------------------------------
# Workspace-first system prompt
# ---------------------------------------------------------------------------

_WORKSPACE_SYSTEM_TEMPLATE = """\
You are an expert Python data analyst operating inside a programmable
workspace. Multiple objects — spreadsheets, documents, and extracted
tables — are pre-loaded as live Python variables. Choose the right
object(s) for each task and write code that orchestrates them.

EXECUTION ENVIRONMENT
The following names are already defined — do NOT import or redefine:
  workspace                  → registry of every loaded object
  spreadsheets[name]         → SpreadsheetObject  (.df, .columns, .save(),
                                                   .get_sheet(sheet_name))
  documents[name]            → DocumentObject     (.paragraphs list[str],
                                                   .headings, .sections,
                                                   .table_names list[str],
                                                   .replace_text(old,new),
                                                   .add_paragraph(text),
                                                   .add_table_from_df(df),
                                                   .save(path=None))
  tables[name]               → TableObject        (.df)
  df                         → active spreadsheet's DataFrame (if any)
  doc                        → active document   (if any)
  memory                     → workspace.memory (recent results & mutations)
  register_table(df, name=…) → save a derived DataFrame back to the workspace
  pd, np                     → pandas, numpy

{workspace_block}

RULES (no exceptions):
  1. Reference objects by the exact names shown above.
  2. Store the final answer in a variable named  result
  3. Never write import statements.
  4. Never use open(), exec(), eval(), or any file I/O —
     persistence happens through  obj.save()  calls only.
  5. For text matches on user-supplied strings, prefer case-insensitive
     comparisons:  s.str.lower() == "value"
  6. Return ONLY raw Python code — no markdown fences, no prose, no comments.\
"""


def build_workspace_system_prompt(workspace: "Workspace", query: str | None = None) -> str:
    """
    Build the system prompt that injects compiled workspace context.

    The workspace summary is the compact block from `core.compile_context`
    and never includes row-level data.
    """
    from core import compile_context  # noqa: PLC0415
    block = compile_context(workspace, query=query, include_memory=True)
    return _WORKSPACE_SYSTEM_TEMPLATE.format(workspace_block=block)


# ---------------------------------------------------------------------------
# Retry prompt (workspace-aware)
# ---------------------------------------------------------------------------

_WORKSPACE_RETRY_TEMPLATE = """\
Your previous code failed.

ERROR:
{error}

YOUR CODE:
{code}

AVAILABLE WORKSPACE OBJECTS:
{available}

Fix the code. Return ONLY corrected raw Python — no prose, no fences.\
"""


def build_workspace_retry_message(error: str, code: str, workspace: "Workspace") -> str:
    parts: list[str] = []
    if workspace.spreadsheets:
        parts.append("Spreadsheets:")
        for name, obj in workspace.spreadsheets.items():
            parts.append(f"  spreadsheets[{name!r}]  cols: {', '.join(obj.columns)}")
    if workspace.documents:
        parts.append("Documents:")
        for name, obj in workspace.documents.items():
            sections = ", ".join(s["name"] for s in obj.sections) or "(no sections)"
            parts.append(f"  documents[{name!r}]  sections: {sections}")
    if workspace.tables:
        parts.append("Tables:")
        for name, obj in workspace.tables.items():
            parts.append(f"  tables[{name!r}]  cols: {', '.join(obj.columns)}")
    return _WORKSPACE_RETRY_TEMPLATE.format(
        error=error, code=code, available="\n".join(parts) or "(workspace empty)",
    )


# ---------------------------------------------------------------------------
# Back-compat: schema-only system prompt
# ---------------------------------------------------------------------------

_LEGACY_SYSTEM_TEMPLATE = """\
You are a pandas expert.

EXECUTION ENVIRONMENT
The following variables are already available — do NOT import or redefine them:
  df   →  pandas DataFrame
  pd   →  pandas
  np   →  numpy

DATAFRAME SCHEMA  ({rows:,} rows × {cols} columns)
{column_block}

KNOWN VALUES
  Categorical 1: {sports}
  Categorical 2: {results}

SAMPLE DATA (3 rows for context only — do NOT hardcode these values)
{sample_block}

RULES (follow exactly, no exceptions):
  1. Use ONLY the exact column names listed in DATAFRAME SCHEMA above.
  2. Store the final answer in a variable named  result
  3. Never write import statements — df, pd, np are pre-loaded.
  4. Never use open(), exec(), eval(), or any file I/O.
  5. Return ONLY raw Python code — no markdown fences, no prose, no comments.\
"""


def build_system_prompt(schema: "SchemaInfo") -> str:
    """Legacy: schema-only prompt. Use build_workspace_system_prompt() for new code."""
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

AVAILABLE COLUMNS (use ONLY these exact names):
{columns}

Fix the code. Return ONLY the corrected raw Python code — no prose, no fences.\
"""


def build_retry_message(error: str, code: str, schema: "SchemaInfo") -> str:
    col_list = "\n".join(f"  {c!r}" for c in schema.columns)
    return _LEGACY_RETRY_TEMPLATE.format(error=error, code=code, columns=col_list)

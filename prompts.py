"""
prompts.py — System prompt generation and query semantic normalization.
"""

from __future__ import annotations
import re
from schema import SchemaInfo

# ---------------------------------------------------------------------------
# Semantic alias map — applied to user queries BEFORE sending to the model.
# Prevents hallucinated column names like 'league', 'team', 'match_result'.
# ---------------------------------------------------------------------------

# Each key is a regex pattern (case-insensitive); value is the real column name.
_ALIAS_MAP: list[tuple[str, str]] = [
    # Sport / league
    (r"\bleagues?\b",          "Sport"),
    (r"\bsport[s ]?categor\w+", "Sport"),

    # Selection / team / player
    (r"\bteams?\b",            "Selection"),
    (r"\bplayers?\b",          "Selection"),
    (r"\bpicks?\b",            "Selection"),
    (r"\bsides?\b",            "Selection"),

    # Result / outcome
    (r"\bmatch[_ ]?results?\b", "Result"),
    (r"\boutcomes?\b",          "Result"),
    (r"\bwon\b",                "Result"),

    # Code / odds / line
    (r"\bodds\b",               "Code"),
    (r"\blines?\b",             "Code"),
    (r"\bspread\b",             "Code"),

    # Stake / wager / amount
    (r"\bwagered\b",            "Stake"),
    (r"\bamounts? (bet|wagered|risked)\b", "Stake"),

    # Ticket
    (r"\bticket[_ ]?(?:numbers?|#?s?)\b", "Ticket #"),

    # Provider / book
    (r"\bbookmakers?\b",        "Provider"),
    (r"\bsportsbooks?\b",       "Provider"),
    (r"\bbooks?\b",             "Provider"),

    # Date / time
    (r"\bmatch ?dates?\b",      "Game Date"),
    (r"\bgame ?dates?\b",       "Game Date"),
    (r"\bmatch ?times?\b",      "Game Time"),
]

# Compiled once
_COMPILED_ALIASES = [(re.compile(p, re.IGNORECASE), r) for p, r in _ALIAS_MAP]


def normalize_query(query: str) -> str:
    """Replace semantic aliases with exact column names."""
    for pattern, replacement in _COMPILED_ALIASES:
        query = pattern.sub(replacement, query)
    return query


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_TEMPLATE = """\
You are a pandas expert assistant for sports betting analytics.

EXECUTION ENVIRONMENT
The following variables are already available — do NOT import or redefine them:
  df   →  pandas DataFrame  (the betting data)
  pd   →  pandas
  np   →  numpy

DATAFRAME SCHEMA  ({rows:,} rows × {cols} columns)
Column name               dtype
{column_block}

SEMANTIC MAPPINGS  (user may say → use this exact column)
  "league", "category"            →  Sport
  "team", "player", "pick"        →  Selection
  "match result", "outcome"       →  Result
  "odds", "line", "spread"        →  Code
  "wager", "amount wagered"       →  Stake
  "book", "sportsbook"            →  Provider

KNOWN VALUES
  Sport   : {sports}
  Result  : {results}

SAMPLE DATA (3 rows for context only — do NOT hardcode these values)
{sample_block}

RULES  (follow exactly, no exceptions)
  1. Only use the exact column names listed in DATAFRAME SCHEMA above.
  2. Store the final answer in a variable named  result
  3. Never write import statements — df, pd, np are pre-loaded.
  4. Never use open(), exec(), eval(), or any file I/O.
  5. Return ONLY raw Python code — no markdown fences, no prose, no comments.
  6. For text filters use case-insensitive matching:  .str.lower() == "value"
  7. You may reference previous questions and results in this conversation.\
"""


def build_system_prompt(schema: SchemaInfo) -> str:
    return _SYSTEM_TEMPLATE.format(
        rows=schema.shape[0],
        cols=schema.shape[1],
        column_block=schema.column_block,
        sports=", ".join(schema.unique_sports) or "N/A",
        results=", ".join(schema.unique_results) or "N/A",
        sample_block=schema.sample_block,
    )


# ---------------------------------------------------------------------------
# Retry message
# ---------------------------------------------------------------------------

_RETRY_TEMPLATE = """\
Your previous code failed.

ERROR:
{error}

YOUR CODE:
{code}

AVAILABLE COLUMNS (use ONLY these exact names):
{columns}

Fix the code. Return ONLY the corrected raw Python code — no prose, no fences.\
"""


def build_retry_message(error: str, code: str, schema: SchemaInfo) -> str:
    col_list = "\n".join(f"  {c!r}" for c in schema.columns)
    return _RETRY_TEMPLATE.format(error=error, code=code, columns=col_list)

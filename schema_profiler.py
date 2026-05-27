"""
schema_profiler.py — Lightweight DataFrame profiling and domain inference.

Infers column semantics and spreadsheet domain from column names and
value distributions — no hardcoded domain templates.

Semantic tags: temporal | monetary | entity | categorical | identifier |
               numeric  | boolean_like | high_cardinality | text | unknown
Domains:       sports_betting | finance | crm | inventory | hr |
               ecommerce | general
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field

import pandas as pd

from utils import get_logger

log = get_logger("schema_profiler")

# ---------------------------------------------------------------------------
# Domain keyword registry (column-name matching, no value inspection)
# ---------------------------------------------------------------------------

DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "sports_betting": [
        "stake", "odds", "bet", "selection", "result", "sport",
        "wager", "ticket", "parlay", "spread", "moneyline", "provider",
        "score", "handicap", "bookmaker",
    ],
    "finance": [
        "revenue", "profit", "loss", "price", "cost", "balance",
        "account", "transaction", "debit", "credit", "invoice",
        "tax", "budget", "expense", "asset", "liability",
    ],
    "crm": [
        "customer", "client", "lead", "contact", "email", "phone",
        "company", "deal", "pipeline", "opportunity", "stage",
        "prospect", "churn", "lifetime",
    ],
    "inventory": [
        "sku", "stock", "quantity", "item", "product", "warehouse",
        "supplier", "reorder", "unit", "barcode", "bin", "lot",
    ],
    "hr": [
        "employee", "salary", "department", "hire", "role",
        "manager", "headcount", "position", "payroll", "onboard",
    ],
    "ecommerce": [
        "order", "cart", "checkout", "shipping", "return",
        "refund", "discount", "coupon", "fulfillment", "basket",
    ],
}

# Column-name word sets for semantic tagging
_TEMPORAL_WORDS   = frozenset({"date", "time", "day", "month", "year", "timestamp",
                                "created", "updated", "modified", "at", "on", "when", "due"})
_MONETARY_WORDS   = frozenset({"price", "cost", "amount", "stake", "salary", "revenue",
                                "profit", "loss", "fee", "wage", "total", "balance",
                                "budget", "spend", "value", "usd", "gbp", "eur"})
_ENTITY_WORDS     = frozenset({"name", "team", "player", "company", "customer", "client",
                                "vendor", "selection", "product", "item", "supplier",
                                "employee", "contact", "person", "brand", "title"})
_CATEGORY_WORDS   = frozenset({"status", "result", "outcome", "type", "category", "class",
                                "tier", "stage", "label", "flag", "sport", "gender",
                                "region", "group", "division", "priority"})
_IDENTIFIER_WORDS = frozenset({"id", "code", "ticket", "sku", "ref", "number", "key",
                                "uuid", "serial", "order", "hash", "token"})
_NUMERIC_WORDS    = frozenset({"score", "count", "qty", "quantity", "rate", "ratio",
                                "percent", "pct", "rank", "age", "duration", "weight",
                                "height", "volume", "index"})

_DATE_STRING_RE = re.compile(
    r"\d{4}[-/]\d{2}[-/]\d{2}"
    r"|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
    r"|\d{1,2}\s+\w{3,9}\s+\d{4}",
)


def _looks_like_date_strings(samples: list) -> bool:
    hits = sum(1 for s in samples if _DATE_STRING_RE.search(str(s)))
    return hits >= max(1, len(samples) // 2)


def infer_column_semantic(col_name: str, series: pd.Series) -> str:
    """Infer semantic tag for a single column from its name and dtype/values."""
    name  = col_name.lower().replace("_", " ").replace("-", " ")
    words = set(name.split())

    # Fast path: name-based
    if words & _TEMPORAL_WORDS:
        return "temporal"
    if words & _MONETARY_WORDS or "$" in name or "£" in name:
        return "monetary"
    if words & _ENTITY_WORDS:
        return "entity"
    if words & _CATEGORY_WORDS:
        return "categorical"
    if words & _IDENTIFIER_WORDS or name.endswith((" id", " #", " no")):
        return "identifier"
    if words & _NUMERIC_WORDS:
        return "numeric"

    # Fallback: dtype / value distribution
    dtype = str(series.dtype)
    if "datetime" in dtype:
        return "temporal"
    if dtype.startswith(("float", "int")):
        n_unique = series.nunique(dropna=True)
        return "boolean_like" if n_unique <= 2 else "numeric"
    if dtype == "object":
        sample = series.dropna().head(10).tolist()
        if _looks_like_date_strings(sample):
            return "temporal"
        n_unique = series.nunique(dropna=True)
        n_rows   = max(series.notna().sum(), 1)
        uniq_rate = n_unique / n_rows
        if uniq_rate < 0.05 and n_unique <= 50:
            return "categorical"
        if uniq_rate > 0.80:
            return "high_cardinality"
        return "text"

    return "unknown"


def infer_domain(col_names: list[str]) -> tuple[str, float]:
    """
    Score column names against domain keyword lists.
    Returns (domain, confidence ∈ [0.40, 0.95]).
    """
    combined = " ".join(col_names).lower()
    scores: dict[str, int] = {
        domain: sum(1 for kw in kws if kw in combined)
        for domain, kws in DOMAIN_KEYWORDS.items()
    }
    scores = {d: s for d, s in scores.items() if s > 0}

    if not scores:
        return "general", 0.40

    best  = max(scores, key=scores.get)
    total = sum(scores.values())
    conf  = min(0.95, 0.40 + (scores[best] / total) * 0.55)

    log.debug("profiler: domain=%s conf=%.0f%% scores=%s", best, conf * 100, scores)
    return best, round(conf, 2)


# ---------------------------------------------------------------------------
# Profile dataclass
# ---------------------------------------------------------------------------

@dataclass
class DataFrameProfile:
    domain:                   str
    domain_confidence:        float
    shape:                    tuple[int, int]
    columns:                  list[str]
    dtypes:                   dict[str, str]
    semantic_hints:           dict[str, str]   # col → semantic tag
    numeric_columns:          list[str]
    date_columns:             list[str]
    categorical_columns:      list[str]
    entity_columns:           list[str]
    identifier_columns:       list[str]
    high_cardinality_columns: list[str]
    null_stats:               dict[str, float]  # col → null rate 0.0–1.0
    sample_rows:              list[dict]

    def compact_block(self) -> str:
        """One-liner per column with dtype and semantic tag — for LLM prompts."""
        lines = [
            f"Domain  : {self.domain}  ({self.domain_confidence:.0%} confidence)",
            f"Shape   : {self.shape[0]:,} rows × {self.shape[1]} columns",
            "Columns :",
        ]
        for col in self.columns:
            sem   = self.semantic_hints.get(col, "")
            tag   = f"  [{sem}]" if sem else ""
            dtype = self.dtypes.get(col, "")
            lines.append(f"  {col!r:<26} {dtype}{tag}")
        return "\n".join(lines)

    def semantic_context(self) -> str:
        """
        Compact semantic grouping for LLM prompt context.
        Replaces hardcoded 'league → Sport' style mappings.
        """
        parts: list[str] = []
        if self.entity_columns:
            parts.append(f"  Entity/Name columns : {', '.join(repr(c) for c in self.entity_columns[:6])}")
        if self.date_columns:
            parts.append(f"  Date/Time columns   : {', '.join(repr(c) for c in self.date_columns[:4])}")
        if self.numeric_columns:
            parts.append(f"  Numeric columns     : {', '.join(repr(c) for c in self.numeric_columns[:6])}")
        if self.categorical_columns:
            parts.append(f"  Category columns    : {', '.join(repr(c) for c in self.categorical_columns[:6])}")
        return "COLUMN GROUPS\n" + "\n".join(parts) + "\n" if parts else ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def profile_dataframe(df: pd.DataFrame) -> DataFrameProfile:
    """Profile any DataFrame and return a DataFrameProfile."""
    domain, domain_conf = infer_domain(list(df.columns))

    semantic_hints:    dict[str, str]   = {}
    numeric_cols:      list[str]        = []
    date_cols:         list[str]        = []
    cat_cols:          list[str]        = []
    entity_cols:       list[str]        = []
    id_cols:           list[str]        = []
    high_card_cols:    list[str]        = []
    null_stats:        dict[str, float] = {}

    n_rows = max(len(df), 1)

    for col in df.columns:
        series = df[col]
        sem    = infer_column_semantic(col, series)
        semantic_hints[col] = sem
        null_stats[col]     = round(series.isna().sum() / n_rows, 3)

        if sem == "temporal":
            date_cols.append(col)
        elif sem in ("numeric", "monetary", "boolean_like"):
            numeric_cols.append(col)
        elif sem == "categorical":
            cat_cols.append(col)
        elif sem == "entity":
            entity_cols.append(col)
        elif sem == "identifier":
            id_cols.append(col)
        elif sem == "high_cardinality":
            high_card_cols.append(col)

    sample_rows = [
        {c: (str(v) if pd.notna(v) else "") for c, v in row.items()}
        for _, row in df.dropna(how="all").head(5).iterrows()
    ]

    log.debug(
        "profiler: entity=%s  date=%s  numeric=%s  cat=%s",
        entity_cols, date_cols, numeric_cols[:4], cat_cols[:4],
    )

    return DataFrameProfile(
        domain=domain,
        domain_confidence=domain_conf,
        shape=df.shape,
        columns=list(df.columns),
        dtypes={c: str(t) for c, t in df.dtypes.items()},
        semantic_hints=semantic_hints,
        numeric_columns=numeric_cols,
        date_columns=date_cols,
        categorical_columns=cat_cols,
        entity_columns=entity_cols,
        identifier_columns=id_cols,
        high_cardinality_columns=high_card_cols,
        null_stats=null_stats,
        sample_rows=sample_rows,
    )

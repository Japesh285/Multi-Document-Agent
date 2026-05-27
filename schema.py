"""
schema.py — DataFrame introspection and schema metadata.

Generic loader — works with any supported spreadsheet (xlsx/xls/xlsm/csv/tsv).
Excludes Unnamed columns (noise/empty headers) but imposes no column whitelist.
Domain and semantics are inferred by schema_profiler.

Concrete file parsing is delegated to the `loaders` package. This module
intentionally keeps a thin back-compat surface (`load_dataframe`) so existing
callers keep working; new code should prefer `loaders.load_any()` directly.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from loaders import LoadedSpreadsheet, LoaderError, load_any
from schema_profiler import profile_dataframe


@dataclass
class SchemaInfo:
    file_path:          str
    shape:              tuple[int, int]
    columns:            list[str]
    dtypes:             dict[str, str]
    null_counts:        dict[str, int]
    sample:             pd.DataFrame
    unique_sports:      list[str]
    unique_results:     list[str]
    semantics:          dict[str, str]        # col → semantic tag
    column_block:       str = field(default="", repr=False)
    sample_block:       str = field(default="", repr=False)
    # Domain fields — populated by profiler
    domain:             str            = "general"
    domain_confidence:  float          = 0.5
    profile:            Any            = field(default=None, repr=False)
    # Ingestion metadata — populated when built from a LoadedSpreadsheet
    file_type:          str            = ""    # "xlsx"|"xls"|"xlsm"|"csv"|"tsv"
    sheets:             list[dict]     = field(default_factory=list)   # SheetInfo dicts
    active_sheet:       str            = ""
    encoding:           str | None     = None
    delimiter:          str | None     = None
    workbook_metadata:  dict           = field(default_factory=dict)
    ingestion_warnings: list[str]      = field(default_factory=list)

    def compact_summary(self) -> str:
        """One-liner per column with dtype and semantic hint."""
        lines = [
            f"Shape : {self.shape[0]:,} rows × {self.shape[1]} columns",
            "Columns:",
        ]
        for col in self.columns:
            dtype = self.dtypes.get(col, "")
            sem   = self.semantics.get(col, "")
            hint  = f"  — {sem}" if sem else ""
            lines.append(f"  {col!r:<24} {dtype}{hint}")
        if self.unique_sports:
            lines.append(f"Sport values  : {', '.join(self.unique_sports[:15])}")
        if self.unique_results:
            lines.append(f"Result values : {', '.join(self.unique_results)}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Loader — delegates to the universal loader package
# ---------------------------------------------------------------------------

def load_dataframe(file_path: str, sheet: str | None = None) -> pd.DataFrame:
    """
    Load the active sheet of any supported spreadsheet.

    Back-compat shim — internally uses `loaders.load_any()`. New call sites
    that need sheet info, encoding, or workbook metadata should call
    `loaders.load_any()` directly.
    """
    try:
        loaded = load_any(file_path, sheet=sheet)
    except LoaderError as exc:
        # Surface as the legacy exception types so older try/except blocks still work
        msg = str(exc)
        if "not found" in msg.lower() and "sheet" not in msg.lower():
            raise FileNotFoundError(msg) from exc
        raise ValueError(msg) from exc
    return loaded.df


def load_spreadsheet(file_path: str, sheet: str | None = None) -> LoadedSpreadsheet:
    """Full-fidelity load — returns the LoadedSpreadsheet (preferred for new code)."""
    return load_any(file_path, sheet=sheet)


# ---------------------------------------------------------------------------
# Schema builder
# ---------------------------------------------------------------------------

def _safe_unique(series: pd.Series, limit: int = 30) -> list[str]:
    return sorted(series.dropna().astype(str).unique().tolist())[:limit]


def build_schema(
    df: pd.DataFrame,
    file_path: str,
    *,
    loaded: LoadedSpreadsheet | None = None,
) -> SchemaInfo:
    """
    Build a SchemaInfo from any loaded DataFrame using the profiler.

    If a `LoadedSpreadsheet` is passed, ingestion metadata (file_type, sheets,
    encoding, delimiter, workbook_metadata) is propagated onto the SchemaInfo
    so the UI can render a richer context panel.
    """
    prof = profile_dataframe(df)

    null_counts = {c: int(df[c].isna().sum()) for c in df.columns}
    sample      = df.dropna(how="all").head(3)

    # Discover unique values for categoricals the profiler flagged
    unique_sports:  list[str] = []
    unique_results: list[str] = []
    for col in prof.categorical_columns:
        cl = col.lower()
        if cl in ("sport", "sports", "league", "competition"):
            unique_sports  = _safe_unique(df[col])
        elif cl in ("result", "outcome", "status"):
            unique_results = _safe_unique(df[col])

    col_lines = []
    for c in df.columns:
        sem  = prof.semantic_hints.get(c, "")
        hint = f"  [{sem}]" if sem else ""
        col_lines.append(f"  {c!r:<26} {prof.dtypes.get(c, '')}{hint}")
    column_block = "\n".join(col_lines)

    info = SchemaInfo(
        file_path=str(file_path),
        shape=df.shape,
        columns=list(df.columns),
        dtypes=prof.dtypes,
        null_counts=null_counts,
        sample=sample,
        unique_sports=unique_sports,
        unique_results=unique_results,
        semantics=prof.semantic_hints,
        column_block=column_block,
        sample_block=sample.to_string(index=False),
        domain=prof.domain,
        domain_confidence=prof.domain_confidence,
        profile=prof,
    )

    if loaded is not None:
        info.file_type          = loaded.file_type
        info.sheets             = [s.to_dict() for s in loaded.sheet_info]
        info.active_sheet       = loaded.active_sheet
        info.encoding           = loaded.encoding
        info.delimiter          = loaded.delimiter
        info.workbook_metadata  = dict(loaded.workbook_metadata)
        info.ingestion_warnings = list(loaded.warnings)

    return info

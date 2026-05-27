"""
loaders.normalize — shared, non-destructive DataFrame cleanup.

Applied by every loader before the DataFrame leaves the ingestion layer.
Cleanup is deliberately conservative:
  - never drops a non-empty row
  - never renames a column the user actually filled in
  - never silently coerces values that don't look like the inferred type
"""

from __future__ import annotations

import re
from typing import Iterable

import pandas as pd

from utils import get_logger

log = get_logger("loaders.normalize")


_UNNAMED_RE = re.compile(r"^Unnamed:\s*\d+$")
_DATE_HINT_WORDS = ("date", "time", "timestamp", "created", "updated", "modified")
_WHITESPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Column-name cleanup
# ---------------------------------------------------------------------------


def _clean_header(value: object) -> str:
    """Coerce a header value to a stripped string."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip()
    return _WHITESPACE_RE.sub(" ", s)


def _drop_unnamed_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Remove columns whose headers are pandas-generated 'Unnamed: N' AND fully empty."""
    dropped: list[str] = []
    keep: list = []
    for col in df.columns:
        col_str = str(col)
        if _UNNAMED_RE.match(col_str) and df[col].isna().all():
            dropped.append(col_str)
            continue
        keep.append(col)
    if dropped:
        df = df[keep]
    return df, dropped


def _dedupe_columns(columns: Iterable) -> list[str]:
    """Disambiguate duplicate column names by suffixing .1, .2, etc."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for col in columns:
        name = _clean_header(col) or "column"
        if name not in seen:
            seen[name] = 0
            out.append(name)
        else:
            seen[name] += 1
            out.append(f"{name}.{seen[name]}")
    return out


# ---------------------------------------------------------------------------
# Date coercion
# ---------------------------------------------------------------------------


def _coerce_date_columns(df: pd.DataFrame) -> list[str]:
    """In-place: convert columns whose names hint at dates to datetime64."""
    coerced: list[str] = []
    for col in df.columns:
        cl = str(col).lower()
        if any(w in cl for w in _DATE_HINT_WORDS):
            if df[col].dtype.kind in ("M",):  # already datetime
                continue
            try:
                converted = pd.to_datetime(df[col], errors="coerce")
                # Only adopt if at least one value parsed (avoid wrecking text cols
                # that happen to have 'date' in the name, e.g. "Mandate Notes").
                if converted.notna().any():
                    df[col] = converted
                    coerced.append(str(col))
            except Exception:
                pass
    return coerced


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_dataframe(
    df: pd.DataFrame,
    *,
    drop_unnamed: bool = True,
    dedupe_headers: bool = True,
    coerce_dates: bool = True,
    drop_empty_rows: bool = True,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Apply non-destructive cleanup to a freshly-loaded DataFrame.

    Returns (cleaned_df, list_of_warnings).
    """
    warnings: list[str] = []

    if df is None or df.empty:
        return df if df is not None else pd.DataFrame(), warnings

    if drop_unnamed:
        df, dropped = _drop_unnamed_columns(df)
        if dropped:
            log.debug("normalize: dropped %d unnamed empty columns", len(dropped))

    if dedupe_headers:
        original = list(df.columns)
        deduped  = _dedupe_columns(original)
        if deduped != [str(c) for c in original]:
            df.columns = deduped
            dup_count = sum(1 for n in deduped if "." in n and n.rsplit(".", 1)[-1].isdigit())
            if dup_count:
                warnings.append(f"Renamed {dup_count} duplicate column(s) with .N suffix")

    if drop_empty_rows:
        before = len(df)
        df = df.dropna(how="all").reset_index(drop=True)
        if before - len(df):
            log.debug("normalize: dropped %d fully-empty rows", before - len(df))

    if coerce_dates:
        coerced = _coerce_date_columns(df)
        if coerced:
            log.debug("normalize: coerced date columns %s", coerced)

    return df, warnings

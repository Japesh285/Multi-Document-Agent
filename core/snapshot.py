"""
core.snapshot — deterministic, LLM-free data snapshots.

Replaces the LLM-driven probe. Produces the same text content that
previously came back from `pytesseract`-style introspection, but built
in pure Python in < 100 ms instead of via a 3-5 s LLM round trip.

The returned string is injected into the task system prompt as
DATA SNAPSHOTS.

Public:
    build_snapshot(obj) -> str           — dispatches by type
    refresh_snapshot(obj) -> None        — rebuilds in place; sets obj.snapshot
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from utils import get_logger

if TYPE_CHECKING:
    from .workspace_objects import (
        DocumentObject, SpreadsheetObject, TableObject, WorkspaceObject,
    )

log = get_logger("snapshot")

# Tunables — three layers of compactness so prompts stay small.
#
# Layer 1 (always): shape, dtypes, column names — the structural skeleton.
# Layer 2 (compact): unique values for low-cardinality cols, numeric ranges.
# Layer 3 (sample/preview): head rows, section text previews.
#
# Local 14B models do best when the prompt is ≤ 4k tokens. We aim for
# snapshots ≤ ~1200 chars per object so 3-5 objects can coexist comfortably.

MAX_UNIQUE_VALUES_TO_LIST  = 20
MAX_NUMERIC_COLS_TO_RANGE  = 10
MAX_TEXT_COLS_TO_PROFILE   = 20
HEAD_ROWS                  = 3
DOC_SECTION_PREVIEW_CHARS  = 100
DOC_PARAGRAPH_PREVIEW      = 3
OCR_PAGES_TO_PREVIEW       = 2
OCR_PAGE_TEXT_PREVIEW      = 200
TABLE_HEAD_ROWS            = 3


# ---------------------------------------------------------------------------
# Spreadsheet
# ---------------------------------------------------------------------------


def _spreadsheet_snapshot(obj: "SpreadsheetObject") -> str:
    df = obj.df
    if df is None or df.empty:
        return f"{obj.name}: (empty spreadsheet)"

    rows, cols = df.shape
    lines: list[str] = [
        f"Shape: {rows} rows × {cols} columns",
    ]
    if obj.schema and obj.schema.domain and obj.schema.domain != "general":
        lines.append(
            f"Domain: {obj.schema.domain} "
            f"({obj.schema.domain_confidence:.0%} confidence)"
        )
    if obj.loaded and obj.loaded.is_multi_sheet():
        sheets = ", ".join(f"{s.name}({s.rows})" for s in obj.loaded.sheet_info if not s.is_empty)
        lines.append(f"Sheets in workbook: {sheets}  active='{obj.active_sheet}'")

    lines.append("Columns and dtypes:")
    for c, dt in df.dtypes.items():
        lines.append(f"  {c!r}: {dt}")

    lines.append(f"Head ({HEAD_ROWS} rows):")
    lines.append(df.head(HEAD_ROWS).to_string())

    # Categorical / text columns — list unique values when low-cardinality
    text_cols = list(df.select_dtypes(include=["object", "category", "string"]).columns)
    profiled = 0
    for c in text_cols:
        if profiled >= MAX_TEXT_COLS_TO_PROFILE:
            break
        try:
            uniques = df[c].dropna().unique()
        except Exception:
            continue
        if len(uniques) == 0:
            continue
        if len(uniques) <= MAX_UNIQUE_VALUES_TO_LIST:
            vals = [str(v) for v in uniques]
            lines.append(f"{c!r} values: {vals}")
        else:
            sample = [str(v) for v in uniques[:8]]
            lines.append(
                f"{c!r} unique count: {len(uniques)} (sample: {sample})"
            )
        profiled += 1

    # Numeric column ranges
    num_cols = list(df.select_dtypes(include="number").columns)
    if num_cols:
        lines.append("Numeric ranges:")
        for c in num_cols[:MAX_NUMERIC_COLS_TO_RANGE]:
            try:
                col = df[c].dropna()
                if col.empty:
                    continue
                mn, mx, mean = col.min(), col.max(), col.mean()
                lines.append(f"  {c!r}: min={mn} max={mx} mean={mean:.2f}")
            except Exception:
                continue

    # Date column ranges
    date_cols = list(df.select_dtypes(include=["datetime", "datetimetz"]).columns)
    if date_cols:
        lines.append("Date ranges:")
        for c in date_cols:
            try:
                col = df[c].dropna()
                if col.empty:
                    continue
                lines.append(f"  {c!r}: {col.min()} → {col.max()}")
            except Exception:
                continue

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


def _table_snapshot(obj: "TableObject") -> str:
    df = obj.df
    if df is None or df.empty:
        return f"{obj.name}: (empty table)"

    rows, cols = df.shape
    lines: list[str] = [
        f"Shape: {rows} × {cols}",
        f"Origin: {obj.source_kind}:{obj.source_object or '—'}"
        + (f" ({obj.source_locator})" if obj.source_locator else ""),
    ]
    if obj.nearby_heading:
        lines.append(f"Nearby heading: {obj.nearby_heading!r}")
    lines.append("Columns and dtypes:")
    for c, dt in df.dtypes.items():
        lines.append(f"  {c!r}: {dt}")
    lines.append(f"Head ({TABLE_HEAD_ROWS} rows):")
    lines.append(df.head(TABLE_HEAD_ROWS).to_string())
    # Low-cardinality unique values
    for c in df.select_dtypes(include=["object", "category", "string"]).columns[:10]:
        try:
            uniques = df[c].dropna().unique()
            if 0 < len(uniques) <= MAX_UNIQUE_VALUES_TO_LIST:
                lines.append(f"{c!r} values: {[str(v) for v in uniques]}")
        except Exception:
            continue
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Document (DOCX)
# ---------------------------------------------------------------------------


def _document_snapshot(obj: "DocumentObject") -> str:
    lines: list[str] = [
        f"Type: docx document",
        f"Paragraphs: {len(obj.paragraphs)}  (≈ {obj.word_count} words)",
    ]

    if obj.sections:
        lines.append("Sections (heading-derived):")
        for s in obj.sections:
            name = s.get("name", "")
            start = s.get("paragraph_start", 0)
            end   = s.get("paragraph_end", start)
            preview = ""
            if 0 <= start < len(obj.paragraphs):
                preview = obj.paragraphs[start][:DOC_SECTION_PREVIEW_CHARS]
                if start + 1 < end and len(preview) < DOC_SECTION_PREVIEW_CHARS:
                    # include a bit of the next paragraph too
                    nxt = obj.paragraphs[start + 1][: DOC_SECTION_PREVIEW_CHARS - len(preview) - 1]
                    preview = (preview + " " + nxt).strip()
            lines.append(f"  {name!r}  → {preview!r}")
    else:
        lines.append("Sections: (no headings detected)")
        lines.append("First paragraphs:")
        for p in obj.paragraphs[:DOC_PARAGRAPH_PREVIEW]:
            lines.append(f"  {p[:DOC_SECTION_PREVIEW_CHARS]!r}")

    if obj.table_names:
        lines.append(f"Extracted tables (workspace names): {obj.table_names}")

    # Metadata
    meta = obj.metadata or {}
    if meta.get("title") or meta.get("author"):
        lines.append(
            f"Metadata: title={meta.get('title','')!r} "
            f"author={meta.get('author','')!r}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# OCR document
# ---------------------------------------------------------------------------


def _ocr_snapshot(obj: "DocumentObject") -> str:
    ocr = obj.ocr  # OcrContext
    lines: list[str] = [
        f"Type: ocr_document  source={ocr.source_kind}  language={ocr.language}",
        f"Pages: {ocr.page_count}  tables extracted: {len(obj.table_names)}",
    ]
    cs = ocr.confidence_summary or {}
    if cs:
        lines.append(
            f"OCR confidence: mean={cs.get('word_confidence_mean', 0):.0f}  "
            f"low-conf words={cs.get('low_conf_word_count', 0)}  "
            f"quality={cs.get('quality', '')}"
        )

    # Preview a few pages
    for p in ocr.pages[:OCR_PAGES_TO_PREVIEW]:
        text = (p.text or "").strip()
        preview = text[:OCR_PAGE_TEXT_PREVIEW].replace("\n", " ")
        lines.append(
            f"page {p.page_index + 1} "
            f"[{p.width}×{p.height}, conf {p.word_confidence_mean:.0f}]: {preview!r}"
        )

    if obj.table_names:
        lines.append(f"Extracted table names: {obj.table_names}")
    if ocr.warnings:
        lines.append(f"Warnings: {list(ocr.warnings)[:3]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def build_snapshot(obj: "WorkspaceObject") -> str:
    """Return a compact, deterministic text snapshot for `obj`."""
    # Local imports to avoid a top-level cycle through workspace_objects
    from .workspace_objects import (  # noqa: PLC0415
        DocumentObject, SpreadsheetObject, TableObject,
    )
    try:
        if isinstance(obj, SpreadsheetObject):
            return _spreadsheet_snapshot(obj)
        if isinstance(obj, DocumentObject):
            if obj.is_ocr:
                return _ocr_snapshot(obj)
            return _document_snapshot(obj)
        if isinstance(obj, TableObject):
            return _table_snapshot(obj)
    except Exception as exc:
        log.warning("snapshot: build failed for %s:%s — %s", obj.kind, obj.name, exc)
        return f"{obj.kind}:{obj.name}  (snapshot unavailable: {exc})"
    return f"{obj.kind}:{obj.name}  (unsupported object type)"


def refresh_snapshot(obj: "WorkspaceObject") -> str:
    """Rebuild `obj.snapshot` from scratch and return the new text."""
    text = build_snapshot(obj)
    obj.snapshot = text
    log.debug("snapshot: refreshed %s:%s (%d chars)", obj.kind, obj.name, len(text))
    return text

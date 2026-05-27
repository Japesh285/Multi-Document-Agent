"""
loaders.docx.docx_loader — open a .docx and produce a DocumentObject.

Responsibilities:
  - Parse paragraphs (with style hint for headings)
  - Detect section boundaries from headings
  - Extract tables → DataFrames, attached to the document's
    `_pending_tables` attribute (the workspace registers them as
    TableObjects right after this returns)
  - Pull core document properties (title, author, dates)

This loader does NOT touch the workspace registry directly — that is the
caller's job (`Workspace.register_document_from_path`).
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from utils import get_logger

log = get_logger("loaders.docx")


class DocumentLoadError(Exception):
    """Could not load the .docx — corrupted, wrong format, etc."""


# Heading style names produced by Word / python-docx
_HEADING_PREFIX = "Heading"


def _classify_paragraph(para) -> tuple[str, int]:
    """Return (kind, heading_level). kind ∈ {'heading', 'body'}."""
    style_name = (para.style.name if para.style else "") or ""
    if style_name == "Title":
        return "heading", 0
    if style_name.startswith(_HEADING_PREFIX):
        # "Heading 1" → 1, "Heading 2" → 2, etc.
        tail = style_name[len(_HEADING_PREFIX):].strip()
        try:
            return "heading", int(tail) if tail else 1
        except ValueError:
            return "heading", 1
    return "body", 0


def _table_to_df(tbl) -> pd.DataFrame | None:
    """
    Convert a python-docx Table to a DataFrame. First row is treated as
    the header. Returns None if the table has 0 rows or 0 cols.
    """
    rows = list(tbl.rows)
    if not rows:
        return None
    matrix: list[list[str]] = []
    for row in rows:
        matrix.append([cell.text.strip() for cell in row.cells])
    if not matrix or not matrix[0]:
        return None
    header = matrix[0]
    # If header has duplicates or empty values, fall back to generic names
    if any(h == "" for h in header) or len(set(header)) != len(header):
        header = [f"col_{i+1}" for i in range(len(matrix[0]))]
        body = matrix
    else:
        body = matrix[1:]
    if not body:
        return None
    try:
        df = pd.DataFrame(body, columns=header)
    except Exception as exc:
        log.warning("docx: could not coerce table to DataFrame — %s", exc)
        return None
    return df


def _nearest_heading_text(paragraph_texts: list[str], heading_indices: list[int], target_index: int) -> str:
    """Find the most recent heading appearing before `target_index`."""
    last_h = ""
    for idx in heading_indices:
        if idx < target_index:
            last_h = paragraph_texts[idx]
        else:
            break
    return last_h


def load_docx(file_path: str, *, name: str | None = None):
    """
    Parse a .docx file and return a `core.DocumentObject`.

    The returned object has a temporary attribute `_pending_tables` holding
    a list of (DataFrame, nearby_heading_text) tuples for the workspace to
    register as TableObjects.
    """
    # Local import to keep core out of the loader's dependency graph
    from core.workspace_objects import DocumentObject  # noqa: PLC0415

    try:
        from docx import Document  # noqa: PLC0415
    except ImportError as exc:
        raise DocumentLoadError(
            "python-docx is not installed. Install with: pip install python-docx"
        ) from exc

    path = Path(file_path)
    if not path.exists():
        raise DocumentLoadError(f"File not found: {path}")
    if path.suffix.lower() != ".docx":
        raise DocumentLoadError(f"Not a .docx file: {path.name}")

    t0 = time.perf_counter()
    try:
        doc = Document(str(path))
    except Exception as exc:
        raise DocumentLoadError(f"Could not open {path.name}: {exc}") from exc

    # 1. Paragraphs + headings (track index for section assembly)
    paragraphs: list[str] = []
    headings:   list[dict] = []
    heading_indices: list[int] = []
    word_count = 0
    for i, para in enumerate(doc.paragraphs):
        text = para.text or ""
        paragraphs.append(text)
        word_count += len(text.split())
        kind, level = _classify_paragraph(para)
        if kind == "heading" and text.strip():
            headings.append({"level": level, "text": text, "index": i})
            heading_indices.append(i)

    # 2. Sections — paragraph ranges between top-level headings
    sections: list[dict] = []
    if headings:
        for h_idx, h in enumerate(headings):
            start = h["index"]
            end = (
                headings[h_idx + 1]["index"]
                if h_idx + 1 < len(headings)
                else len(paragraphs)
            )
            sections.append({
                "name":              h["text"],
                "level":             h["level"],
                "paragraph_start":   start,
                "paragraph_end":     end,
            })

    # 3. Tables → DataFrames (with nearest preceding heading)
    pending_tables: list[tuple[pd.DataFrame, str]] = []
    for t_idx, tbl in enumerate(doc.tables):
        df = _table_to_df(tbl)
        if df is None or df.empty:
            log.debug("docx: skipping empty table %d", t_idx)
            continue
        # Find a sensible nearby heading — python-docx doesn't expose
        # paragraph position of tables directly, so we approximate using
        # the table's index against the heading positions.
        heading_text = ""
        if heading_indices:
            # Approximate: tables roughly follow their section's heading
            heading_text = paragraphs[heading_indices[min(t_idx, len(heading_indices) - 1)]]
        pending_tables.append((df, heading_text))

    # 4. Metadata
    props = doc.core_properties
    metadata = {
        "title":            getattr(props, "title", "") or "",
        "author":           getattr(props, "author", "") or "",
        "created":          props.created.isoformat() if props.created else "",
        "modified":         props.modified.isoformat() if props.modified else "",
        "last_modified_by": getattr(props, "last_modified_by", "") or "",
        "category":         getattr(props, "category", "") or "",
    }

    elapsed = time.perf_counter() - t0
    log.info(
        "docx: loaded %s — %d paragraphs, %d headings, %d tables in %.2fs",
        path.name, len(paragraphs), len(headings), len(pending_tables), elapsed,
    )

    obj_name = name or path.stem
    document_obj = DocumentObject(
        name=obj_name,
        doc=doc,
        source_path=str(path),
        paragraphs=paragraphs,
        headings=headings,
        sections=sections,
        word_count=word_count,
        metadata=metadata,
    )
    # Stash for the workspace registrar to consume; popped after registration
    document_obj._pending_tables = pending_tables  # type: ignore[attr-defined]
    return document_obj

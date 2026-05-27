"""
core.workspace_objects — typed wrappers around the things the LLM can manipulate.

Three concrete object types live in the workspace:

  SpreadsheetObject  → wraps a LoadedSpreadsheet + SchemaInfo. Active sheet
                       is exposed as .df; other sheets via .get_sheet().
  DocumentObject     → wraps a python-docx Document. Exposes paragraphs,
                       headings, sections, tables, plus mutation helpers
                       (replace_text, add_paragraph, add_table_from_df).
  TableObject        → wraps a pandas DataFrame plus provenance info. Tables
                       extracted from DOCX are auto-registered; spreadsheets
                       can also be re-exposed as TableObjects for joins.

Every concrete object is a `WorkspaceObject` with a `summary()` method used
by the context compiler to build the LLM prompt without dumping data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from loaders import LoadedSpreadsheet
    from schema import SchemaInfo


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


@dataclass
class WorkspaceObject:
    """Common base for everything the workspace can register."""

    name: str                                    # unique within its category
    kind: str = ""                               # "spreadsheet" | "document" | "table"
    source_path: str = ""                        # original file path (may be "")
    created_at: str = ""                         # ISO timestamp
    metadata: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Must be overridden — used by the context compiler
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """One-line LLM-facing summary. Subclasses override."""
        return f"{self.kind}:{self.name}"

    def shape_hint(self) -> str:
        """Optional second-line shape/column hint."""
        return ""

    def to_metadata_dict(self) -> dict:
        """Serializable inventory entry."""
        return {
            "name":        self.name,
            "kind":        self.kind,
            "source_path": self.source_path,
            "created_at":  self.created_at,
            "summary":     self.summary(),
            "metadata":    dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# Spreadsheet
# ---------------------------------------------------------------------------


@dataclass
class SpreadsheetObject(WorkspaceObject):
    """A loaded workbook (one or more sheets) plus its profiled schema."""

    loaded: "LoadedSpreadsheet | None" = None
    schema: "SchemaInfo | None"        = None
    kind: str                          = "spreadsheet"

    # ------------------------------------------------------------------
    # Convenience properties — these are what LLM-generated code calls
    # ------------------------------------------------------------------

    @property
    def df(self) -> pd.DataFrame:
        return self.loaded.df if self.loaded else pd.DataFrame()

    @property
    def columns(self) -> list[str]:
        return list(self.df.columns)

    @property
    def shape(self) -> tuple[int, int]:
        return self.df.shape

    @property
    def active_sheet(self) -> str:
        return self.loaded.active_sheet if self.loaded else ""

    @property
    def sheets(self) -> dict[str, pd.DataFrame]:
        return self.loaded.sheets if self.loaded else {}

    def get_sheet(self, name: str) -> pd.DataFrame:
        if self.loaded is None:
            raise RuntimeError(f"Spreadsheet {self.name!r} has no underlying workbook")
        return self.loaded.get_sheet(name)

    def set_active_sheet(self, name: str) -> None:
        if self.loaded is None:
            raise RuntimeError(f"Spreadsheet {self.name!r} has no underlying workbook")
        self.loaded.set_active_sheet(name)

    def preview(self, n: int = 5) -> pd.DataFrame:
        return self.df.head(n)

    # ------------------------------------------------------------------
    # Mutation (safe write-back through excel_writer)
    # ------------------------------------------------------------------

    def save(self, *, sheet_name: str | None = None, backup: bool = True) -> dict:
        from excel_writer import save_excel  # noqa: PLC0415

        if not self.source_path:
            raise RuntimeError(f"Spreadsheet {self.name!r} has no source path")
        kwargs: dict = {"backup": backup}
        kwargs["sheet_name"] = sheet_name or self.active_sheet
        if self.loaded:
            if self.loaded.delimiter is not None:
                kwargs["delimiter"] = self.loaded.delimiter
            if self.loaded.encoding is not None:
                kwargs["encoding"] = self.loaded.encoding
        return save_excel(self.df, self.source_path, **kwargs)

    # ------------------------------------------------------------------
    # Context summary
    # ------------------------------------------------------------------

    def summary(self) -> str:
        rows, cols = self.shape
        cols_str = ", ".join(self.columns[:8])
        if len(self.columns) > 8:
            cols_str += ", …"
        sheet_tag = ""
        if self.loaded and self.loaded.is_multi_sheet():
            sheet_tag = f" [active={self.active_sheet!r}, {len(self.loaded.sheets)} sheets]"
        return f"{self.name}  [{rows}×{cols}]{sheet_tag}  cols: {cols_str}"

    def shape_hint(self) -> str:
        if self.schema and self.schema.domain and self.schema.domain != "general":
            return f"domain={self.schema.domain} ({self.schema.domain_confidence:.0%})"
        return ""


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


@dataclass
class TableObject(WorkspaceObject):
    """
    A standalone DataFrame with provenance — e.g. a table lifted from a
    DOCX document or a derived view created in workspace code.
    """

    df: pd.DataFrame                  = field(default_factory=pd.DataFrame)
    source_kind: str                  = ""    # "document" | "spreadsheet" | "derived"
    source_object: str                = ""    # parent object's workspace name
    source_locator: str               = ""    # e.g. "table_index:2" or "sheet:Pricing"
    nearby_heading: str               = ""    # heading text closest to the table
    kind: str                         = "table"

    @property
    def columns(self) -> list[str]:
        return list(self.df.columns)

    @property
    def shape(self) -> tuple[int, int]:
        return self.df.shape

    def preview(self, n: int = 5) -> pd.DataFrame:
        return self.df.head(n)

    def summary(self) -> str:
        rows, cols = self.shape
        cols_str = ", ".join(self.columns[:6])
        if len(self.columns) > 6:
            cols_str += ", …"
        prov = ""
        if self.source_object:
            prov = f" (from {self.source_kind}:{self.source_object}"
            if self.nearby_heading:
                prov += f", near §{self.nearby_heading!r}"
            prov += ")"
        return f"{self.name}  [{rows}×{cols}]{prov}  cols: {cols_str}"


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


@dataclass
class DocumentObject(WorkspaceObject):
    """A python-docx Document plus its extracted structure."""

    doc: Any                      = None        # python-docx Document
    paragraphs: list[str]         = field(default_factory=list)   # plain text per para
    headings:   list[dict]        = field(default_factory=list)   # [{level, text, index}]
    sections:   list[dict]        = field(default_factory=list)   # [{name, paragraph_start, paragraph_end}]
    table_names: list[str]        = field(default_factory=list)   # workspace names of extracted tables
    word_count: int               = 0
    kind: str                     = "document"

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def text(self) -> str:
        """Full plain-text body."""
        return "\n".join(self.paragraphs)

    def find_paragraphs(self, query: str, *, case_sensitive: bool = False) -> list[tuple[int, str]]:
        """Return (index, text) for paragraphs containing `query`."""
        if case_sensitive:
            return [(i, p) for i, p in enumerate(self.paragraphs) if query in p]
        ql = query.lower()
        return [(i, p) for i, p in enumerate(self.paragraphs) if ql in p.lower()]

    def section_text(self, section_name: str) -> str:
        """Concatenate all paragraphs inside a named section (case-insensitive match)."""
        for sec in self.sections:
            if sec["name"].lower() == section_name.lower():
                return "\n".join(self.paragraphs[sec["paragraph_start"]:sec["paragraph_end"]])
        return ""

    # ------------------------------------------------------------------
    # Mutation (every call MUST go through safe helpers; raw doc is exposed
    # but writes happen via .save() which always backs up first)
    # ------------------------------------------------------------------

    def replace_text(self, old: str, new: str) -> int:
        """In-memory text replacement across all paragraphs. Returns count."""
        if self.doc is None:
            return 0
        count = 0
        for para in self.doc.paragraphs:
            if old in para.text:
                for run in para.runs:
                    if old in run.text:
                        run.text = run.text.replace(old, new)
                        count += 1
                # If old spans runs the simple per-run loop misses it; cover
                # that by checking the full paragraph text and rewriting.
                if old in para.text:
                    para.text = para.text.replace(old, new)
                    count += 1
        # Refresh cached plain text
        self.paragraphs = [p.text for p in self.doc.paragraphs]
        return count

    def add_paragraph(self, text: str, style: str | None = None) -> None:
        if self.doc is None:
            raise RuntimeError(f"Document {self.name!r} has no underlying Document")
        self.doc.add_paragraph(text, style=style) if style else self.doc.add_paragraph(text)
        self.paragraphs.append(text)

    def add_heading(self, text: str, level: int = 1) -> None:
        if self.doc is None:
            raise RuntimeError(f"Document {self.name!r} has no underlying Document")
        self.doc.add_heading(text, level=level)
        self.paragraphs.append(text)
        self.headings.append({"level": level, "text": text, "index": len(self.paragraphs) - 1})

    def add_table_from_df(self, df: pd.DataFrame, *, style: str = "Light List") -> None:
        """Append a DataFrame as a Word table."""
        if self.doc is None:
            raise RuntimeError(f"Document {self.name!r} has no underlying Document")
        rows, cols = df.shape
        tbl = self.doc.add_table(rows=rows + 1, cols=cols)
        try:
            tbl.style = style
        except Exception:
            pass
        for ci, col in enumerate(df.columns):
            tbl.cell(0, ci).text = str(col)
        for ri in range(rows):
            for ci in range(cols):
                tbl.cell(ri + 1, ci).text = str(df.iat[ri, ci])

    def save(self, path: str | None = None, *, backup: bool = True) -> dict:
        """Save the docx. Backs up the original first by default."""
        from excel_writer import backup_excel  # noqa: PLC0415  (shared backup util)
        if self.doc is None:
            return {"success": False, "error": "no underlying Document"}
        target = Path(path or self.source_path)
        if not str(target):
            return {"success": False, "error": "no path to save to"}

        backup_path = ""
        if backup and target.exists():
            backup_path = backup_excel(str(target))

        try:
            self.doc.save(str(target))
            return {
                "success":     True,
                "file":        str(target),
                "backup_path": backup_path,
                "file_type":   "docx",
                "word_count":  self.word_count,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc),
                    "file": str(target), "backup_path": backup_path}

    # ------------------------------------------------------------------
    # Context summary
    # ------------------------------------------------------------------

    def summary(self) -> str:
        section_names = [s["name"] for s in self.sections if s.get("name")]
        if not section_names and self.headings:
            section_names = [h["text"] for h in self.headings[:5]]
        section_str = ", ".join(section_names[:5]) or "—"
        tcount = len(self.table_names)
        return (
            f"{self.name}  ({len(self.paragraphs)} paragraphs, "
            f"{tcount} table{'s' if tcount != 1 else ''}, "
            f"~{self.word_count} words)  sections: {section_str}"
        )

    def shape_hint(self) -> str:
        if self.table_names:
            return f"tables: {', '.join(self.table_names[:4])}"
        return ""

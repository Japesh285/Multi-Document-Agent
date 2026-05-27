"""
core.workspace_manager — central registry for every object the AI can manipulate.

A `Workspace` holds three parallel registries plus a memory:

    spreadsheets : dict[str, SpreadsheetObject]
    documents    : dict[str, DocumentObject]
    tables       : dict[str, TableObject]
    memory       : WorkspaceMemory

Names are unique *within* each category but free across categories (so a
spreadsheet and a document can both be called "contract"). Conflicts are
resolved by appending `_2`, `_3`, … on registration.

`register_spreadsheet_from_path` and `register_document_from_path` are the
canonical entry points — they delegate to the loader layer, profile the
result, and register everything (including DOCX tables) in one call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from utils import get_logger

from .workspace_memory import WorkspaceMemory
from .workspace_objects import (
    DocumentObject,
    SpreadsheetObject,
    TableObject,
    WorkspaceObject,
)

log = get_logger("workspace")


def _slugify(s: str) -> str:
    """Convert an arbitrary string into a Python-friendly object name."""
    s = re.sub(r"[^\w]+", "_", s.strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "object"


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


@dataclass
class Workspace:
    spreadsheets: dict[str, SpreadsheetObject] = field(default_factory=dict)
    documents:    dict[str, DocumentObject]    = field(default_factory=dict)
    tables:       dict[str, TableObject]       = field(default_factory=dict)
    memory:       WorkspaceMemory              = field(default_factory=WorkspaceMemory)
    metadata:     dict                         = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------

    def _unique_name(self, desired: str, registry: dict) -> str:
        base = _slugify(desired)
        if base not in registry:
            return base
        i = 2
        while f"{base}_{i}" in registry:
            i += 1
        return f"{base}_{i}"

    def get(self, name: str) -> WorkspaceObject | None:
        """Look up an object by name across all three registries."""
        return (
            self.spreadsheets.get(name)
            or self.documents.get(name)
            or self.tables.get(name)
        )

    def all_objects(self) -> list[WorkspaceObject]:
        out: list[WorkspaceObject] = []
        out.extend(self.spreadsheets.values())
        out.extend(self.documents.values())
        out.extend(self.tables.values())
        return out

    def is_empty(self) -> bool:
        return not (self.spreadsheets or self.documents or self.tables)

    # ------------------------------------------------------------------
    # Direct registration
    # ------------------------------------------------------------------

    def register_spreadsheet(self, obj: SpreadsheetObject) -> SpreadsheetObject:
        if obj.name in self.spreadsheets:
            obj.name = self._unique_name(obj.name, self.spreadsheets)
        obj.created_at = obj.created_at or _now()
        self.spreadsheets[obj.name] = obj
        self.memory.touch(obj.name)
        log.info("workspace: registered spreadsheet %r (%d×%d)",
                 obj.name, obj.shape[0], obj.shape[1])
        return obj

    def register_document(self, obj: DocumentObject) -> DocumentObject:
        if obj.name in self.documents:
            obj.name = self._unique_name(obj.name, self.documents)
        obj.created_at = obj.created_at or _now()
        self.documents[obj.name] = obj
        self.memory.touch(obj.name)
        log.info("workspace: registered document %r (%d paragraphs, %d tables)",
                 obj.name, len(obj.paragraphs), len(obj.table_names))
        return obj

    def register_table(self, obj: TableObject) -> TableObject:
        if obj.name in self.tables:
            obj.name = self._unique_name(obj.name, self.tables)
        obj.created_at = obj.created_at or _now()
        self.tables[obj.name] = obj
        self.memory.touch(obj.name)
        log.info("workspace: registered table %r (%d×%d) from %s",
                 obj.name, obj.shape[0], obj.shape[1], obj.source_object or "—")
        return obj

    # ------------------------------------------------------------------
    # Path-based loaders (the canonical entry points)
    # ------------------------------------------------------------------

    def register_spreadsheet_from_path(
        self,
        file_path: str,
        *,
        name: str | None = None,
        sheet: str | None = None,
        expose_sheets_as_tables: bool = False,
    ) -> SpreadsheetObject:
        from loaders import load_any  # noqa: PLC0415
        from schema import build_schema  # noqa: PLC0415

        loaded   = load_any(file_path, sheet=sheet)
        schema   = build_schema(loaded.df, file_path, loaded=loaded)
        base     = name or Path(file_path).stem
        obj_name = self._unique_name(base, self.spreadsheets)

        obj = SpreadsheetObject(
            name=obj_name,
            loaded=loaded,
            schema=schema,
            source_path=str(file_path),
            metadata={
                "file_type":    loaded.file_type,
                "encoding":     loaded.encoding,
                "delimiter":    loaded.delimiter,
                "is_multi_sheet": loaded.is_multi_sheet(),
            },
        )
        self.register_spreadsheet(obj)

        if expose_sheets_as_tables and loaded.is_multi_sheet():
            for sname, sdf in loaded.sheets.items():
                if sdf.empty:
                    continue
                tbl = TableObject(
                    name=self._unique_name(f"{obj_name}__{sname}", self.tables),
                    df=sdf,
                    source_kind="spreadsheet",
                    source_object=obj_name,
                    source_locator=f"sheet:{sname}",
                )
                self.register_table(tbl)

        return obj

    def register_document_from_path(
        self,
        file_path: str,
        *,
        name: str | None = None,
        extract_tables: bool = True,
    ) -> DocumentObject:
        from loaders.docx import load_docx  # noqa: PLC0415

        base     = name or Path(file_path).stem
        obj_name = self._unique_name(base, self.documents)
        doc_obj  = load_docx(file_path, name=obj_name)
        self.register_document(doc_obj)

        if extract_tables and getattr(doc_obj, "_pending_tables", None):
            for idx, (tbl_df, heading) in enumerate(doc_obj._pending_tables):
                tbl_name = self._unique_name(
                    f"{obj_name}__table_{idx+1}", self.tables
                )
                tbl = TableObject(
                    name=tbl_name,
                    df=tbl_df,
                    source_kind="document",
                    source_object=obj_name,
                    source_locator=f"table_index:{idx}",
                    nearby_heading=heading or "",
                )
                self.register_table(tbl)
                doc_obj.table_names.append(tbl_name)
            # Strip the temporary attribute now that registration is done
            del doc_obj._pending_tables

        return doc_obj

    def register_dataframe_as_table(
        self,
        df: pd.DataFrame,
        *,
        name: str,
        source_kind: str = "derived",
        source_object: str = "",
        source_locator: str = "",
        nearby_heading: str = "",
    ) -> TableObject:
        tbl_name = self._unique_name(name, self.tables)
        tbl = TableObject(
            name=tbl_name,
            df=df,
            source_kind=source_kind,
            source_object=source_object,
            source_locator=source_locator,
            nearby_heading=nearby_heading,
        )
        return self.register_table(tbl)

    # ------------------------------------------------------------------
    # Removal
    # ------------------------------------------------------------------

    def remove(self, name: str) -> bool:
        """Remove an object from whichever registry holds it."""
        for registry in (self.spreadsheets, self.documents, self.tables):
            if name in registry:
                del registry[name]
                if name in self.memory.active_objects:
                    self.memory.active_objects.remove(name)
                log.info("workspace: removed %r", name)
                return True
        return False

    def clear(self) -> None:
        self.spreadsheets.clear()
        self.documents.clear()
        self.tables.clear()
        self.memory = WorkspaceMemory()
        self.metadata.clear()
        log.info("workspace: cleared")

    # ------------------------------------------------------------------
    # Convenience views
    # ------------------------------------------------------------------

    @property
    def active_spreadsheet(self) -> SpreadsheetObject | None:
        """Most-recently-touched spreadsheet, falling back to the only one if there's just one."""
        for name in self.memory.active_objects:
            if name in self.spreadsheets:
                return self.spreadsheets[name]
        if len(self.spreadsheets) == 1:
            return next(iter(self.spreadsheets.values()))
        return None

    @property
    def active_document(self) -> DocumentObject | None:
        for name in self.memory.active_objects:
            if name in self.documents:
                return self.documents[name]
        if len(self.documents) == 1:
            return next(iter(self.documents.values()))
        return None

    def inventory_dict(self) -> dict:
        return {
            "spreadsheets": [s.to_metadata_dict() for s in self.spreadsheets.values()],
            "documents":    [d.to_metadata_dict() for d in self.documents.values()],
            "tables":       [t.to_metadata_dict() for t in self.tables.values()],
            "active":       {
                "spreadsheet": self.active_spreadsheet.name if self.active_spreadsheet else None,
                "document":    self.active_document.name    if self.active_document    else None,
                "most_recent": self.memory.most_recent_object,
            },
            "memory":       self.memory.snapshot(),
        }

"""
loaders.base_loader — abstract loader interface + shared data structures.

All concrete loaders (Excel, CSV) return a `LoadedSpreadsheet`.
Callers should not import format-specific loaders directly — use
`loaders.load_any()` or `loaders.get_loader()` from `loader_router`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LoaderError(Exception):
    """Base class for all loader failures. Message is user-safe."""


class UnsupportedFormatError(LoaderError):
    """File extension is not recognised by any registered loader."""


class CorruptedFileError(LoaderError):
    """File appears to be a supported format but cannot be parsed."""


class EmptyFileError(LoaderError):
    """File parsed cleanly but contains no usable data."""


# ---------------------------------------------------------------------------
# Per-sheet info
# ---------------------------------------------------------------------------


@dataclass
class SheetInfo:
    """Lightweight metadata for one sheet (or the single CSV/TSV table)."""

    name:          str
    rows:          int
    columns:       int
    column_names:  list[str] = field(default_factory=list)
    is_primary:    bool      = False
    is_empty:      bool      = False

    def to_dict(self) -> dict:
        return {
            "name":         self.name,
            "rows":         self.rows,
            "columns":      self.columns,
            "column_names": self.column_names,
            "is_primary":   self.is_primary,
            "is_empty":     self.is_empty,
        }


# ---------------------------------------------------------------------------
# Loaded spreadsheet — the standardized return shape
# ---------------------------------------------------------------------------


@dataclass
class LoadedSpreadsheet:
    """
    Standardized return for every loader.

    For a multi-sheet workbook, `sheets` holds every parsed sheet keyed by
    name and `active_sheet` is the primary (most-rows non-empty) sheet.
    For CSV/TSV, `sheets` has a single entry keyed by the file stem.
    """

    file_path:         str
    file_type:         str                                # "xlsx" | "xls" | "xlsm" | "csv" | "tsv"
    loader_name:       str                                # "ExcelLoader" | "CSVLoader"
    sheets:            dict[str, pd.DataFrame] = field(default_factory=dict)
    sheet_info:        list[SheetInfo]         = field(default_factory=list)
    active_sheet:      str                     = ""
    encoding:          str | None              = None    # csv/tsv only
    delimiter:         str | None              = None    # csv/tsv only
    workbook_metadata: dict                    = field(default_factory=dict)
    sampled:           bool                    = False   # True if a large-file sampling path was used
    sample_rows:       int                     = 0       # rows actually loaded if sampled
    total_rows_est:    int                     = 0       # estimated total rows on disk (csv/tsv)
    elapsed:           float                   = 0.0     # load duration in seconds
    warnings:          list[str]               = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def df(self) -> pd.DataFrame:
        """The DataFrame for the active sheet."""
        if not self.sheets:
            return pd.DataFrame()
        if self.active_sheet and self.active_sheet in self.sheets:
            return self.sheets[self.active_sheet]
        # Fallback: first sheet
        return next(iter(self.sheets.values()))

    def get_sheet(self, name: str) -> pd.DataFrame:
        if name not in self.sheets:
            raise KeyError(
                f"Sheet {name!r} not found. Available: {list(self.sheets.keys())}"
            )
        return self.sheets[name]

    def set_active_sheet(self, name: str) -> None:
        if name not in self.sheets:
            raise KeyError(
                f"Sheet {name!r} not found. Available: {list(self.sheets.keys())}"
            )
        self.active_sheet = name

    def preview_rows(self, n: int = 5, sheet: str | None = None) -> pd.DataFrame:
        target = self.get_sheet(sheet) if sheet else self.df
        return target.head(n)

    def is_multi_sheet(self) -> bool:
        return len(self.sheets) > 1

    def to_metadata_dict(self) -> dict:
        """Serializable metadata suitable for UI / session storage."""
        return {
            "file_path":         self.file_path,
            "file_name":         Path(self.file_path).name,
            "file_type":         self.file_type,
            "loader":            self.loader_name,
            "encoding":          self.encoding,
            "delimiter":         self.delimiter,
            "sheets":            [s.to_dict() for s in self.sheet_info],
            "active_sheet":      self.active_sheet,
            "is_multi_sheet":    self.is_multi_sheet(),
            "workbook_metadata": self.workbook_metadata,
            "sampled":           self.sampled,
            "sample_rows":       self.sample_rows,
            "total_rows_est":    self.total_rows_est,
            "elapsed":           round(self.elapsed, 3),
            "warnings":          list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Abstract base loader
# ---------------------------------------------------------------------------


class BaseLoader(ABC):
    """Abstract loader interface. Subclasses must implement `load()`."""

    #: Lowercase file extensions (including leading dot) this loader claims.
    EXTENSIONS: tuple[str, ...] = ()

    #: Human-readable name (used in logs and LoadedSpreadsheet.loader_name).
    NAME: str = "BaseLoader"

    def __init__(self, file_path: str | Path):
        self.file_path = Path(file_path)
        if not self.file_path.exists():
            raise LoaderError(f"File not found: {self.file_path}")
        if self.file_path.stat().st_size == 0:
            raise EmptyFileError(f"File is empty: {self.file_path}")

    # ------------------------------------------------------------------
    # Required
    # ------------------------------------------------------------------

    @abstractmethod
    def load(self, sheet: str | None = None, *, max_rows: int | None = None) -> LoadedSpreadsheet:
        """Parse the file and return a LoadedSpreadsheet."""

    # ------------------------------------------------------------------
    # Optional — concrete loaders may override
    # ------------------------------------------------------------------

    def extract_sheets(self) -> list[str]:
        """Return sheet names without fully parsing the file. Default: single anon sheet."""
        return [self.file_path.stem]

    def profile(self, loaded: LoadedSpreadsheet) -> dict:
        """Return a lightweight per-sheet profile (rows, cols, columns)."""
        return {info.name: info.to_dict() for info in loaded.sheet_info}

    def preview_rows(self, n: int = 5) -> pd.DataFrame:
        """Load and return the first N rows of the primary sheet (cheap path)."""
        return self.load(max_rows=n).df.head(n)

    # ------------------------------------------------------------------
    # Helpers for subclasses
    # ------------------------------------------------------------------

    @classmethod
    def claims(cls, file_path: str | Path) -> bool:
        return Path(file_path).suffix.lower() in cls.EXTENSIONS

    def __repr__(self) -> str:
        return f"<{self.NAME} {self.file_path.name}>"

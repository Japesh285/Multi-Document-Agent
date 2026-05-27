"""
loaders.loader_router — extension-based dispatch.

    .xlsx | .xlsm | .xls    → ExcelLoader
    .csv  | .tsv  | .txt    → CSVLoader

`load_any()` is the one-call entry point for the rest of the app.
"""

from __future__ import annotations

from pathlib import Path

from utils import get_logger

from .base_loader import (
    BaseLoader,
    LoadedSpreadsheet,
    LoaderError,
    UnsupportedFormatError,
)
from .csv_loader import CSVLoader
from .excel_loader import ExcelLoader

log = get_logger("loaders.router")


# Ordered list — first claimant wins
_LOADERS: tuple[type[BaseLoader], ...] = (ExcelLoader, CSVLoader)


SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    ext for loader in _LOADERS for ext in loader.EXTENSIONS
)


def is_supported(file_path: str | Path) -> bool:
    """True if any registered loader claims this extension."""
    return Path(file_path).suffix.lower() in SUPPORTED_EXTENSIONS


def get_loader(file_path: str | Path) -> BaseLoader:
    """
    Return a loader instance for the given path. Raises
    UnsupportedFormatError if no loader claims the extension.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()
    for loader_cls in _LOADERS:
        if suffix in loader_cls.EXTENSIONS:
            log.debug("router: %s → %s", path.name, loader_cls.NAME)
            return loader_cls(path)
    raise UnsupportedFormatError(
        f"No loader for '{suffix}'. Supported: {sorted(SUPPORTED_EXTENSIONS)}"
    )


def load_any(
    file_path: str | Path,
    sheet: str | None = None,
    *,
    max_rows: int | None = None,
) -> LoadedSpreadsheet:
    """
    One-shot: pick the right loader and return a LoadedSpreadsheet.

    Args:
        file_path: path to the spreadsheet
        sheet:     for multi-sheet workbooks, optionally pin a sheet to be active
        max_rows:  cap rows during read (used by previews); None = full file

    Raises:
        LoaderError or one of its subclasses on any failure
    """
    loader = get_loader(file_path)
    return loader.load(sheet=sheet, max_rows=max_rows)

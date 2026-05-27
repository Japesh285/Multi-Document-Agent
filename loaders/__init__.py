"""
loaders — universal spreadsheet ingestion layer.

Supported formats: .xlsx, .xls, .xlsm, .csv, .tsv

Public surface:
    load_any(path, sheet=None) -> LoadedSpreadsheet
    get_loader(path)           -> BaseLoader
    is_supported(path)         -> bool
    SUPPORTED_EXTENSIONS       -> frozenset[str]
    LoadedSpreadsheet, SheetInfo, BaseLoader
    LoaderError, UnsupportedFormatError, CorruptedFileError, EmptyFileError
"""

from .base_loader import (
    BaseLoader,
    CorruptedFileError,
    EmptyFileError,
    LoadedSpreadsheet,
    LoaderError,
    SheetInfo,
    UnsupportedFormatError,
)
from .loader_router import SUPPORTED_EXTENSIONS, get_loader, is_supported, load_any

__all__ = [
    "BaseLoader",
    "CorruptedFileError",
    "EmptyFileError",
    "LoadedSpreadsheet",
    "LoaderError",
    "SheetInfo",
    "SUPPORTED_EXTENSIONS",
    "UnsupportedFormatError",
    "get_loader",
    "is_supported",
    "load_any",
]

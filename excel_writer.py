"""
excel_writer.py — Safe dataframe mutation and spreadsheet write engine.

All write operations:
  1. Validate the change before applying
  2. Create a timestamped backup of the original file
  3. Apply to a df copy
  4. Save with the correct engine for the file's extension:
       .xlsx  → openpyxl (overwrite)
       .xlsm  → openpyxl with keep_vba=True (macros preserved)
       .xls   → re-saved as .xlsx alongside the original (legacy formats
                cannot be safely round-tripped from pandas)
       .csv   → text write, original delimiter preserved when known
       .tsv   → tab-delimited text write
  5. Return a change log

Caller is responsible for updating the global _df in app.py after a
successful write.
"""

from __future__ import annotations
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from utils import get_logger

log = get_logger("excel_writer")

_BACKUP_DIR = Path("output") / "backups"


# ---------------------------------------------------------------------------
# Change log entry
# ---------------------------------------------------------------------------

@dataclass
class ChangeEntry:
    action:        str            # "add_column" | "update_rows" | "set_value"
    column:        str
    rows_affected: int
    timestamp:     str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    detail:        str = ""
    success:       bool = True
    error:         str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_excel(file_path: str) -> str:
    """
    Copy the source spreadsheet to output/backups/ with a timestamp suffix.
    Works for any supported file type (extension is preserved).
    Returns the backup path, or "" on failure.
    """
    src = Path(file_path)
    if not src.exists():
        log.warning("excel_writer: backup skipped — source not found: %s", src)
        return ""

    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = _BACKUP_DIR / f"{src.stem}_{ts}{src.suffix}"

    try:
        shutil.copy2(src, dst)
        log.info("excel_writer: backup → %s", dst)
        return str(dst)
    except Exception as exc:
        log.error("excel_writer: backup failed — %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_new_column(df: pd.DataFrame, col_name: str, overwrite: bool) -> str | None:
    """Return error message, or None if valid."""
    if not col_name or not col_name.strip():
        return "Column name must not be empty."
    if col_name in df.columns and not overwrite:
        return f"Column '{col_name}' already exists. Use overwrite=True to replace."
    return None


def _validate_series(df: pd.DataFrame, values: pd.Series | list) -> str | None:
    n = len(values)
    if n != len(df):
        return f"Length mismatch: DataFrame has {len(df)} rows, values have {n}."
    return None


# ---------------------------------------------------------------------------
# Mutation operations
# ---------------------------------------------------------------------------

def add_column(
    df: pd.DataFrame,
    col_name: str,
    values: pd.Series | list,
    overwrite: bool = False,
) -> tuple[pd.DataFrame, ChangeEntry]:
    """
    Add or overwrite a column in df.
    Returns updated df copy + ChangeEntry.
    """
    err = _validate_new_column(df, col_name, overwrite) or _validate_series(df, values)
    if err:
        return df, ChangeEntry(action="add_column", column=col_name, rows_affected=0,
                               success=False, error=err)

    df_new = df.copy()
    df_new[col_name] = list(values) if not isinstance(values, pd.Series) else values.values
    change = ChangeEntry(
        action="add_column",
        column=col_name,
        rows_affected=len(df_new),
        detail=f"overwrite={overwrite}",
    )
    log.info("excel_writer: add_column '%s' (%d rows)", col_name, len(df_new))
    return df_new, change


def update_rows(
    df: pd.DataFrame,
    mask: pd.Series,
    col_name: str,
    value: Any,
    overwrite: bool = True,
) -> tuple[pd.DataFrame, ChangeEntry]:
    """
    Update specific rows (selected by boolean mask) in an existing column.
    """
    if col_name not in df.columns and not overwrite:
        err = f"Column '{col_name}' not found."
        return df, ChangeEntry(action="update_rows", column=col_name, rows_affected=0,
                               success=False, error=err)

    df_new        = df.copy()
    rows_affected = int(mask.sum())
    if col_name not in df_new.columns:
        df_new[col_name] = None
    df_new.loc[mask, col_name] = value
    change = ChangeEntry(
        action="update_rows",
        column=col_name,
        rows_affected=rows_affected,
        detail=f"value={value!r}",
    )
    log.info("excel_writer: update_rows '%s' → %d rows", col_name, rows_affected)
    return df_new, change


def apply_series_column(
    df: pd.DataFrame,
    col_name: str,
    series: pd.Series,
    overwrite: bool = True,
) -> tuple[pd.DataFrame, ChangeEntry]:
    """
    Apply a fully computed Series as a new/overwritten column.
    Wrapper around add_column for use after code execution.
    """
    return add_column(df, col_name, series, overwrite=overwrite)


# ---------------------------------------------------------------------------
# Save — format-aware
# ---------------------------------------------------------------------------

def save_excel(
    df: pd.DataFrame,
    file_path: str,
    *,
    backup: bool = True,
    sheet_name: str | None = None,
    delimiter: str | None = None,
    encoding: str = "utf-8",
) -> dict:
    """
    Save df back to its source file using the correct engine for the extension.

    Args:
        df:         the DataFrame to write
        file_path:  destination path; extension determines the writer
        backup:     timestamped copy of existing file before overwrite
        sheet_name: which sheet to write (.xlsx/.xlsm only). Defaults to the
                    first sheet for .xlsm round-trips, or "Sheet1" for new .xlsx.
        delimiter:  override for csv/tsv writes (defaults to ',' or '\\t')
        encoding:   text encoding for csv/tsv writes

    Returns {success, file, backup_path, rows, columns, file_type} or
            {success: False, error, ...}.
    """
    path        = Path(file_path)
    suffix      = path.suffix.lower()
    backup_path = ""

    if backup and path.exists():
        backup_path = backup_excel(file_path)

    try:
        if suffix in (".xlsx",):
            target_sheet = sheet_name or "Sheet1"
            df.to_excel(path, index=False, sheet_name=target_sheet, engine="openpyxl")

        elif suffix == ".xlsm":
            from loaders.excel_loader import write_xlsm_preserving_macros  # noqa: PLC0415
            write_xlsm_preserving_macros(df, path, sheet_name=sheet_name)

        elif suffix == ".xls":
            # pandas/openpyxl cannot write legacy .xls. Save as .xlsx beside it
            # so the user does not lose their data; the original .xls is the backup.
            new_path = path.with_suffix(".xlsx")
            df.to_excel(new_path, index=False, sheet_name=sheet_name or "Sheet1",
                        engine="openpyxl")
            log.warning(
                "excel_writer: .xls cannot be written directly — saved as %s",
                new_path.name,
            )
            return {
                "success":     True,
                "file":        str(new_path),
                "backup_path": backup_path,
                "rows":        len(df),
                "columns":     list(df.columns),
                "file_type":   "xlsx",
                "note":        "Legacy .xls files are saved as .xlsx (original kept as backup).",
            }

        elif suffix in (".csv", ".txt"):
            sep = delimiter or ","
            df.to_csv(path, index=False, sep=sep, encoding=encoding)

        elif suffix == ".tsv":
            df.to_csv(path, index=False, sep="\t", encoding=encoding)

        else:
            return {
                "success": False,
                "error": f"Unsupported extension '{suffix}' for write",
                "file":   str(path),
                "backup_path": backup_path,
            }

        log.info("excel_writer: saved %d rows × %d cols → %s (type=%s)",
                 len(df), len(df.columns), path, suffix.lstrip("."))
        return {
            "success":     True,
            "file":        str(path),
            "backup_path": backup_path,
            "rows":        len(df),
            "columns":     list(df.columns),
            "file_type":   suffix.lstrip("."),
        }

    except Exception as exc:
        log.error("excel_writer: save failed — %s", exc)
        return {
            "success": False,
            "error":   str(exc),
            "file":    str(path),
            "backup_path": backup_path,
        }


# ---------------------------------------------------------------------------
# High-level: LLM code → column write
# ---------------------------------------------------------------------------

def execute_column_mutation(
    code: str,
    df: pd.DataFrame,
    col_name: str,
    file_path: str,
    overwrite: bool = True,
    *,
    sheet_name: str | None = None,
    delimiter: str | None = None,
    encoding: str = "utf-8",
) -> tuple[pd.DataFrame, ChangeEntry, dict]:
    """
    Run generated pandas code (which should store a Series in `result`),
    add the result as `col_name`, and save.

    Returns (updated_df, change_entry, save_result).
    """
    from executor import safe_execute  # noqa: PLC0415

    output, error, elapsed = safe_execute(code, df)

    if error:
        entry = ChangeEntry(action="add_column", column=col_name, rows_affected=0,
                            success=False, error=f"Code execution failed: {error}")
        return df, entry, {"success": False, "error": error}

    # Re-execute to capture the raw Series object (safe_execute returns a string)
    raw_result = _exec_get_result(code, df)
    if raw_result is None or not isinstance(raw_result, pd.Series):
        entry = ChangeEntry(action="add_column", column=col_name, rows_affected=0,
                            success=False,
                            error=f"Code did not return a pd.Series — got {type(raw_result).__name__}")
        return df, entry, {"success": False, "error": entry.error}

    df_new, change = add_column(df, col_name, raw_result, overwrite=overwrite)
    if not change.success:
        return df, change, {"success": False, "error": change.error}

    save_result = save_excel(
        df_new,
        file_path,
        sheet_name=sheet_name,
        delimiter=delimiter,
        encoding=encoding,
    )
    return df_new, change, save_result


def _exec_get_result(code: str, df: pd.DataFrame) -> Any:
    """Re-run code in minimal sandbox and return the raw `result` object."""
    import io  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    from contextlib import redirect_stdout  # noqa: PLC0415

    sandbox    = {"pd": pd, "np": np, "df": df.copy(), "__builtins__": {}}
    local_vars: dict = {}
    try:
        with redirect_stdout(io.StringIO()):
            exec(code, sandbox, local_vars)  # noqa: S102
        return local_vars.get("result")
    except Exception:
        return None

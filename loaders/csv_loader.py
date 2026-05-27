"""
loaders.csv_loader — .csv / .tsv ingestion.

Handles real-world messiness:
  - encoding auto-detection (chardet → charset_normalizer → ordered fallback)
  - delimiter sniffing (extension hint + csv.Sniffer over a sample)
  - malformed rows (on_bad_lines="skip" with a warning)
  - very large files (lightweight sampling path when row count crosses threshold)
"""

from __future__ import annotations

import csv
import io
import time
from pathlib import Path

import pandas as pd

from utils import get_logger

from .base_loader import (
    BaseLoader,
    CorruptedFileError,
    EmptyFileError,
    LoadedSpreadsheet,
    SheetInfo,
)
from .normalize import normalize_dataframe

log = get_logger("loaders.csv")


# Common encodings tried in order if both detectors fail
_FALLBACK_ENCODINGS = ("utf-8", "utf-8-sig", "cp1252", "latin1")

# Threshold for switching to sampling. ~50 MB is enough for any spreadsheet that
# fits comfortably in memory; beyond that we sample and report total estimate.
_LARGE_FILE_BYTES = 50 * 1024 * 1024

# Read at most this many rows for the sampled path
_SAMPLE_ROWS = 20_000

# Sample byte size for encoding + delimiter sniffing
_SNIFF_BYTES = 64 * 1024


class CSVLoader(BaseLoader):
    """Loader for .csv / .tsv files."""

    EXTENSIONS = (".csv", ".tsv", ".txt")  # .txt accepted; sniffer decides delimiter
    NAME = "CSVLoader"

    # ------------------------------------------------------------------
    # Encoding detection
    # ------------------------------------------------------------------

    # Confidence below this is treated as a guess and overridden when the
    # sample is mostly ASCII. chardet is unreliable on short samples; a
    # cp1252 file can be flagged as Big5 / GB18030 if it has too few bytes.
    _LOW_CONFIDENCE = 0.85
    _ASCII_RATIO_FOR_UTF8 = 0.97

    @classmethod
    def _detect_encoding(cls, raw: bytes) -> tuple[str, float]:
        """
        Return (encoding, confidence). Tries chardet, then charset_normalizer.
        For low-confidence detections on predominantly-ASCII samples, prefers
        utf-8 — short ASCII samples produce notoriously bad chardet guesses.
        """
        if not raw:
            return "utf-8", 0.0

        # Pre-check: BOMs are authoritative
        if raw.startswith(b"\xef\xbb\xbf"):
            return "utf-8-sig", 1.0
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return "utf-16", 1.0

        ascii_ratio = sum(1 for b in raw if b < 128) / max(len(raw), 1)

        detected_enc: str | None = None
        detected_conf: float = 0.0

        try:
            import chardet  # noqa: PLC0415
            result = chardet.detect(raw) or {}
            detected_enc  = result.get("encoding")
            detected_conf = float(result.get("confidence") or 0.0)
        except ImportError:
            log.debug("csv: chardet not installed — trying charset_normalizer")
        except Exception as exc:
            log.debug("csv: chardet detection failed — %s", exc)

        if not detected_enc:
            try:
                import charset_normalizer  # noqa: PLC0415
                best = charset_normalizer.from_bytes(raw).best()
                if best:
                    detected_enc  = best.encoding
                    detected_conf = max(0.0, 1.0 - float(best.chaos or 0.0))
            except ImportError:
                log.debug("csv: charset_normalizer unavailable")
            except Exception as exc:
                log.debug("csv: charset_normalizer detection failed — %s", exc)

        # Low-confidence + mostly-ASCII → utf-8 is overwhelmingly more likely
        if detected_enc and detected_conf < cls._LOW_CONFIDENCE and ascii_ratio >= cls._ASCII_RATIO_FOR_UTF8:
            log.debug("csv: overriding low-conf %s (%.2f) with utf-8 (ascii_ratio=%.2f)",
                      detected_enc, detected_conf, ascii_ratio)
            return "utf-8", 0.5

        if detected_enc:
            return detected_enc, detected_conf

        return "utf-8", 0.0

    @classmethod
    def _read_with_fallback_encoding(
        cls, path: Path, primary: str, sample: bytes
    ) -> tuple[str, bytes]:
        """
        Confirm `primary` can decode the sample; otherwise walk the fallback list.
        Returns (encoding, decoded_sample_bytes-decoded-as-string is caller-side).
        """
        candidates = [primary] + [e for e in _FALLBACK_ENCODINGS if e.lower() != primary.lower()]
        last_err: Exception | None = None
        for enc in candidates:
            try:
                sample.decode(enc)
                return enc, sample
            except (UnicodeDecodeError, LookupError) as exc:
                last_err = exc
                continue
        # Everything failed; fall back to latin1 which never raises
        log.warning("csv: encoding fallback to latin1 for %s (last error: %s)", path.name, last_err)
        return "latin1", sample

    # ------------------------------------------------------------------
    # Delimiter sniffing
    # ------------------------------------------------------------------

    def _extension_delimiter_hint(self) -> str | None:
        suffix = self.file_path.suffix.lower()
        if suffix == ".tsv":
            return "\t"
        if suffix == ".csv":
            return ","
        return None

    @staticmethod
    def _sniff_delimiter(sample_text: str, hint: str | None) -> str:
        """Use csv.Sniffer with a tight candidate set. Falls back to hint or ','."""
        candidates = [",", "\t", ";", "|"]
        try:
            dialect = csv.Sniffer().sniff(sample_text, delimiters="".join(candidates))
            return dialect.delimiter
        except (csv.Error, Exception) as exc:
            log.debug("csv: sniffer failed — %s", exc)
            return hint or ","

    # ------------------------------------------------------------------
    # Required API
    # ------------------------------------------------------------------

    def extract_sheets(self) -> list[str]:
        return [self.file_path.stem]

    def _file_type(self) -> str:
        suffix = self.file_path.suffix.lower().lstrip(".")
        # Normalise .txt → csv (delimiter has been sniffed)
        if suffix == "txt":
            return "csv"
        return suffix

    def load(self, sheet: str | None = None, *, max_rows: int | None = None) -> LoadedSpreadsheet:
        t0   = time.perf_counter()
        path = self.file_path
        size = path.stat().st_size

        # --- 1. Encoding ---------------------------------------------------
        with path.open("rb") as fh:
            sample_bytes = fh.read(_SNIFF_BYTES)
        detected_enc, _conf = self._detect_encoding(sample_bytes)
        encoding, sample_bytes = self._read_with_fallback_encoding(path, detected_enc, sample_bytes)

        try:
            sample_text = sample_bytes.decode(encoding, errors="replace")
        except Exception as exc:
            raise CorruptedFileError(f"Could not decode {path.name}: {exc}") from exc

        # --- 2. Delimiter --------------------------------------------------
        hint      = self._extension_delimiter_hint()
        delimiter = self._sniff_delimiter(sample_text, hint)
        log.info("csv: %s  enc=%s  delim=%r  size=%dB", path.name, encoding, delimiter, size)

        # --- 3. Sampling decision ------------------------------------------
        warnings: list[str] = []
        sampled  = False
        nrows    = max_rows
        if max_rows is None and size >= _LARGE_FILE_BYTES:
            nrows = _SAMPLE_ROWS
            sampled = True
            warnings.append(
                f"Large file ({size/1024/1024:.1f} MB) — sampled first {_SAMPLE_ROWS:,} rows"
            )
            log.warning("csv: large file — sampling first %d rows", _SAMPLE_ROWS)

        # --- 4. Read with pandas, robust to bad lines ----------------------
        read_kwargs = dict(
            filepath_or_buffer=path,
            sep=delimiter,
            encoding=encoding,
            engine="python",        # tolerant of bad lines & mixed quoting
            on_bad_lines="skip",
            skip_blank_lines=True,
            dtype_backend="numpy_nullable",
            nrows=nrows,
        )
        try:
            df = pd.read_csv(**read_kwargs)
        except UnicodeDecodeError as exc:
            log.warning("csv: decode error with %s — retrying as latin1", encoding)
            encoding = "latin1"
            read_kwargs["encoding"] = encoding
            try:
                df = pd.read_csv(**read_kwargs)
            except Exception as inner:
                raise CorruptedFileError(
                    f"Could not parse {path.name} (encoding/delimiter detection failed): {inner}"
                ) from inner
        except Exception as exc:
            raise CorruptedFileError(
                f"Could not parse {path.name}: {exc}"
            ) from exc

        if df.empty:
            raise EmptyFileError(f"{path.name} parsed cleanly but contains no rows")

        # --- 5. Normalize --------------------------------------------------
        df, norm_warnings = normalize_dataframe(df)
        warnings.extend(norm_warnings)

        # --- 6. Estimate total rows for sampled files ----------------------
        total_est = 0
        if sampled:
            total_est = self._estimate_total_rows(path, encoding, sample_bytes)

        sheet_name = path.stem
        info = SheetInfo(
            name=sheet_name,
            rows=len(df),
            columns=len(df.columns),
            column_names=[str(c) for c in df.columns],
            is_primary=True,
        )

        elapsed = time.perf_counter() - t0
        log.info("csv: parsed %d rows × %d cols in %.2fs", len(df), len(df.columns), elapsed)

        return LoadedSpreadsheet(
            file_path=str(path),
            file_type=self._file_type(),
            loader_name=self.NAME,
            sheets={sheet_name: df},
            sheet_info=[info],
            active_sheet=sheet_name,
            encoding=encoding,
            delimiter=delimiter,
            workbook_metadata={
                "file_size_bytes": size,
                "newline_count_est": sample_bytes.count(b"\n"),
            },
            sampled=sampled,
            sample_rows=len(df) if sampled else 0,
            total_rows_est=total_est,
            elapsed=elapsed,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Row-count estimation for sampled large files
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_total_rows(path: Path, encoding: str, sample_bytes: bytes) -> int:
        """
        Cheap row-count estimate: bytes per line in the sample × file size / sample size.
        Off by a few rows for variable-length lines but enough for UI display.
        """
        try:
            line_count = max(sample_bytes.count(b"\n"), 1)
            bytes_per_line = len(sample_bytes) / line_count
            return max(int(path.stat().st_size / bytes_per_line) - 1, 0)
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# Write-back helpers (used by excel_writer)
# ---------------------------------------------------------------------------


def write_csv(df: pd.DataFrame, path: str | Path, delimiter: str = ",",
              encoding: str = "utf-8") -> None:
    """Plain text write. Uses UTF-8 with no BOM by default."""
    p = Path(path)
    df.to_csv(p, index=False, sep=delimiter, encoding=encoding)


def write_tsv(df: pd.DataFrame, path: str | Path, encoding: str = "utf-8") -> None:
    write_csv(df, path, delimiter="\t", encoding=encoding)

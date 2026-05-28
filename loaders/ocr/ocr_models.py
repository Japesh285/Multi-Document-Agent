"""
loaders.ocr.ocr_models — typed OCR result containers.

Attached to `DocumentObject.ocr` after a successful OCR run. Designed to be
small, JSON-serialisable, and friendly to LLM-generated code (e.g.
`for page in doc.ocr.pages: ...`).

Bounding boxes are (x, y, width, height) in image-pixel coordinates.
Confidence values are 0-100 (Tesseract's native scale; -1 → unknown,
filtered out before reaching these structures).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Word / line / block (in increasing aggregation)
# ---------------------------------------------------------------------------


@dataclass
class OcrWord:
    text:       str
    confidence: float                      # 0–100
    bbox:       tuple[int, int, int, int]  # (x, y, w, h)
    line_id:    int = -1
    block_id:   int = -1

    def to_dict(self) -> dict:
        return {
            "text":       self.text,
            "confidence": self.confidence,
            "bbox":       list(self.bbox),
            "line_id":    self.line_id,
            "block_id":   self.block_id,
        }


@dataclass
class OcrLine:
    text:       str
    confidence: float                      # mean of word confidences
    bbox:       tuple[int, int, int, int]
    block_id:   int
    word_count: int

    def to_dict(self) -> dict:
        return {
            "text":       self.text,
            "confidence": self.confidence,
            "bbox":       list(self.bbox),
            "block_id":   self.block_id,
            "word_count": self.word_count,
        }


@dataclass
class OcrBlock:
    text:       str
    confidence: float                      # mean of word confidences in block
    bbox:       tuple[int, int, int, int]
    line_count: int
    word_count: int

    def to_dict(self) -> dict:
        return {
            "text":       self.text,
            "confidence": self.confidence,
            "bbox":       list(self.bbox),
            "line_count": self.line_count,
            "word_count": self.word_count,
        }


# ---------------------------------------------------------------------------
# Table reconstructed from word bounding boxes
# ---------------------------------------------------------------------------


@dataclass
class OcrTable:
    """A semi-structured table recovered from a page's OCR layout."""

    page_index:   int
    rows:         list[list[str]] = field(default_factory=list)
    bbox:         tuple[int, int, int, int] = (0, 0, 0, 0)
    confidence:   float = 0.0           # mean cell-word confidence
    column_count: int   = 0
    row_count:    int   = 0

    def to_df(self):
        import pandas as pd  # noqa: PLC0415
        if not self.rows or not self.rows[0]:
            return pd.DataFrame()
        header = [str(c).strip() or f"col_{i+1}" for i, c in enumerate(self.rows[0])]
        body   = self.rows[1:]
        if not body:
            return pd.DataFrame(columns=header)
        # Make sure every body row has the right width
        norm = [r + [""] * (len(header) - len(r)) for r in body]
        norm = [r[:len(header)] for r in norm]
        return pd.DataFrame(norm, columns=header)

    def to_dict(self) -> dict:
        return {
            "page_index":   self.page_index,
            "rows":         [list(r) for r in self.rows],
            "bbox":         list(self.bbox),
            "confidence":   self.confidence,
            "column_count": self.column_count,
            "row_count":    self.row_count,
        }


# ---------------------------------------------------------------------------
# Page + top-level context
# ---------------------------------------------------------------------------


@dataclass
class OcrPage:
    """One page of an OCR'd document. Image is NOT stored — only metadata."""

    page_index:    int
    image_path:    str                                       # cached PNG location
    width:         int
    height:        int
    text:          str                                       # full plain-text joining lines
    blocks:        list[OcrBlock] = field(default_factory=list)
    lines:         list[OcrLine]  = field(default_factory=list)
    words:         list[OcrWord]  = field(default_factory=list)
    tables:        list[OcrTable] = field(default_factory=list)
    word_confidence_mean: float   = 0.0
    low_conf_word_count:  int     = 0                        # words with conf < threshold

    def to_dict(self) -> dict:
        return {
            "page_index":            self.page_index,
            "image_path":            self.image_path,
            "size":                  {"width": self.width, "height": self.height},
            "text_length":           len(self.text),
            "block_count":           len(self.blocks),
            "line_count":            len(self.lines),
            "word_count":            len(self.words),
            "table_count":           len(self.tables),
            "word_confidence_mean":  round(self.word_confidence_mean, 1),
            "low_conf_word_count":   self.low_conf_word_count,
        }

    def summary(self, *, max_chars: int = 120) -> str:
        snip = self.text.replace("\n", " ")[:max_chars].strip()
        return (
            f"page {self.page_index + 1} "
            f"[{self.width}×{self.height}, conf {self.word_confidence_mean:.0f}, "
            f"{len(self.tables)} tables] {snip!r}…"
        )


@dataclass
class OcrContext:
    """Top-level OCR result for one document. Attached to DocumentObject.ocr."""

    source_path:   str
    source_kind:   str                                       # "pdf_scanned" | "pdf_text" | "image"
    pages:         list[OcrPage]      = field(default_factory=list)
    language:      str                = "eng"
    engine:        str                = "tesseract"
    confidence_summary: dict          = field(default_factory=dict)
    timings:       dict               = field(default_factory=dict)
    warnings:      list[str]          = field(default_factory=list)

    # Convenience flat views — built lazily but cached after first access
    _all_blocks: list[OcrBlock] | None = None
    _all_lines:  list[OcrLine]  | None = None
    _all_words:  list[OcrWord]  | None = None
    _all_tables: list[OcrTable] | None = None

    @property
    def blocks(self) -> list[OcrBlock]:
        if self._all_blocks is None:
            self._all_blocks = [b for p in self.pages for b in p.blocks]
        return self._all_blocks

    @property
    def lines(self) -> list[OcrLine]:
        if self._all_lines is None:
            self._all_lines = [l for p in self.pages for l in p.lines]
        return self._all_lines

    @property
    def words(self) -> list[OcrWord]:
        if self._all_words is None:
            self._all_words = [w for p in self.pages for w in p.words]
        return self._all_words

    @property
    def tables(self) -> list[OcrTable]:
        if self._all_tables is None:
            self._all_tables = [t for p in self.pages for t in p.tables]
        return self._all_tables

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def to_dict(self) -> dict:
        return {
            "source_path":        self.source_path,
            "source_kind":        self.source_kind,
            "page_count":         self.page_count,
            "language":           self.language,
            "engine":             self.engine,
            "confidence_summary": dict(self.confidence_summary),
            "timings":            dict(self.timings),
            "pages":              [p.to_dict() for p in self.pages],
            "table_count":        len(self.tables),
            "warnings":           list(self.warnings),
        }

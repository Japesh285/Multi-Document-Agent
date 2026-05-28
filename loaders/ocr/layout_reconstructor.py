"""
loaders.ocr.layout_reconstructor — recover tabular structure from OCR words.

Approach (intentionally simple, "usable > perfect"):
  1. Cluster words into rows by Y-coordinate proximity.
  2. Inside each row, sort words by X-coordinate.
  3. Cluster columns by X-coordinate alignment across rows.
  4. Assemble a 2-D grid; skip rows that look like prose (single long span).

Output: a list of `OcrTable` objects per page.
"""

from __future__ import annotations

from typing import Sequence

from utils import get_logger

from .ocr_models import OcrTable, OcrWord

log = get_logger("ocr.layout")


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Two words on the same row if their y-centers are within this fraction
# of the smaller word's height. Loose enough to handle slight skew.
ROW_TOLERANCE_FRAC = 0.6

# X-coordinate cluster tolerance (in image pixels) for grouping rows into
# columns. Wider tolerance merges columns; tighter tolerance fragments.
COLUMN_TOLERANCE_PX = 24

# A "table candidate" needs at least this many rows AND ≥ 2 columns
MIN_TABLE_ROWS = 3
MIN_TABLE_COLS = 2

# Discard rows whose only token is longer than this — these are usually
# paragraph lines, not table cells.
MAX_PROSE_LINE_TOKENS = 1
MAX_PROSE_LINE_CHARS  = 60


# ---------------------------------------------------------------------------
# Row clustering
# ---------------------------------------------------------------------------


def _cluster_rows(words: Sequence[OcrWord]) -> list[list[OcrWord]]:
    """Group words into row clusters by Y-coordinate proximity."""
    if not words:
        return []
    sorted_words = sorted(words, key=lambda w: (w.bbox[1] + w.bbox[3] / 2))

    rows: list[list[OcrWord]] = []
    current: list[OcrWord]    = [sorted_words[0]]
    current_center = sorted_words[0].bbox[1] + sorted_words[0].bbox[3] / 2

    for w in sorted_words[1:]:
        h         = w.bbox[3]
        center    = w.bbox[1] + h / 2
        tolerance = max(4.0, h * ROW_TOLERANCE_FRAC)
        if abs(center - current_center) <= tolerance:
            current.append(w)
            # update running center
            centers       = [ww.bbox[1] + ww.bbox[3] / 2 for ww in current]
            current_center = sum(centers) / len(centers)
        else:
            rows.append(current)
            current = [w]
            current_center = center
    rows.append(current)

    # Sort each row left-to-right
    for r in rows:
        r.sort(key=lambda w: w.bbox[0])
    return rows


# ---------------------------------------------------------------------------
# Column clustering
# ---------------------------------------------------------------------------


def _detect_columns(rows: Sequence[Sequence[OcrWord]]) -> list[float]:
    """
    Pick column anchors (x-coordinates) by finding x-positions that appear
    in many rows. Returns the x-centers of detected columns, sorted left
    to right.
    """
    starts: list[float] = []
    for r in rows:
        for w in r:
            starts.append(float(w.bbox[0]))
    if not starts:
        return []

    starts.sort()
    clusters: list[list[float]] = [[starts[0]]]
    for x in starts[1:]:
        if abs(x - clusters[-1][-1]) <= COLUMN_TOLERANCE_PX:
            clusters[-1].append(x)
        else:
            clusters.append([x])

    # Keep clusters that appear on at least ~40% of rows (so a stray heading
    # word doesn't create a phantom column).
    min_support = max(2, int(0.4 * len(rows)))
    columns = [
        sum(c) / len(c)
        for c in clusters
        if len(c) >= min_support
    ]
    columns.sort()
    return columns


def _assign_to_columns(row: Sequence[OcrWord], columns: list[float]) -> list[str]:
    """Place each word into its nearest column. Words within a cell are joined with spaces."""
    if not columns:
        return [" ".join(w.text for w in row)]

    cells: list[list[str]] = [[] for _ in columns]
    for w in row:
        x = w.bbox[0]
        # Find nearest column anchor
        best_i = min(range(len(columns)), key=lambda i: abs(columns[i] - x))
        cells[best_i].append(w.text)
    return [" ".join(c).strip() for c in cells]


# ---------------------------------------------------------------------------
# Prose row filter
# ---------------------------------------------------------------------------


def _looks_like_prose(row_words: Sequence[OcrWord]) -> bool:
    """True if a row reads like a paragraph line, not a table row."""
    if len(row_words) <= MAX_PROSE_LINE_TOKENS:
        # Single long span → probably a sentence
        total = sum(len(w.text) for w in row_words)
        return total > MAX_PROSE_LINE_CHARS
    return False


def _bounding_box(words: Sequence[OcrWord]) -> tuple[int, int, int, int]:
    xs1 = [w.bbox[0] for w in words]
    ys1 = [w.bbox[1] for w in words]
    xs2 = [w.bbox[0] + w.bbox[2] for w in words]
    ys2 = [w.bbox[1] + w.bbox[3] for w in words]
    x0, y0 = min(xs1), min(ys1)
    x1, y1 = max(xs2), max(ys2)
    return (x0, y0, x1 - x0, y1 - y0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reconstruct_tables(words: Sequence[OcrWord], *, page_index: int) -> list[OcrTable]:
    """
    Find table candidates among `words` and return them as `OcrTable`s.

    A "table" is any contiguous run of ≥ MIN_TABLE_ROWS non-prose rows that
    share ≥ MIN_TABLE_COLS columns. Multiple tables on one page are detected
    by breaking at large vertical gaps or at runs of prose rows.
    """
    rows = _cluster_rows(words)
    if len(rows) < MIN_TABLE_ROWS:
        return []

    # Split into "blocks" of consecutive non-prose rows
    blocks: list[list[list[OcrWord]]] = []
    current: list[list[OcrWord]]      = []
    for r in rows:
        if _looks_like_prose(r):
            if len(current) >= MIN_TABLE_ROWS:
                blocks.append(current)
            current = []
        else:
            current.append(r)
    if len(current) >= MIN_TABLE_ROWS:
        blocks.append(current)

    tables: list[OcrTable] = []
    for block in blocks:
        columns = _detect_columns(block)
        if len(columns) < MIN_TABLE_COLS:
            continue
        grid = [_assign_to_columns(r, columns) for r in block]
        # Drop rows that ended up empty after column assignment
        grid = [r for r in grid if any(cell.strip() for cell in r)]
        if len(grid) < MIN_TABLE_ROWS:
            continue

        flat_words = [w for r in block for w in r]
        conf_mean  = (
            sum(w.confidence for w in flat_words) / len(flat_words)
            if flat_words else 0.0
        )
        tables.append(OcrTable(
            page_index=page_index,
            rows=grid,
            bbox=_bounding_box(flat_words),
            confidence=round(conf_mean, 1),
            column_count=len(columns),
            row_count=len(grid),
        ))
        log.debug("layout: page %d → table %d rows × %d cols (conf=%.1f)",
                  page_index, len(grid), len(columns), conf_mean)

    return tables

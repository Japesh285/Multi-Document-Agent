"""
loaders.ocr.ocr_loader — single entry point for image / PDF OCR.

Flow:
  - Image (.png/.jpg/.jpeg/.webp/.bmp/.tif/.tiff):
      preprocess → tesseract.image_to_data → assemble OcrPage
  - PDF (.pdf):
      detect text vs scanned via pymupdf
      if text PDF       → use embedded text (no OCR)
      if scanned PDF    → render every page at DPI → OCR each page

The output is always a `DocumentObject` whose `.ocr` field is an `OcrContext`.
Tables recovered by layout reconstruction are stashed on the doc as
`_pending_tables` so the workspace registrar can promote them to TableObjects.
"""

from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from utils import get_logger

from .confidence_manager import LOW_CONFIDENCE_THRESHOLD, summarize_context
from .image_preprocessor import load_image, preprocess
from .layout_reconstructor import reconstruct_tables
from .ocr_models import OcrBlock, OcrContext, OcrLine, OcrPage, OcrWord

if TYPE_CHECKING:
    from core.workspace_objects import DocumentObject

log = get_logger("ocr.loader")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OcrUnavailableError(RuntimeError):
    """The system Tesseract binary cannot be located."""


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
_PDF_EXTS   = (".pdf",)
SUPPORTED_OCR_EXTENSIONS: frozenset[str] = frozenset(_IMAGE_EXTS + _PDF_EXTS)


def is_ocr_supported(file_path: str | Path) -> bool:
    return Path(file_path).suffix.lower() in SUPPORTED_OCR_EXTENSIONS


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


_CACHE_DIR = Path("output") / "ocr_cache"


def _cache_subdir_for(file_path: Path) -> Path:
    """Stable per-file cache directory keyed by absolute path + mtime."""
    key = f"{file_path.resolve()}|{file_path.stat().st_mtime_ns}"
    h   = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    out = _CACHE_DIR / f"{file_path.stem}_{h}"
    out.mkdir(parents=True, exist_ok=True)
    return out


# ---------------------------------------------------------------------------
# Tesseract availability + invocation
# ---------------------------------------------------------------------------


def _check_tesseract():
    """Return the pytesseract module if Tesseract is reachable; else raise."""
    try:
        import pytesseract  # noqa: PLC0415
    except ImportError as exc:
        raise OcrUnavailableError(
            "pytesseract is not installed. Run: pip install pytesseract"
        ) from exc
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:
        raise OcrUnavailableError(
            "Tesseract binary not found. Install Tesseract OCR for Windows "
            "(UB-Mannheim build at https://github.com/UB-Mannheim/tesseract/wiki) "
            "and ensure tesseract.exe is on PATH, or set pytesseract.tesseract_cmd."
        ) from exc
    return pytesseract


def _image_to_ocr_data(image, language: str, config: str) -> dict:
    """Call Tesseract via image_to_data and return the raw dict."""
    pytesseract = _check_tesseract()
    from PIL import Image  # noqa: PLC0415

    if hasattr(image, "ndim"):
        # numpy ndarray (post-preprocess)
        pil = Image.fromarray(image)
    else:
        pil = image

    return pytesseract.image_to_data(
        pil,
        lang=language,
        config=config,
        output_type=pytesseract.Output.DICT,
    )


# ---------------------------------------------------------------------------
# Result assembly: tesseract dict → OcrPage
# ---------------------------------------------------------------------------


def _assemble_page(
    data:        dict,
    page_index:  int,
    image_path:  str,
    width:       int,
    height:      int,
    *,
    low_threshold: float,
) -> OcrPage:
    """
    Turn the dict returned by pytesseract.image_to_data into a typed OcrPage.

    Tesseract emits one row per word, plus aggregation rows (block/par/line)
    with empty `text`. We keep word rows for the granular OcrWord list and
    aggregate manually for blocks and lines so the structure is consistent
    even when Tesseract returns partial data.
    """
    n = len(data.get("text", []))
    if n == 0:
        return OcrPage(page_index=page_index, image_path=image_path,
                       width=width, height=height, text="",
                       word_confidence_mean=0.0, low_conf_word_count=0)

    words: list[OcrWord] = []
    # Aggregate per (block, line)
    line_groups:  dict[tuple[int, int], list[int]] = {}
    block_groups: dict[int, list[int]]              = {}

    for i in range(n):
        txt   = (data["text"][i] or "").strip()
        conf  = float(data["conf"][i])
        block = int(data.get("block_num", [0]*n)[i])
        line  = int(data.get("line_num",  [0]*n)[i])
        if not txt or conf < 0:
            continue
        x, y = int(data["left"][i]), int(data["top"][i])
        w, h = int(data["width"][i]), int(data["height"][i])
        words.append(OcrWord(
            text=txt, confidence=conf, bbox=(x, y, w, h),
            line_id=line, block_id=block,
        ))
        line_groups.setdefault((block, line), []).append(len(words) - 1)
        block_groups.setdefault(block, []).append(len(words) - 1)

    # Build lines
    lines: list[OcrLine] = []
    for (block, line), indices in sorted(line_groups.items()):
        ws = [words[i] for i in indices]
        line_text = " ".join(w.text for w in ws)
        confs     = [w.confidence for w in ws]
        x0 = min(w.bbox[0] for w in ws)
        y0 = min(w.bbox[1] for w in ws)
        x1 = max(w.bbox[0] + w.bbox[2] for w in ws)
        y1 = max(w.bbox[1] + w.bbox[3] for w in ws)
        lines.append(OcrLine(
            text=line_text,
            confidence=sum(confs) / len(confs),
            bbox=(x0, y0, x1 - x0, y1 - y0),
            block_id=block,
            word_count=len(ws),
        ))

    # Build blocks
    blocks: list[OcrBlock] = []
    for block, indices in sorted(block_groups.items()):
        ws = [words[i] for i in indices]
        block_text = " ".join(w.text for w in ws)
        confs      = [w.confidence for w in ws]
        x0 = min(w.bbox[0] for w in ws)
        y0 = min(w.bbox[1] for w in ws)
        x1 = max(w.bbox[0] + w.bbox[2] for w in ws)
        y1 = max(w.bbox[1] + w.bbox[3] for w in ws)
        line_count = len({(w.block_id, w.line_id) for w in ws})
        blocks.append(OcrBlock(
            text=block_text,
            confidence=sum(confs) / len(confs),
            bbox=(x0, y0, x1 - x0, y1 - y0),
            line_count=line_count,
            word_count=len(ws),
        ))

    # Flat text — preserve line order
    page_text = "\n".join(l.text for l in lines)

    word_conf_mean = (
        sum(w.confidence for w in words) / len(words) if words else 0.0
    )
    low_conf = sum(1 for w in words if w.confidence < low_threshold)

    tables = reconstruct_tables(words, page_index=page_index)

    return OcrPage(
        page_index=page_index,
        image_path=image_path,
        width=width,
        height=height,
        text=page_text,
        blocks=blocks,
        lines=lines,
        words=words,
        tables=tables,
        word_confidence_mean=word_conf_mean,
        low_conf_word_count=low_conf,
    )


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------


def _pdf_is_scanned(pdf_path: Path, *, min_chars_per_page: int = 50) -> tuple[bool, list[str]]:
    """
    Decide whether a PDF is scanned (needs OCR) or text-based.
    Returns (is_scanned, per_page_extracted_text).
    """
    try:
        import fitz  # noqa: PLC0415  (pymupdf)
    except ImportError as exc:
        raise OcrUnavailableError(
            "pymupdf is not installed. Run: pip install pymupdf"
        ) from exc

    doc = fitz.open(str(pdf_path))
    try:
        per_page_text: list[str] = []
        for page in doc:
            per_page_text.append(page.get_text("text") or "")
    finally:
        doc.close()

    total_chars = sum(len(t) for t in per_page_text)
    avg_chars   = total_chars / max(len(per_page_text), 1)
    scanned     = avg_chars < min_chars_per_page
    log.info("ocr: PDF type-detect %s — %d pages, avg %.0f chars/page → %s",
             pdf_path.name, len(per_page_text), avg_chars,
             "SCANNED" if scanned else "TEXT")
    return scanned, per_page_text


def _render_pdf_pages(pdf_path: Path, cache_dir: Path, *, dpi: int) -> list[Path]:
    """Render each PDF page to a PNG at the requested DPI, cached on disk."""
    import fitz  # noqa: PLC0415

    out_paths: list[Path] = []
    doc = fitz.open(str(pdf_path))
    try:
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        for i, page in enumerate(doc):
            out = cache_dir / f"page_{i+1:04d}.png"
            if out.exists():
                out_paths.append(out)
                continue
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            pix.save(str(out))
            out_paths.append(out)
        log.info("ocr: rendered %d pages from %s at %d DPI",
                 len(out_paths), pdf_path.name, dpi)
    finally:
        doc.close()
    return out_paths


# ---------------------------------------------------------------------------
# Image flow
# ---------------------------------------------------------------------------


def _ocr_image_file(
    image_path: Path,
    page_index: int,
    cache_dir:  Path,
    *,
    language:    str,
    config:      str,
    threshold:   float,
    preprocess_kwargs: dict,
) -> OcrPage:
    cached_preproc = cache_dir / f"preproc_{image_path.stem}.png"
    img = load_image(image_path)
    height, width = img.shape[:2]
    out = preprocess(img, **preprocess_kwargs)
    # Save preprocessed copy for debugging / UI
    import cv2  # noqa: PLC0415
    cv2.imwrite(str(cached_preproc), out)
    data = _image_to_ocr_data(out, language=language, config=config)
    return _assemble_page(
        data, page_index=page_index,
        image_path=str(cached_preproc),
        width=width, height=height,
        low_threshold=threshold,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_ocr(
    file_path:   str | Path,
    *,
    name:        str | None = None,
    language:    str        = "eng",
    dpi:         int        = 220,
    psm:         int        = 6,                    # uniform block of text — most receipts/reports
    oem:         int        = 3,                    # default LSTM engine
    threshold:   float      = LOW_CONFIDENCE_THRESHOLD,
    preprocess_kwargs: dict | None = None,
    text_pdf_fallback: bool = True,
) -> "DocumentObject":
    """
    OCR an image or PDF and return a `core.DocumentObject` with `.ocr`
    populated and `_pending_tables` set for the workspace to register.

    Args:
        file_path:  .pdf / .png / .jpg / .jpeg / .webp / .bmp / .tif / .tiff
        language:   Tesseract language code (use "+"-joined for multi e.g. "eng+fra")
        dpi:        Page render DPI for PDFs (220 is a good speed/accuracy sweet spot)
        psm/oem:    Tesseract page-segmentation mode and engine mode
        threshold:  word-confidence cutoff for the "low conf" bucket
        preprocess_kwargs: passed through to image_preprocessor.preprocess()
        text_pdf_fallback: if False, force OCR even when a PDF has selectable text

    Raises:
        OcrUnavailableError if Tesseract / pymupdf cannot be loaded
        ValueError on unsupported extension or empty file
    """
    from core.workspace_objects import DocumentObject  # noqa: PLC0415

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"OCR source not found: {path}")
    if path.stat().st_size == 0:
        raise ValueError(f"OCR source is empty: {path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_OCR_EXTENSIONS:
        raise ValueError(
            f"Unsupported OCR extension '{suffix}'. "
            f"Supported: {sorted(SUPPORTED_OCR_EXTENSIONS)}"
        )

    preprocess_kwargs = preprocess_kwargs or {}
    config            = f"--oem {oem} --psm {psm}"
    cache_dir         = _cache_subdir_for(path)
    t_total           = time.perf_counter()
    timings: dict     = {}
    warnings: list[str] = []

    pages: list[OcrPage] = []
    source_kind: str

    # ---- Image flow -----------------------------------------------------
    if suffix in _IMAGE_EXTS:
        source_kind = "image"
        t0 = time.perf_counter()
        page = _ocr_image_file(
            path, page_index=0, cache_dir=cache_dir,
            language=language, config=config,
            threshold=threshold,
            preprocess_kwargs=preprocess_kwargs,
        )
        timings["ocr_seconds"] = round(time.perf_counter() - t0, 2)
        pages = [page]

    # ---- PDF flow -------------------------------------------------------
    else:
        # Detect text vs scanned
        scanned, per_page_text = _pdf_is_scanned(path)
        if not scanned and text_pdf_fallback:
            source_kind = "pdf_text"
            warnings.append("PDF appears to have selectable text — used native extraction instead of OCR.")
            for i, text in enumerate(per_page_text):
                pages.append(OcrPage(
                    page_index=i, image_path="",
                    width=0, height=0,
                    text=text or "",
                    word_confidence_mean=100.0,  # native text is "perfect"
                    low_conf_word_count=0,
                ))
            timings["render_seconds"] = 0.0
            timings["ocr_seconds"]    = 0.0
        else:
            source_kind = "pdf_scanned"
            t0          = time.perf_counter()
            page_imgs   = _render_pdf_pages(path, cache_dir, dpi=dpi)
            timings["render_seconds"] = round(time.perf_counter() - t0, 2)

            t0 = time.perf_counter()
            for i, img_path in enumerate(page_imgs):
                page = _ocr_image_file(
                    img_path, page_index=i, cache_dir=cache_dir,
                    language=language, config=config,
                    threshold=threshold,
                    preprocess_kwargs=preprocess_kwargs,
                )
                pages.append(page)
            timings["ocr_seconds"] = round(time.perf_counter() - t0, 2)

    timings["total_seconds"] = round(time.perf_counter() - t_total, 2)

    ctx = OcrContext(
        source_path=str(path),
        source_kind=source_kind,
        pages=pages,
        language=language,
        engine="tesseract" if source_kind != "pdf_text" else "pymupdf-text",
        timings=timings,
        warnings=warnings,
    )
    ctx.confidence_summary = summarize_context(ctx, threshold=threshold)

    # Wrap as DocumentObject
    paragraphs = [p.text for p in pages]
    word_count = sum(len(p.text.split()) for p in pages)
    doc_obj = DocumentObject(
        name=name or path.stem,
        doc=None,                                  # python-docx Document not applicable
        source_path=str(path),
        paragraphs=paragraphs,
        headings=[],
        sections=[
            {"name": f"page_{i+1}", "level": 1,
             "paragraph_start": i, "paragraph_end": i + 1}
            for i in range(len(pages))
        ],
        word_count=word_count,
        metadata={
            "ocr":           ctx.to_dict(),
            "source_kind":   source_kind,
            "page_count":    len(pages),
            "language":      language,
            "ocr_confidence": ctx.confidence_summary.get("word_confidence_mean", 0.0),
        },
    )
    doc_obj.ocr = ctx  # type: ignore[attr-defined]

    # Stash recovered tables for the workspace to register
    pending: list[tuple] = []
    for page in pages:
        for tbl in page.tables:
            df = tbl.to_df()
            if df.empty:
                continue
            heading = f"page {page.page_index + 1}"
            pending.append((df, heading))
    doc_obj._pending_tables = pending  # type: ignore[attr-defined]

    log.info(
        "ocr: %s done — %d page(s), %d table(s), conf %.1f, %.2fs total",
        path.name, len(pages), len(pending),
        ctx.confidence_summary.get("word_confidence_mean", 0.0),
        timings["total_seconds"],
    )
    return doc_obj


def clear_cache() -> int:
    """Wipe rendered-page + preprocessed cache. Returns bytes freed (approx)."""
    if not _CACHE_DIR.exists():
        return 0
    freed = 0
    for p in _CACHE_DIR.rglob("*"):
        if p.is_file():
            freed += p.stat().st_size
    shutil.rmtree(_CACHE_DIR, ignore_errors=True)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log.info("ocr: cache cleared (%.1f MB freed)", freed / 1024 / 1024)
    return freed

"""
loaders.ocr — local OCR pipeline (Tesseract + OpenCV + PyMuPDF).

Public surface:
    load_ocr(path, *, name=None, ...) -> DocumentObject
        — single entry point. Dispatches on extension to image or PDF flow.

    OcrContext, OcrPage, OcrBlock, OcrLine, OcrWord, OcrTable
        — typed containers attached to DocumentObject.ocr.

    OcrUnavailableError
        — raised when the system Tesseract binary cannot be found.

    SUPPORTED_OCR_EXTENSIONS
        — frozenset of extensions this loader claims.
"""

from .ocr_loader import (
    OcrUnavailableError,
    SUPPORTED_OCR_EXTENSIONS,
    is_ocr_supported,
    load_ocr,
)
from .ocr_models import (
    OcrBlock,
    OcrContext,
    OcrLine,
    OcrPage,
    OcrTable,
    OcrWord,
)

__all__ = [
    "OcrBlock",
    "OcrContext",
    "OcrLine",
    "OcrPage",
    "OcrTable",
    "OcrUnavailableError",
    "OcrWord",
    "SUPPORTED_OCR_EXTENSIONS",
    "is_ocr_supported",
    "load_ocr",
]

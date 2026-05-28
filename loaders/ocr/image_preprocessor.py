"""
loaders.ocr.image_preprocessor — image cleanup before OCR.

A conservative pipeline that improves Tesseract accuracy on common inputs
(scanned receipts, photographed forms, screenshots) without over-aggressively
modifying clean images. Each step is opt-in via keyword arg.

Public:
    preprocess(image, *, grayscale, denoise, threshold, contrast,
               upscale_to_min_width, sharpen) -> np.ndarray
    load_image(path)   -> np.ndarray  (BGR, OpenCV convention)
    save_image(image, path)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from utils import get_logger

log = get_logger("ocr.preprocessor")


# Tesseract performs best when characters are at least ~25px tall, which
# corresponds to roughly 1500-1800px page width for typical documents.
_DEFAULT_MIN_WIDTH = 1600


def load_image(path: str | Path):
    """Load an image as a BGR numpy array (OpenCV's native format)."""
    import cv2  # noqa: PLC0415
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"OpenCV could not read image: {path}")
    return img


def save_image(image, path: str | Path) -> None:
    import cv2  # noqa: PLC0415
    if not cv2.imwrite(str(path), image):
        raise IOError(f"OpenCV could not write image: {path}")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def preprocess(
    image,
    *,
    grayscale:            bool = True,
    denoise:              bool = True,
    threshold:            bool = True,
    contrast:             bool = True,
    upscale_to_min_width: int  = _DEFAULT_MIN_WIDTH,
    sharpen:              bool = False,
):
    """
    Apply the OCR preprocessing pipeline. Returns a numpy array suitable
    for direct hand-off to `pytesseract.image_to_data(Image.fromarray(...))`.

    Order matters: upscale → grayscale → denoise → contrast → threshold → sharpen.
    """
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    if image is None or getattr(image, "size", 0) == 0:
        raise ValueError("preprocess: received empty image")

    out = image

    # 1. Upscale undersized scans (lanczos preserves character strokes)
    if upscale_to_min_width and out.shape[1] < upscale_to_min_width:
        scale = upscale_to_min_width / out.shape[1]
        new_w = upscale_to_min_width
        new_h = int(out.shape[0] * scale)
        out = cv2.resize(out, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
        log.debug("preprocess: upscaled to %dx%d (scale=%.2f)", new_w, new_h, scale)

    # 2. Grayscale
    if grayscale and len(out.shape) == 3:
        out = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)

    # 3. Denoise — fastNlMeansDenoising preserves edges better than blur
    if denoise:
        if len(out.shape) == 2:
            out = cv2.fastNlMeansDenoising(out, h=10, templateWindowSize=7, searchWindowSize=21)
        else:
            out = cv2.fastNlMeansDenoisingColored(out, None, 10, 10, 7, 21)

    # 4. Contrast — CLAHE works better than global histogram equalisation
    if contrast and len(out.shape) == 2:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        out   = clahe.apply(out)

    # 5. Adaptive threshold (binary) — only meaningful on grayscale
    if threshold and len(out.shape) == 2:
        out = cv2.adaptiveThreshold(
            out, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            blockSize=31, C=10,
        )

    # 6. Sharpen — opt-in; can amplify scan artifacts
    if sharpen:
        kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
        out    = cv2.filter2D(out, -1, kernel)

    return out


def preprocess_path(input_path: str | Path, output_path: str | Path, **kwargs) -> Path:
    """Convenience: read → preprocess → write, return output path."""
    img = load_image(input_path)
    out = preprocess(img, **kwargs)
    save_image(out, output_path)
    return Path(output_path)

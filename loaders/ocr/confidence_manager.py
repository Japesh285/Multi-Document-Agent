"""
loaders.ocr.confidence_manager — confidence statistics over OCR output.

Operates on the typed structures from `ocr_models`. Produces a structured
summary fit for inclusion in workspace metadata and context prompts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ocr_models import OcrContext, OcrPage


# Words with confidence below this are treated as "suspicious"
LOW_CONFIDENCE_THRESHOLD = 60.0

# Maximum reasonable word length — anything longer is likely OCR noise
MAX_REASONABLE_WORD_LEN = 40


def _suspicious_words(words) -> int:
    """Count words that look like OCR noise even if conf is OK."""
    n = 0
    for w in words:
        t = w.text
        # Empty / only-punctuation
        if not t or not any(c.isalnum() for c in t):
            n += 1
            continue
        # Absurdly long token (no spaces)
        if len(t) > MAX_REASONABLE_WORD_LEN:
            n += 1
            continue
    return n


def summarize_page(page: "OcrPage", *, threshold: float = LOW_CONFIDENCE_THRESHOLD) -> dict:
    words = page.words
    if not words:
        # Two cases where we have no per-word records:
        #   - genuinely empty page (text empty)
        #   - text-PDF path: text is present and trusted but no OCR ran
        if page.text.strip():
            return {
                "page_index":           page.page_index,
                "word_count":           len(page.text.split()),
                "word_confidence_mean": page.word_confidence_mean or 100.0,
                "low_conf_word_count":  0,
                "low_conf_ratio":       0.0,
                "suspicious_count":     0,
                "table_count":          len(page.tables),
                "quality":              "high",          # native text is trusted
            }
        return {
            "page_index":           page.page_index,
            "word_count":           0,
            "word_confidence_mean": 0.0,
            "low_conf_word_count":  0,
            "low_conf_ratio":       0.0,
            "suspicious_count":     0,
            "table_count":          len(page.tables),
            "quality":              "no_text",
        }

    confs    = [w.confidence for w in words]
    mean     = sum(confs) / len(confs)
    low      = sum(1 for c in confs if c < threshold)
    low_r    = low / len(words)
    suspect  = _suspicious_words(words)

    if mean >= 85 and low_r <= 0.05:
        quality = "high"
    elif mean >= 70 and low_r <= 0.15:
        quality = "good"
    elif mean >= 55:
        quality = "fair"
    else:
        quality = "poor"

    return {
        "page_index":           page.page_index,
        "word_count":           len(words),
        "word_confidence_mean": round(mean, 1),
        "low_conf_word_count":  low,
        "low_conf_ratio":       round(low_r, 3),
        "suspicious_count":     suspect,
        "table_count":          len(page.tables),
        "quality":              quality,
    }


def summarize_context(ctx: "OcrContext",
                       *, threshold: float = LOW_CONFIDENCE_THRESHOLD) -> dict:
    """
    Aggregate page summaries into a single document-level confidence report.

    Returns a dict that goes into `OcrContext.confidence_summary` and is
    surfaced via the context compiler for prompts.
    """
    if not ctx.pages:
        return {"page_count": 0, "quality": "no_pages",
                "word_confidence_mean": 0.0, "pages": []}

    per_page = [summarize_page(p, threshold=threshold) for p in ctx.pages]
    total_words = sum(p["word_count"] for p in per_page)
    if total_words == 0:
        return {"page_count": len(ctx.pages), "quality": "no_text",
                "word_confidence_mean": 0.0, "pages": per_page}

    # Weighted mean by word count
    weighted_conf = sum(p["word_confidence_mean"] * p["word_count"] for p in per_page)
    overall_mean  = weighted_conf / total_words
    low_total     = sum(p["low_conf_word_count"] for p in per_page)
    low_ratio     = low_total / total_words

    if overall_mean >= 85 and low_ratio <= 0.05:
        overall_quality = "high"
    elif overall_mean >= 70 and low_ratio <= 0.15:
        overall_quality = "good"
    elif overall_mean >= 55:
        overall_quality = "fair"
    else:
        overall_quality = "poor"

    return {
        "page_count":           len(ctx.pages),
        "word_count":           total_words,
        "word_confidence_mean": round(overall_mean, 1),
        "low_conf_word_count":  low_total,
        "low_conf_ratio":       round(low_ratio, 3),
        "quality":              overall_quality,
        "pages":                per_page,
    }

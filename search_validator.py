"""
search_validator.py — Filter and validate web search results before date extraction.

Prevents biographical dates (birthdays, death dates), historical records,
and unrelated results from polluting fixture/event verification.

Validation checks (in order):
  1. Reject biographical content (born / birthday markers)
  2. Entity name must appear in result text
  3. Found date must be within max_days of expected date
  4. Optional: event language present (match / game / vs / fixture)
"""

from __future__ import annotations
from datetime import datetime

from utils import get_logger

log = get_logger("search_validator")

# Words that signal a biographical / non-event result
_BIRTH_MARKERS = frozenset({
    "born", "birth", "birthday", "birthdate", "date of birth",
    "natal", "was born", "born on", "born in",
})

# Words that signal an actual sports/scheduling event
_EVENT_MARKERS = frozenset({
    "match", "game", "vs", "versus", "fixture", "schedule", "scheduled",
    "kick-off", "kickoff", "tip-off", "tipoff", "face", "host", "play",
    "takes on", "will play", "kick off",
})

_DEFAULT_MAX_DAYS = 180   # 6-month window around expected date


def _entity_match_score(entity_name: str, text: str) -> float:
    """Fraction of entity name words (len > 2) found in text (0.0–1.0)."""
    words = [w for w in entity_name.lower().split() if len(w) > 2]
    if not words:
        return 0.0
    tl = text.lower()
    return sum(1 for w in words if w in tl) / len(words)


def _extract_found_date(result: dict) -> str | None:
    """Parse a date from title + body of a result dict."""
    from verification import parse_date_from_text  # lazy import avoids circular
    text = f"{result.get('title', '')} {result.get('body', '')}"
    return parse_date_from_text(text)


def _days_apart(d1: str, d2: str) -> int | None:
    try:
        return abs((datetime.fromisoformat(d1) - datetime.fromisoformat(d2)).days)
    except Exception:
        return None


def validate_result(
    result:        dict,
    entity_name:   str,
    expected_date: str,
    max_days:      int = _DEFAULT_MAX_DAYS,
) -> tuple[bool, float, str]:
    """
    Validate one search result for use in date/fixture verification.

    Args:
        result:        dict with 'title', 'body', optionally 'href'
        entity_name:   clean team / player name we searched for
        expected_date: ISO date from the spreadsheet (may be "")
        max_days:      maximum allowable days between expected and found date

    Returns:
        (is_valid, confidence ∈ [0, 1], reason_string)
    """
    title    = result.get("title") or ""
    body     = result.get("body")  or ""
    combined = f"{title} {body}"
    combined_lower = combined.lower()

    # ── Check 1: reject biographical content ─────────────────────────────────
    if any(marker in combined_lower for marker in _BIRTH_MARKERS):
        log.debug("validator: bio result rejected — '%s'", title[:70])
        return False, 0.0, "biographical content detected"

    # ── Check 2: entity name must appear ─────────────────────────────────────
    entity_score = _entity_match_score(entity_name, combined)
    if entity_score < 0.25:
        return False, 0.0, f"entity '{entity_name[:30]}' not in result"

    # ── Check 3: date range ───────────────────────────────────────────────────
    found_date = _extract_found_date(result)
    if not found_date:
        confidence = round(entity_score * 0.25, 3)
        return False, confidence, "no date found in result"

    if expected_date:
        diff = _days_apart(expected_date, found_date)
        if diff is not None and diff > max_days:
            log.debug(
                "validator: date out of range — entity=%s expected=%s found=%s diff=%d",
                entity_name[:30], expected_date, found_date, diff,
            )
            return False, 0.0, f"date {diff} days from expected (limit {max_days})"

    # ── Confidence score ──────────────────────────────────────────────────────
    event_bonus = 0.10 if any(m in combined_lower for m in _EVENT_MARKERS) else 0.0
    confidence  = min(0.95, entity_score * 0.65 + 0.25 + event_bonus)

    return True, round(confidence, 3), "valid"


def filter_relevant_results(
    results:       list[dict],
    entity_name:   str,
    expected_date: str,
    max_days:      int = _DEFAULT_MAX_DAYS,
) -> list[dict]:
    """
    Filter a list of search results, keeping only validated ones.
    Attaches '_confidence' and '_validation' keys to passing results.
    Returns filtered list preserving original order.
    """
    out: list[dict] = []
    for r in results:
        valid, conf, reason = validate_result(r, entity_name, expected_date, max_days)
        if valid:
            r = {**r, "_confidence": conf, "_validation": reason}
            out.append(r)
        else:
            log.debug(
                "validator: dropped — %s — '%s'",
                reason, (r.get("title") or "")[:60],
            )
    log.debug(
        "validator: %d/%d results kept for entity=%r",
        len(out), len(results), entity_name[:30],
    )
    return out

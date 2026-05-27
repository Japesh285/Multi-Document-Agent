"""
verification.py — Fixture date verification engine.

Compares spreadsheet Game Date values against dates found in live web search results.
Returns structured VerificationResult objects with per-entity confidence scores.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from datetime import datetime

from search_validator import filter_relevant_results
from utils import get_logger

log = get_logger("verification")

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3,  "apr": 4,  "may": 5,  "jun": 6,
    "jul": 7, "aug": 8, "sep": 9,  "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    entity:            str
    spreadsheet_date:  str
    web_date:          str | None
    match:             bool
    confidence:        float         # 0.0–1.0
    source:            str
    detail:            str = ""

    def to_dict(self) -> dict:
        return {
            "entity":           self.entity,
            "spreadsheet_date": self.spreadsheet_date,
            "web_date":         self.web_date,
            "match":            self.match,
            "confidence":       round(self.confidence, 3),
            "source":           self.source,
            "detail":           self.detail,
        }


@dataclass
class VerificationSummary:
    total:       int
    matched:     int
    mismatched:  int
    uncertain:   int
    results:     list[VerificationResult] = field(default_factory=list)

    @property
    def match_rate(self) -> float:
        return self.matched / self.total if self.total else 0.0

    def to_markdown(self) -> str:
        lines = [
            "## Date Verification Report",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Checked    | {self.total} |",
            f"| Matched    | {self.matched} |",
            f"| Mismatched | {self.mismatched} |",
            f"| Uncertain  | {self.uncertain} |",
            f"| Match rate | {self.match_rate:.0%} |",
            "",
            "### Per-Entity Results",
            "",
        ]
        for r in self.results:
            if r.match:
                icon = "✓"
            elif r.confidence < 0.5:
                icon = "?"
            else:
                icon = "✗"
            lines.append(
                f"- **{icon} {r.entity}**  "
                f"Spreadsheet: `{r.spreadsheet_date or 'N/A'}`  "
                f"Web: `{r.web_date or 'not found'}`  "
                f"Confidence: {r.confidence:.0%}"
            )
            if r.detail:
                lines.append(f"  *{r.detail}*")
        return "\n".join(lines)

    def to_dict_list(self) -> list[dict]:
        return [r.to_dict() for r in self.results]


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def parse_date_from_text(text: str) -> str | None:
    """
    Extract the first recognisable date from arbitrary text.
    Returns ISO 'YYYY-MM-DD' string or None.
    Scans up to 3 000 characters.
    """
    text = text[:3000]

    # ISO
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if m:
        return m.group(1)

    # DD Month YYYY  e.g. "24 May 2026"
    m = re.search(
        r"\b(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may"
        r"|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r",?\s+(\d{4})\b",
        text, re.IGNORECASE,
    )
    if m:
        try:
            day = int(m.group(1))
            mon = _MONTH_MAP[m.group(2).lower()[:3]]
            yr  = int(m.group(3))
            return datetime(yr, mon, day).strftime("%Y-%m-%d")
        except (ValueError, KeyError):
            pass

    # Month DD, YYYY  e.g. "May 24, 2026"
    m = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may"
        r"|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
        text, re.IGNORECASE,
    )
    if m:
        try:
            mon = _MONTH_MAP[m.group(1).lower()[:3]]
            day = int(m.group(2))
            yr  = int(m.group(3))
            return datetime(yr, mon, day).strftime("%Y-%m-%d")
        except (ValueError, KeyError):
            pass

    # MM/DD/YYYY
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None


# ---------------------------------------------------------------------------
# Date comparison
# ---------------------------------------------------------------------------

def compare_dates(d1: str, d2: str, tolerance_days: int = 1) -> tuple[bool, float]:
    """
    Compare two ISO date strings.
    Returns (is_match, confidence) where confidence ∈ [0, 1].
    """
    if not d1 or not d2:
        return False, 0.0
    try:
        dt1  = datetime.fromisoformat(d1)
        dt2  = datetime.fromisoformat(d2)
        diff = abs((dt1 - dt2).days)
        if diff == 0:
            return True,  1.00
        if diff <= tolerance_days:
            return True,  0.85   # off by ≤ 1 day (timezone edge case)
        if diff <= 3:
            return False, 0.70   # close but likely wrong
        return False, 0.95       # clear mismatch — high confidence in the finding
    except Exception:
        return False, 0.0


# ---------------------------------------------------------------------------
# Core verification pipeline
# ---------------------------------------------------------------------------

def verify_match_dates(
    matchups: list,                         # list[Matchup] from entity_extractor
    search_results: dict[str, list[dict]],  # entity_name → raw result dicts
    tolerance_days: int = 1,
) -> list[VerificationResult]:
    """
    Compare spreadsheet Game Date values against dates extracted from web results.

    Args:
        matchups:       Matchup objects produced by entity_extractor.extract_matchups()
        search_results: mapping of entity (Selection) name → list of search result dicts
                        Each dict has 'title', 'body', 'href' keys.
        tolerance_days: days of slack for a "match" (handles timezone offsets).
    """
    results: list[VerificationResult] = []

    for matchup in matchups:
        entity  = matchup.selection
        sp_date = matchup.game_date

        raw = search_results.get(entity, [])
        if not raw:
            results.append(VerificationResult(
                entity=entity,
                spreadsheet_date=sp_date,
                web_date=None,
                match=False,
                confidence=0.0,
                source="",
                detail="No web results found",
            ))
            continue

        # Filter out biographical / out-of-range results before parsing dates
        relevant = filter_relevant_results(raw, entity, sp_date, max_days=365)
        scan     = relevant if relevant else raw   # fall back to unfiltered if all dropped

        # Scan results for a date
        found_date   = None
        source_title = ""
        for r in scan:
            combined = f"{r.get('title', '')} {r.get('body', '')}"
            d = parse_date_from_text(combined)
            if d:
                found_date   = d
                source_title = r.get("title", r.get("href", "web"))[:80]
                break

        if not found_date:
            results.append(VerificationResult(
                entity=entity,
                spreadsheet_date=sp_date,
                web_date=None,
                match=False,
                confidence=0.30,
                source=raw[0].get("title", "") if raw else "",
                detail="Date not found in search results",
            ))
            continue

        is_match, confidence = compare_dates(sp_date, found_date, tolerance_days)

        detail = ""
        if sp_date and found_date:
            try:
                diff = abs((datetime.fromisoformat(sp_date) - datetime.fromisoformat(found_date)).days)
                detail = f"Δ {diff} day(s)"
            except Exception:
                pass

        results.append(VerificationResult(
            entity=entity,
            spreadsheet_date=sp_date,
            web_date=found_date,
            match=is_match,
            confidence=confidence,
            source=source_title,
            detail=detail,
        ))

        log.debug(
            "verification: %-30s  sheet=%-12s  web=%-12s  match=%s  conf=%.0f%%",
            entity[:30], sp_date, found_date, is_match, confidence * 100,
        )

    return results


def generate_verification_summary(results: list[VerificationResult]) -> VerificationSummary:
    matched    = sum(1 for r in results if r.match)
    uncertain  = sum(1 for r in results if not r.match and r.confidence < 0.5)
    mismatched = len(results) - matched - uncertain
    return VerificationSummary(
        total=len(results),
        matched=matched,
        mismatched=mismatched,
        uncertain=uncertain,
        results=results,
    )

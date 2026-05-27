"""
router.py — Intent detection and execution routing.

Two-stage classification:
  1. Keyword scoring  (fast, zero LLM calls)  — handles obvious cases
  2. LLM classification (1 call)              — handles ambiguous queries

Returns a structured IntentResult used by app.py to route execution.
"""

from __future__ import annotations
import re
import json
from dataclasses import dataclass, asdict

from utils import get_logger

log = get_logger("router")

# ---------------------------------------------------------------------------
# Intent taxonomy
# ---------------------------------------------------------------------------

INTENTS = [
    "data_query",
    "report_generation",
    "excel_modification",
    "internet_research",
    "hybrid_analysis",
    "chart_generation",
]

# Keyword → intent scoring weights
_KEYWORD_MAP: dict[str, list[str]] = {
    "report_generation": [
        "report", "analysis", "analyze", "analyse", "breakdown",
        "performance", "summary", "summarize", "generate", "detailed",
        "comprehensive", "deep dive", "overview",
    ],
    "data_query": [
        "show", "find", "list", "count", "how many", "which",
        "filter", "display", "get", "what is", "what are",
        "fetch", "retrieve", "tell me",
    ],
    "excel_modification": [
        "add column", "add a column", "update", "modify", "change",
        "write back", "save to", "risk level", "label", "flag",
        "mark", "append column", "create field", "enrich", "annotate",
    ],
    "internet_research": [
        "news", "injury", "injuries", "latest", "current", "live",
        "today", "recent", "search", "look up", "web", "online",
        "odds change", "transfer", "roster",
    ],
    "chart_generation": [
        "chart", "graph", "plot", "visualize", "visualise",
        "bar chart", "pie chart", "trend", "visual", "diagram",
    ],
}

# Keywords that force specific needs flags regardless of intent
_WEB_KEYWORDS   = {"news", "injury", "injuries", "latest", "live", "today", "search", "online", "web"}
_WRITE_KEYWORDS = {"add column", "update column", "save", "write back", "risk level", "flag", "label", "annotate", "enrich"}
_CHART_KEYWORDS = {"chart", "graph", "plot", "visualize", "visualise", "visual", "bar chart", "pie chart", "trend"}

# If keyword confidence ≥ this, skip LLM call
_KEYWORD_CONFIDENCE_THRESHOLD = 0.60


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass
class IntentResult:
    intent:            str
    confidence:        float
    needs_pandas:      bool
    needs_excel_write: bool
    needs_web_search:  bool
    needs_charts:      bool
    method:            str   # "keyword" | "llm"

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Stage 1 — keyword scoring
# ---------------------------------------------------------------------------

def _keyword_scores(query: str) -> dict[str, float]:
    q = query.lower()
    scores: dict[str, float] = {intent: 0.0 for intent in INTENTS}
    for intent, keywords in _KEYWORD_MAP.items():
        for kw in keywords:
            if kw in q:
                # Multi-word keywords score higher
                scores[intent] += 1.0 + 0.3 * (kw.count(" "))
    return scores


def _needs_flags(query: str, scores: dict[str, float]) -> dict[str, bool]:
    q = query.lower()
    web_hit   = any(kw in q for kw in _WEB_KEYWORDS)
    write_hit = any(kw in q for kw in _WRITE_KEYWORDS)
    chart_hit = any(kw in q for kw in _CHART_KEYWORDS)

    # pandas is needed for any data-touching intent
    data_score = scores.get("data_query", 0) + scores.get("report_generation", 0)
    return {
        "needs_pandas":      data_score > 0 or not web_hit,
        "needs_web_search":  web_hit or scores.get("internet_research", 0) > 0,
        "needs_excel_write": write_hit or scores.get("excel_modification", 0) > 0,
        "needs_charts":      chart_hit or scores.get("chart_generation", 0) > 0
                             or scores.get("report_generation", 0) > 0,
    }


def _keyword_classify(query: str) -> tuple[str, float, str]:
    """
    Returns (intent, confidence, 'keyword').
    confidence < threshold → caller should fall back to LLM.
    """
    scores = _keyword_scores(query)
    total  = sum(scores.values())

    if total == 0:
        return "data_query", 0.1, "keyword"   # nothing matched → ambiguous

    best        = max(scores, key=scores.get)
    best_score  = scores[best]
    confidence  = min(0.95, 0.4 + (best_score / total) * 0.6)

    # Hybrid: web + pandas both scored
    if (scores.get("internet_research", 0) > 0
            and scores.get("data_query", 0) + scores.get("report_generation", 0) > 0):
        return "hybrid_analysis", min(0.90, confidence + 0.05), "keyword"

    return best, confidence, "keyword"


# ---------------------------------------------------------------------------
# Stage 2 — LLM classification (called only when keyword confidence is low)
# ---------------------------------------------------------------------------

_LLM_SYSTEM = "You are an intent classifier. Reply with ONLY one intent name."
_LLM_USER   = """\
Classify this query into exactly one intent:
  data_query          — retrieve / filter / count data
  report_generation   — multi-step analysis and written report
  excel_modification  — add/update/write columns to the spreadsheet
  internet_research   — search web, news, live odds/injuries
  hybrid_analysis     — combines data analysis + web search
  chart_generation    — produce a chart or visualisation

Query: "{query}"

Reply with ONE word from the list above.\
"""


def _llm_classify(query: str) -> tuple[str, float]:
    from llm import call_chat  # imported here to avoid circular at module load
    messages = [
        {"role": "system", "content": _LLM_SYSTEM},
        {"role": "user",   "content": _LLM_USER.format(query=query)},
    ]
    raw    = call_chat(messages, stream_to_stdout=False).strip().lower()
    intent = next((i for i in INTENTS if i in raw), "data_query")
    log.debug("llm_classify: raw=%r → intent=%s", raw[:60], intent)
    return intent, 0.75   # LLM is used when keywords were ambiguous → moderate confidence


# ---------------------------------------------------------------------------
# Follow-up reference detection
# ---------------------------------------------------------------------------

_FOLLOWUP_RE = re.compile(
    r"\b(?:verify|check|confirm|search|look\s*up|get|find|update)\s+"
    r"(?:those|these|them|that|it|above|previous|the\s+(?:above|previous|last))\b"
    r"|\bfor\s+(?:those|these|them)\b"
    r"|\b(?:verify|confirm|check)\s+(?:if|whether)\s+(?:the\s+)?"
    r"(?:dates?|fixtures?|matches?|games?|schedules?|results?)\s+(?:are|is)\s+"
    r"(?:correct|right|accurate|valid)\b",
    re.IGNORECASE,
)

_DATE_VERIFY_RE = re.compile(
    r"\b(?:verify|confirm|check|validate)\s+(?:the\s+)?(?:dates?|schedule|fixture)\b"
    r"|\bdate[s]?\s+(?:correct|right|accurate|match|valid)\b",
    re.IGNORECASE,
)


def is_followup_reference(query: str) -> bool:
    """True when the query refers to a previous result ('verify those', 'check these')."""
    return bool(_FOLLOWUP_RE.search(query))


def is_date_verification(query: str) -> bool:
    """True when the user wants to verify match dates against live data."""
    q = query.lower()
    return bool(_DATE_VERIFY_RE.search(q)) or (
        any(w in q for w in ("verify", "correct", "accurate", "validate"))
        and any(w in q for w in ("date", "schedule", "fixture", "match", "game"))
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_intent(query: str, schema=None) -> IntentResult:
    """
    Detect user intent from the natural-language query.

    Logs:
      - keyword scores
      - chosen intent + method used
      - confidence
    """
    scores = _keyword_scores(query)
    log.debug("router: keyword scores=%s", {k: round(v, 1) for k, v in scores.items() if v > 0})

    intent, confidence, method = _keyword_classify(query)

    if confidence < _KEYWORD_CONFIDENCE_THRESHOLD:
        log.debug("router: low keyword confidence (%.2f) → calling LLM", confidence)
        intent, confidence = _llm_classify(query)
        method = "llm"

    flags = _needs_flags(query, scores)

    result = IntentResult(
        intent=intent,
        confidence=round(confidence, 2),
        method=method,
        **flags,
    )

    log.debug(
        "router: intent=%s  conf=%.0f%%  method=%s  web=%s  write=%s  charts=%s",
        result.intent, result.confidence * 100, result.method,
        result.needs_web_search, result.needs_excel_write, result.needs_charts,
    )
    return result

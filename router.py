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
    "document_modification",
    "workspace_operation",
    "internet_research",
    "hybrid_analysis",
    "chart_generation",
]


# ---------------------------------------------------------------------------
# Deterministic regex router (Fix #5)
# ---------------------------------------------------------------------------
#
# Catches the common cases before any keyword scoring / LLM call. Returns
# None for "I don't know — fall through to keyword scoring". The point is
# to FIX the brittle case where "what sports are in the excel" was being
# routed to excel_modification.

_VERIFY_RE = re.compile(
    r"^\s*(verify|validate|confirm|check\s+(?:online|the\s+web|live))\b",
    re.I,
)
_DATE_VERIFY_TIGHT = re.compile(
    r"\b(?:verify|validate|confirm)\s+(?:the\s+)?(?:dates?|schedule|fixtures?|results?)\b",
    re.I,
)
_REPORT_RE = re.compile(
    r"^\s*(?:"
    # "create/generate [a|the] [full|detailed|comprehensive|deep] report/analysis/summary/…"
    r"(?:generate|create|build|produce|write)\s+(?:a\s+|the\s+)?"
    r"(?:(?:full|detailed|comprehensive|deep|professional|executive)\s+)?"
    r"(?:report|summary|analysis|breakdown|brief)"
    # bare adjective+noun form ("a detailed report")
    r"|(?:full|detailed|comprehensive|deep)\s+(?:report|analysis|breakdown)"
    # analyse <scope>
    r"|analy[sz]e\s+(?:this|all|the\s+whole|everything|the\s+entire)"
    r")\b",
    re.I,
)
_WRITE_RE = re.compile(
    r"^\s*(?:"
    # add/append/create [a] [new] column
    r"add\s+(?:a\s+)?(?:new\s+)?column\b"
    r"|append\s+(?:a\s+)?column\b"
    r"|create\s+(?:a\s+)?column\b"
    # update [the] [<name>] column / field / value
    r"|update\s+(?:the\s+)?(?:\w+\s+)?(?:column|field|value)\b"
    # replace X with Y
    r"|replace\s+\w+"
    # set the X to Y
    r"|set\s+(?:the\s+)?\w+\s+to\s+"
    # change the X
    r"|change\s+(?:the\s+)?\w+"
    # delete the first/last/all rows / columns / a specific entity
    r"|delete\s+(?:the\s+)?(?:first|last|all)?\s*(?:rows?|columns?|entries|paragraphs?|values?)\b"
    r"|delete\s+(?:the\s+)?\w+"
    # remove rows / columns / paragraphs
    r"|remove\s+(?:the\s+)?(?:rows?|columns?|paragraphs?)\b"
    # insert a row / column / paragraph
    r"|insert\s+(?:a\s+)?(?:row|column|paragraph)\b"
    # mark / flag rows
    r"|mark\s+(?:every|all|rows?)\b"
    r"|flag\s+\w+"
    # explicit save commands
    r"|save\s+(?:to|as)\s+"
    r"|write\s+back\b"
    r")",
    re.I,
)
_CHART_RE = re.compile(
    r"^\s*(?:"
    r"chart|plot|graph|visuali[sz]e|bar\s+chart|pie\s+chart|line\s+chart|histogram"
    r")\b",
    re.I,
)
# READ verbs that should NEVER be misrouted to a mutation flow
_READ_RE = re.compile(
    r"^\s*(?:"
    r"show|list|find|count|how\s+many|how\s+much|which|what(?:\s+(?:is|are|was|were))?"
    r"|get|fetch|retrieve|tell\s+me|describe|summari[sz]e\s+(?:in\s+)?one"
    r"|filter|sort\s+by|group\s+by|top\s+\d|bottom\s+\d|highest|lowest"
    r"|average|mean|median|sum\s+(?:of\s+)?\S"
    r"|when|where|who"
    r")\b",
    re.I,
)


def _regex_route(query: str) -> str | None:
    """
    Cheap deterministic router. Returns an intent string or None if no
    rule fires. Order matters: VERIFY beats READ ("verify the totals"
    must not route to data_query).
    """
    if _VERIFY_RE.search(query) or _DATE_VERIFY_TIGHT.search(query):
        return "internet_research"
    if _REPORT_RE.search(query):
        return "report_generation"
    if _CHART_RE.search(query):
        return "chart_generation"
    if _READ_RE.search(query):
        return "data_query"
    if _WRITE_RE.search(query):
        return "excel_modification"
    return None

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
        "add column", "add a column", "update column", "write back",
        "risk level", "label", "flag", "mark", "append column",
        "create field", "enrich", "annotate",
    ],
    "document_modification": [
        "edit the doc", "edit the document", "update the doc",
        "update the document", "rewrite", "replace text",
        "insert paragraph", "append to the doc", "modify the contract",
        "update the contract", "change the contract", "add to the document",
        "inject", "fill in the doc", "fill the template",
    ],
    "workspace_operation": [
        "from the doc", "from the document", "from the contract",
        "into the doc", "into the document", "into the spreadsheet",
        "match against", "cross-reference", "join with the doc",
        "convert to docx", "convert to spreadsheet",
        "export to docx", "export to xlsx", "export to csv",
        "extract from the document", "extract from the contract",
        "compare with the doc", "compare with the document",
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
  data_query             — retrieve / filter / count data
  report_generation      — multi-step analysis and written report
  excel_modification     — add/update/write columns to a spreadsheet
  document_modification  — edit, rewrite or update a Word document
  workspace_operation    — cross-object work spanning spreadsheets + documents
  internet_research      — search web, news, live odds/injuries
  hybrid_analysis        — combines data analysis + web search
  chart_generation       — produce a chart or visualisation

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
    Detect user intent. Priority:
      1. Deterministic regex (covers ~90% of real queries cheaply)
      2. Keyword scoring (back-compat for queries the regex misses)
      3. LLM fallback (only when keyword scoring is ambiguous)
    """
    scores = _keyword_scores(query)
    log.debug("router: keyword scores=%s", {k: round(v, 1) for k, v in scores.items() if v > 0})

    # 1. Regex first — deterministic, ~10µs
    regex_intent = _regex_route(query)
    if regex_intent is not None:
        flags = _needs_flags(query, scores)
        result = IntentResult(
            intent=regex_intent,
            confidence=0.95,
            method="regex",
            **flags,
        )
        log.debug("router: regex → %s", regex_intent)
        return result

    # 2. Keyword scoring (existing path)
    intent, confidence, method = _keyword_classify(query)

    # 3. LLM fallback only when truly ambiguous
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
        "router: intent=%s  conf=%.0f%%  method=%s",
        result.intent, result.confidence * 100, result.method,
    )
    return result

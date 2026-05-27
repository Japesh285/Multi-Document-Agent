"""
entity_extractor.py — Context-aware entity extraction from DataFrame subsets.

Extracts teams, leagues, dates, and matchups from:
  - DataFrame subsets (last query result stored in session)
  - AnalysisMemory step results
  - Raw query text (LLM fallback)

Produces structured EntitySet and targeted web search queries.
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass, field

import pandas as pd

from utils import get_logger

log = get_logger("entity_extractor")

MAX_MATCHUPS = 20   # cap per extraction to avoid search spam

# ---------------------------------------------------------------------------
# Selection field cleaning
# ---------------------------------------------------------------------------
# The Selection column holds raw bet strings like:
#   "[932] CIN REDS GM#1 -105 ( ACTION )"
#   "[907] TOTAL o9½-107 (STL CARDINALS GM#2 vrs CIN REDS GM#2) ( ACTION )"
#   "PARLAY (5 TEAMS): [24513] DAN HOLT +133 + ..."
# We extract clean team names and discard everything else.

_TICKET_ID_RE   = re.compile(r"^\[\d+\]\s*")
_ODDS_RE        = re.compile(r"[-+][¼½¾]?\d+(?:\.\d+)?")
_COMPLETE_PAREN = re.compile(r"\([^)]*\)")
_OPEN_PAREN_EOL = re.compile(r"\([^)]*$")
_NOISE_WORDS    = re.compile(r"\bGM#\d+\b|\b[12]H\b|\bTOTAL\b|\b[ou][\d½¾¼]+\b", re.IGNORECASE)
_STRAY_PARENS   = re.compile(r"[)(]")
_DIGITS_ONLY    = re.compile(r"^[\d\s.+\-½¾¼]+$")


def _clean_team_name(raw: str) -> str:
    """Strip ticket IDs, odds, noise keywords, and parens from a raw team fragment."""
    s = raw.strip()
    s = _TICKET_ID_RE.sub("", s)    # [932]
    s = _COMPLETE_PAREN.sub("", s)  # (SETS), ( ACTION )
    s = _OPEN_PAREN_EOL.sub("", s)  # unclosed ( at end of string
    s = _ODDS_RE.sub("", s)         # -105, +¾-116
    s = _NOISE_WORDS.sub("", s)     # GM#1, 1H, TOTAL, o9½
    s = _STRAY_PARENS.sub("", s)    # stray ) or (
    s = " ".join(s.split())
    if len(s) < 2 or _DIGITS_ONLY.match(s):
        return ""
    return s.strip()


def _extract_from_selection(sel: str) -> list[str]:
    """
    Extract clean team name(s) from one Selection column value.

    Rules:
      - PARLAY entries       → skip (too noisy)
      - YES/NO prop bets     → skip
      - "X vrs Y" pattern    → extract both sides
      - Bare TOTAL (no vrs)  → skip (no useful team name)
      - Single team          → clean and return
    """
    sel = sel.strip()
    if not sel or sel.lower() == "nan":
        return []

    if re.match(r"PARLAY", sel, re.IGNORECASE):
        return []

    s = _TICKET_ID_RE.sub("", sel).strip()

    if re.match(r"^(?:YES|NO)\s+[-+]", s, re.IGNORECASE):
        return []

    lower = s.lower()
    if " vrs " in lower:
        idx   = lower.index(" vrs ")
        left  = s[:idx]
        right = s[idx + 5:]

        # Remove "TOTAL o9½-107 (" prefix from left side
        left = re.sub(
            r"^TOTAL\s+[ou][\d½¾¼]+[-+][\d.]*\s*\(?\s*",
            "", left, flags=re.IGNORECASE,
        ).strip().lstrip("(").strip()

        right = right.split(")")[0].strip()  # take up to first closing paren

        teams = []
        for raw in (left, right):
            t = _clean_team_name(raw)
            if t:
                teams.append(t)
        return teams

    if re.match(r"^TOTAL\s+", s, re.IGNORECASE):
        return []

    t = _clean_team_name(s)
    return [t] if t else []


_MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May",     6: "June",     7: "July",  8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}

_INTENT_KEYWORDS = {
    "fixture": ["match", "fixture", "schedule", "scheduled", "kick off", "tip off", "game", "date", "verify", "correct"],
    "injury":  ["injur", "injured", "out", "fit", "fitness", "unavailable", "doubtful", "health"],
    "odds":    ["odds", "line", "spread", "price", "moneyline", "handicap", "over", "under"],
    "result":  ["result", "score", "outcome", "won", "lost", "final"],
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Matchup:
    selection: str
    sport:     str
    game_date: str      # ISO date string, may be ""
    bet_type:  str
    result:    str

    def date_label(self) -> str:
        """'May 2026' style label for search queries."""
        if not self.game_date:
            return ""
        try:
            ts = pd.Timestamp(self.game_date)
            return f"{_MONTH_NAMES[ts.month]} {ts.year}"
        except Exception:
            return self.game_date


@dataclass
class EntitySet:
    teams:    list[str]    = field(default_factory=list)
    leagues:  list[str]    = field(default_factory=list)
    dates:    list[str]    = field(default_factory=list)
    matchups: list[Matchup] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.teams or self.leagues or self.dates or self.matchups)

    def unique_teams(self) -> list[str]:
        seen, out = set(), []
        for t in self.teams:
            k = t.lower().strip()
            if k and k not in seen:
                seen.add(k)
                out.append(t.strip())
        return out


# ---------------------------------------------------------------------------
# Column-based extractors (fast, no LLM)
# ---------------------------------------------------------------------------

# Detects values that use the sports-betting bracket/odds format
_BETTING_VAL_RE = re.compile(r"^\s*\[\d+\]|[-+]\d{3}\b|\(\s*ACTION\s*\)")


def _extract_entity_name(val: str) -> list[str]:
    """
    Auto-detect format and return clean entity name(s) from a column value.
    Betting-style values use the full parser; plain values are returned as-is.
    """
    val = val.strip()
    if not val or val.lower() == "nan":
        return []
    if _BETTING_VAL_RE.search(val):
        return _extract_from_selection(val)
    # Plain value (CRM name, product name, etc.) — light cleanup only
    cleaned = val.strip()
    return [cleaned] if len(cleaned) > 1 else []


def extract_teams(df: pd.DataFrame, entity_cols: list[str] | None = None) -> list[str]:
    """Extract unique entity/team names from entity columns."""
    if entity_cols is None:
        # Fallback: any column whose name suggests entities
        entity_cols = [
            c for c in df.columns
            if any(w in c.lower() for w in
                   ("name", "team", "player", "company", "customer",
                    "client", "selection", "product", "vendor", "supplier"))
        ] or (["Selection"] if "Selection" in df.columns else [])

    seen, out = set(), []
    for col in entity_cols:
        if col not in df.columns:
            continue
        for raw in df[col].dropna().astype(str):
            for name in _extract_entity_name(raw):
                k = name.lower()
                if k not in seen:
                    seen.add(k)
                    out.append(name)
    return sorted(out)


def extract_leagues(df: pd.DataFrame) -> list[str]:
    """Extract unique categorical league/sport/category values."""
    cat_cols = [
        c for c in df.columns
        if any(w in c.lower() for w in ("sport", "league", "category", "division", "competition"))
    ]
    seen, out = set(), []
    for col in cat_cols:
        for v in df[col].dropna().astype(str).str.strip():
            if v.lower() != "nan" and v not in seen:
                seen.add(v)
                out.append(v)
    return sorted(out)


def extract_dates(df: pd.DataFrame, date_cols: list[str] | None = None) -> list[str]:
    """Extract unique ISO dates from date columns."""
    if date_cols is None:
        date_cols = [
            c for c in df.columns
            if any(w in c.lower() for w in ("date", "time", "day", "timestamp"))
        ]
    out: set[str] = set()
    for col in date_cols:
        if col not in df.columns:
            continue
        for d in df[col].dropna().unique():
            try:
                out.add(pd.Timestamp(d).strftime("%Y-%m-%d"))
            except Exception:
                pass
    return sorted(out)


def extract_matchups(
    df:          pd.DataFrame,
    entity_cols: list[str] | None = None,
    date_cols:   list[str] | None = None,
) -> list[Matchup]:
    """
    Extract unique (entity, date) matchups.
    Deduplicates by (clean_name, game_date) and caps at MAX_MATCHUPS.
    Works with any entity column, not just 'Selection'.
    """
    if entity_cols is None:
        entity_cols = [
            c for c in df.columns
            if any(w in c.lower() for w in
                   ("name", "team", "player", "company", "customer",
                    "client", "selection", "product"))
        ] or (["Selection"] if "Selection" in df.columns else [])

    if date_cols is None:
        date_cols = [
            c for c in df.columns
            if any(w in c.lower() for w in ("date", "time", "day", "timestamp"))
        ]

    # Find first available sport/category and result columns
    sport_col   = next((c for c in df.columns if any(w in c.lower() for w in ("sport", "league", "category"))), None)
    type_col    = next((c for c in df.columns if any(w in c.lower() for w in ("type", "class", "kind"))), None)
    result_col  = next((c for c in df.columns if any(w in c.lower() for w in ("result", "outcome", "status"))), None)
    date_col    = date_cols[0] if date_cols else None

    seen:     set[tuple[str, str]] = set()
    matchups: list[Matchup]        = []

    for _, row in df.iterrows():
        # Try each entity column
        entity_names: list[str] = []
        for col in entity_cols:
            raw = str(row.get(col, "")).strip()
            entity_names.extend(_extract_entity_name(raw))

        if not entity_names:
            continue

        raw_date = row.get(date_col) if date_col else None
        try:
            game_date = pd.Timestamp(raw_date).strftime("%Y-%m-%d") if raw_date else ""
        except Exception:
            game_date = ""

        sport    = str(row.get(sport_col,  "")).strip() if sport_col  else ""
        bet_type = str(row.get(type_col,   "")).strip() if type_col   else ""
        result   = str(row.get(result_col, "")).strip() if result_col else ""

        for name in entity_names:
            key = (name.lower(), game_date)
            if key in seen:
                continue
            seen.add(key)
            matchups.append(Matchup(
                selection=name,
                sport=sport,
                game_date=game_date,
                bet_type=bet_type,
                result=result,
            ))
            if len(matchups) >= MAX_MATCHUPS:
                log.debug("entity_extractor: capped at %d matchups", MAX_MATCHUPS)
                return matchups

    return matchups


def extract_entities(df: pd.DataFrame, profile=None) -> EntitySet:
    """
    Extract all entity types from a DataFrame subset.

    Args:
        df:      DataFrame to extract from (typically _session.last_df_result)
        profile: Optional DataFrameProfile — if provided, column roles are taken
                 from profiler output instead of name heuristics.
    """
    if df is None or df.empty:
        return EntitySet()

    entity_cols = getattr(profile, "entity_columns", None)
    date_cols   = getattr(profile, "date_columns",   None)

    es = EntitySet(
        teams=extract_teams(df, entity_cols),
        leagues=extract_leagues(df),
        dates=extract_dates(df, date_cols),
        matchups=extract_matchups(df, entity_cols, date_cols),
    )
    log.debug(
        "entity_extractor: %d teams, %d leagues, %d dates, %d matchups",
        len(es.teams), len(es.leagues), len(es.dates), len(es.matchups),
    )
    return es


# ---------------------------------------------------------------------------
# Search query builder
# ---------------------------------------------------------------------------

def infer_search_intent(query: str) -> str:
    """Derive 'fixture' | 'injury' | 'odds' | 'result' from query text."""
    q = query.lower()
    for intent, keywords in _INTENT_KEYWORDS.items():
        if any(k in q for k in keywords):
            return intent
    return "fixture"


def build_search_queries(
    entities: EntitySet,
    intent_hint: str = "fixture",
    max_queries: int = 8,
) -> list[str]:
    """
    Build targeted, structured web search queries from extracted entities.

    Examples (intent_hint='fixture'):
      "COMO soccer match fixture May 2026"
      "Racing Santander soccer match fixture May 2026"
    """
    keyword_map = {
        "fixture": "match fixture",
        "injury":  "injury report",
        "odds":    "betting odds",
        "result":  "match result score",
    }
    kw = keyword_map.get(intent_hint, "match fixture")

    # Fallback date label from entity set
    fallback_date = ""
    if entities.dates:
        try:
            ts = pd.Timestamp(sorted(entities.dates)[-1])
            fallback_date = f"{_MONTH_NAMES[ts.month]} {ts.year}"
        except Exception:
            pass

    queries: list[str] = []
    seen = set()

    for matchup in entities.matchups[:max_queries]:
        date_part  = matchup.date_label() or fallback_date
        sport_part = matchup.sport or ""
        q = " ".join(filter(None, [matchup.selection, sport_part, kw, date_part]))
        if q not in seen:
            seen.add(q)
            queries.append(q)

    # Fallback: team-only queries when no matchups
    if not queries:
        for team in entities.unique_teams()[:max_queries]:
            q = " ".join(filter(None, [team, kw, fallback_date]))
            if q not in seen:
                seen.add(q)
                queries.append(q)

    log.debug("entity_extractor: %d queries for intent=%s", len(queries), intent_hint)
    return queries[:max_queries]


# ---------------------------------------------------------------------------
# LLM fallback — extract entities from freeform text
# ---------------------------------------------------------------------------

def extract_entities_from_text(
    text: str,
    known_sports: list[str] | None = None,
) -> EntitySet:
    """Use Ollama to extract entities when no DataFrame context is available."""
    from llm import call_chat  # noqa: PLC0415

    sports_hint = f"Known sports: {', '.join(known_sports)}" if known_sports else ""
    msgs = [
        {"role": "system", "content": "Extract sports entities. Return ONLY valid JSON, no prose."},
        {
            "role": "user",
            "content": (
                f"{sports_hint}\n"
                f'Text: "{text}"\n\n'
                'Return: {"teams": [], "leagues": [], "dates": []}'
            ),
        },
    ]
    raw = call_chat(msgs, stream_to_stdout=False).strip()
    try:
        raw = re.sub(r"```(?:json)?", "", raw).strip().strip("`")
        data = json.loads(raw)
        if isinstance(data, dict):
            return EntitySet(
                teams=data.get("teams", []),
                leagues=data.get("leagues", []),
                dates=data.get("dates", []),
            )
    except Exception:
        log.debug("entity_extractor: LLM parse failed for %r", text[:80])
    return EntitySet()

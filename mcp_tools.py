"""
mcp_tools.py — Web search and external data tools.

Primary:  duckduckgo-search library (free, no API key)
Fallback: Ollama's knowledge base (when duckduckgo-search not installed)

Results are cached to disk (output/search_cache/) with a 24-hour TTL
to avoid redundant network calls for repeated queries.
"""

from __future__ import annotations
import json
import hashlib
import time
from pathlib import Path
from typing import Any

from utils import get_logger

log = get_logger("mcp_tools")

_CACHE_DIR = Path("output") / "search_cache"
_CACHE_TTL = 60 * 60 * 24   # 24 hours in seconds

# Try to import duckduckgo_search — gracefully optional
try:
    from ddgs import DDGS                      # new package name (v1+)
    _DDGS_AVAILABLE = True
    log.debug("mcp_tools: ddgs available")
except ImportError:
    try:
        from duckduckgo_search import DDGS     # legacy fallback
        _DDGS_AVAILABLE = True
        log.debug("mcp_tools: duckduckgo_search available (legacy)")
    except ImportError:
        _DDGS_AVAILABLE = False
        log.debug("mcp_tools: no search library — Ollama fallback active")


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _ensure_cache_dir() -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_key(query: str, kind: str = "web") -> str:
    return hashlib.md5(f"{kind}:{query}".encode()).hexdigest()[:16]


def _load_cache(key: str) -> list[dict] | None:
    _ensure_cache_dir()
    path = _CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        age  = time.time() - data.get("ts", 0)
        if age > _CACHE_TTL:
            path.unlink(missing_ok=True)
            return None
        log.debug("mcp_tools: cache hit %s (age %.0fh)", key, age / 3600)
        return data["results"]
    except Exception:
        return None


def _save_cache(key: str, results: list[dict]) -> None:
    _ensure_cache_dir()
    path = _CACHE_DIR / f"{key}.json"
    path.write_text(
        json.dumps({"ts": time.time(), "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# DuckDuckGo search
# ---------------------------------------------------------------------------

def _ddg_text(query: str, max_results: int = 6) -> list[dict]:
    results = []
    try:
        ddgs = DDGS()
        for r in ddgs.text(query, max_results=max_results):
            results.append({
                "title": r.get("title", ""),
                "body":  r.get("body", "")[:600],
                "href":  r.get("href", ""),
            })
    except Exception as exc:
        log.warning("mcp_tools: DuckDuckGo text search failed — %s", exc)
    return results


def _ddg_news(query: str, max_results: int = 5) -> list[dict]:
    results = []
    try:
        ddgs = DDGS()
        for r in ddgs.news(query, max_results=max_results):
            results.append({
                "title": r.get("title", ""),
                "body":  r.get("body", "")[:500],
                "href":  r.get("url", ""),
                "date":  r.get("date", ""),
            })
    except Exception as exc:
        log.warning("mcp_tools: DuckDuckGo news search failed — %s", exc)
    return results


# ---------------------------------------------------------------------------
# Ollama fallback
# ---------------------------------------------------------------------------

def _ollama_research(query: str) -> list[dict]:
    """Use Ollama's training knowledge when live search is unavailable."""
    from llm import call_chat
    msgs = [
        {
            "role": "system",
            "content": (
                "You are a sports research assistant. Provide factual information. "
                "If you don't know something recent (after your training cutoff), say clearly: "
                "'I do not have current data on this.' Do not hallucinate recent events."
            ),
        },
        {
            "role": "user",
            "content": f"Research query: {query}\n\nProvide 3-5 key facts as a concise list.",
        },
    ]
    response = call_chat(msgs, stream_to_stdout=False)
    return [{"title": "Ollama Knowledge Base", "body": response, "href": "", "date": ""}]


# ---------------------------------------------------------------------------
# Public search tools
# ---------------------------------------------------------------------------

def search_web(query: str, max_results: int = 6) -> list[dict]:
    """General web search. Returns list of {title, body, href}."""
    key    = _cache_key(query, "web")
    cached = _load_cache(key)
    if cached is not None:
        return cached

    results = _ddg_text(query, max_results) if _DDGS_AVAILABLE else _ollama_research(query)
    _save_cache(key, results)
    log.debug("mcp_tools: search_web %r → %d results", query[:50], len(results))
    return results


def search_sports_news(team_or_topic: str, sport: str = "") -> list[dict]:
    """Search recent sports news for a team or topic."""
    query  = f"{team_or_topic} {sport} latest news".strip()
    key    = _cache_key(query, "news")
    cached = _load_cache(key)
    if cached is not None:
        return cached

    results = _ddg_news(query) if _DDGS_AVAILABLE else _ollama_research(query)
    _save_cache(key, results)
    log.debug("mcp_tools: sports_news %r → %d results", query[:50], len(results))
    return results


def fetch_odds(team: str, sport: str = "") -> list[dict]:
    """Fetch current betting odds for a team."""
    query = f"{team} {sport} betting odds lines today".strip()
    return search_web(query, max_results=4)


def fetch_injuries(team: str, sport: str = "") -> list[dict]:
    """Fetch injury report for a team."""
    query = f"{team} {sport} injury report latest".strip()
    return search_sports_news(query)


def fetch_schedule(team: str) -> list[dict]:
    """Fetch upcoming schedule for a team."""
    return search_web(f"{team} upcoming schedule next games", max_results=4)


# ---------------------------------------------------------------------------
# Entity-aware search
# ---------------------------------------------------------------------------

def search_entities(
    queries: list[str],
    use_news: bool = False,
    max_per_query: int = 5,
) -> dict[str, list[dict]]:
    """
    Run one search per structured query and return a mapping of
    first token (entity name) → result list.

    Args:
        queries:       Structured queries from entity_extractor.build_search_queries()
        use_news:      Use ddgs.news() instead of ddgs.text()
        max_per_query: Max results per query

    Returns:
        {entity_name: [result_dicts]}
    """
    out: dict[str, list[dict]] = {}
    for q in queries:
        entity_key = q.split()[0] if q else q
        cached_key = _cache_key(q, "news" if use_news else "web")
        cached     = _load_cache(cached_key)

        if cached is not None:
            out[entity_key] = cached
            continue

        results = (
            _ddg_news(q, max_per_query)
            if (use_news and _DDGS_AVAILABLE)
            else _ddg_text(q, max_per_query)
            if _DDGS_AVAILABLE
            else _ollama_research(q)
        )
        _save_cache(cached_key, results)
        out[entity_key] = results
        log.debug("mcp_tools: search_entities %r → %d results", q[:50], len(results))

    return out


# ---------------------------------------------------------------------------
# Summarisation
# ---------------------------------------------------------------------------

def summarize_results(
    results: list[dict],
    context: str = "",
    max_bullets: int = 5,
) -> str:
    """Summarise raw search results into clean bullet points using Ollama."""
    from llm import call_chat

    if not results:
        return "No results found."

    raw = "\n\n".join(
        f"[{r.get('title', 'Source')}]\n{r.get('body', '')[:400]}"
        for r in results[:6]
    )

    msgs = [
        {"role": "system", "content": "Summarise search results into concise, factual bullet points."},
        {
            "role": "user",
            "content": (
                f"Context: {context}\n\n"
                f"Search results:\n{raw}\n\n"
                f"Summarise into {max_bullets} key bullet points. "
                "State clearly if information may be outdated."
            ),
        },
    ]
    return call_chat(msgs, stream_to_stdout=False)


def extract_teams_from_query(query: str, schema_sports: list[str]) -> list[str]:
    """
    Use Ollama to extract team names mentioned in the query.
    Returns a list of team/entity names for subsequent news searches.
    """
    from llm import call_chat

    msgs = [
        {"role": "system", "content": "Extract team or player names from the query. Return ONLY a JSON array of strings."},
        {
            "role": "user",
            "content": f"Known sports: {', '.join(schema_sports)}\nQuery: {query}\nReturn: [\"Team1\", \"Team2\"]",
        },
    ]
    raw = call_chat(msgs, stream_to_stdout=False).strip()
    try:
        raw = raw.strip("`").replace("json", "").strip()
        teams = json.loads(raw)
        if isinstance(teams, list):
            return [str(t) for t in teams[:5]]
    except Exception:
        pass
    return []

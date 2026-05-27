"""
core.reference_resolver — turn conversational references into workspace objects.

Handles phrases like:
  "that contract"          → most recent document
  "the pricing table"      → table whose name or heading mentions 'pricing'
  "those rows"             → memory.last_dataframe_result
  "the second spreadsheet" → ordinal lookup over workspace.spreadsheets
  "the first table"        → ordinal lookup over workspace.tables
  "crm spreadsheet"        → name match

Returns a `ResolvedReference` — never raises on miss; caller decides what to do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from utils import get_logger

from .workspace_manager import Workspace
from .workspace_objects import DocumentObject, SpreadsheetObject, TableObject, WorkspaceObject

log = get_logger("reference_resolver")


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class ResolvedReference:
    """Outcome of attempting to resolve a phrase against the workspace."""

    matched: bool                       = False
    object:  WorkspaceObject | None     = None
    # "those rows" / "that result" resolve to a DataFrame in memory, not a registered object
    raw_value: Any                      = None
    kind:     str                       = ""    # "spreadsheet"|"document"|"table"|"result"|""
    confidence: float                   = 0.0
    rationale: str                      = ""

    def __bool__(self) -> bool:
        return self.matched


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------


# Phrases that point to the *last query result*, not a registered object.
# The pattern accepts both forms with a noun ("those rows") and without
# ("filter those"), since bare those/these/them is the most common
# follow-up phrasing in conversation.
_LAST_RESULT_RE = re.compile(
    r"\b("
    r"those\s+(?:rows|results|entries|records|items)"
    r"|these\s+(?:rows|results|entries|records|items)"
    r"|the\s+(?:last|previous)\s+(?:result|rows|output|query)"
    r"|same\s+(?:rows|result|data)"
    r"|that\s+(?:result|output|table\s+from\s+before|finding)"
    # bare demonstratives — only treat as a result reference when used as
    # an object pronoun (verb + those/these/them, e.g. "filter those",
    # "verify these", "with them")
    r"|(?:filter|verify|check|show|count|sort|group|update|use|"
    r"with|of|from|against)\s+(?:those|these|them)\b"
    r")\b",
    re.I,
)

# "the second spreadsheet" / "the third table"
_ORDINAL_WORDS = {
    "first": 1, "1st": 1,
    "second": 2, "2nd": 2,
    "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5,
    "last": -1,
}
_ORDINAL_RE = re.compile(
    r"\b(first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th)\s+"
    r"(spreadsheet|workbook|document|doc|table|file)\b",
    re.I,
)

# "that contract" / "the doc" / "the spreadsheet"
_KIND_DEMONSTRATIVE_RE = re.compile(
    r"\b(that|the|this|those|these)\s+"
    r"(spreadsheet|workbook|document|doc|docx|contract|report|table|csv|excel|file)s?\b",
    re.I,
)

# Quoted name or backtick-quoted name: 'crm', "contract", `pricing_table`
_QUOTED_NAME_RE = re.compile(r"['\"`]([\w\- .]+)['\"`]")


_KIND_GROUPS = {
    "spreadsheet": ("spreadsheets",),
    "workbook":    ("spreadsheets",),
    "excel":       ("spreadsheets",),
    "csv":         ("spreadsheets",),
    "document":    ("documents",),
    "doc":         ("documents",),
    "docx":        ("documents",),
    "contract":    ("documents",),
    "report":      ("documents",),
    "table":       ("tables",),
    "file":        ("spreadsheets", "documents"),   # ambiguous
}


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class ReferenceResolver:
    """
    Stateless except for the workspace it's pointed at. One resolver per
    Workspace; call `resolve(query)` per turn.
    """

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    # ------------------------------------------------------------------
    # Top-level
    # ------------------------------------------------------------------

    def resolve(self, query: str) -> ResolvedReference:
        if not query:
            return ResolvedReference(rationale="empty query")

        # 1. Past-result phrases ("those rows", "the previous output")
        if _LAST_RESULT_RE.search(query):
            df = self.workspace.memory.last_dataframe_result
            if df is not None:
                return ResolvedReference(
                    matched=True, raw_value=df, kind="result",
                    confidence=0.9, rationale="phrase referenced last query result",
                )

        # 2. Ordinal phrases ("the second table")
        m = _ORDINAL_RE.search(query)
        if m:
            ord_word, kind_word = m.group(1).lower(), m.group(2).lower()
            index = _ORDINAL_WORDS.get(ord_word)
            registries = self._registries_for_kind(kind_word)
            ref = self._resolve_ordinal(index, registries)
            if ref.matched:
                ref.rationale = f"ordinal '{ord_word} {kind_word}'"
                return ref

        # 3. Quoted name takes precedence over fuzzy matching
        for qm in _QUOTED_NAME_RE.finditer(query):
            ref = self._resolve_name(qm.group(1))
            if ref.matched:
                ref.confidence = 0.95
                ref.rationale = f"quoted name {qm.group(1)!r}"
                return ref

        # 4. Demonstrative + kind: "that contract" / "the spreadsheet"
        m = _KIND_DEMONSTRATIVE_RE.search(query)
        if m:
            kind_word = m.group(2).lower()
            registries = self._registries_for_kind(kind_word)
            ref = self._resolve_recent_of_kind(registries)
            if ref.matched:
                ref.rationale = f"demonstrative '{m.group(0)}'"
                return ref

        # 5. Fuzzy name match — any object whose name appears in the query
        ref = self._resolve_fuzzy_in_text(query)
        if ref.matched:
            return ref

        return ResolvedReference(rationale="no reference detected")

    def resolve_all(self, query: str) -> list[ResolvedReference]:
        """
        Return *every* reference detectable in `query` (deduped).
        Used by cross-document operations that mention multiple objects.
        """
        seen: set[str] = set()
        out: list[ResolvedReference] = []

        # quoted names
        for qm in _QUOTED_NAME_RE.finditer(query):
            ref = self._resolve_name(qm.group(1))
            if ref.matched and ref.object and ref.object.name not in seen:
                seen.add(ref.object.name)
                ref.confidence = 0.95
                ref.rationale = f"quoted {qm.group(1)!r}"
                out.append(ref)

        # bare name matches
        query_low = query.lower()
        for obj in self.workspace.all_objects():
            if obj.name in seen:
                continue
            if re.search(rf"\b{re.escape(obj.name)}\b", query_low):
                seen.add(obj.name)
                out.append(ResolvedReference(
                    matched=True, object=obj, kind=obj.kind,
                    confidence=0.85, rationale=f"name match {obj.name!r}",
                ))

        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _registries_for_kind(self, kind_word: str) -> tuple[dict, ...]:
        names = _KIND_GROUPS.get(kind_word, ())
        out: list[dict] = []
        for n in names:
            if n == "spreadsheets":
                out.append(self.workspace.spreadsheets)
            elif n == "documents":
                out.append(self.workspace.documents)
            elif n == "tables":
                out.append(self.workspace.tables)
        return tuple(out)

    def _resolve_ordinal(self, index: int | None, registries: tuple[dict, ...]) -> ResolvedReference:
        if index is None or not registries:
            return ResolvedReference()
        # Merge registries in declaration order
        merged: list[WorkspaceObject] = []
        for reg in registries:
            merged.extend(reg.values())
        if not merged:
            return ResolvedReference()
        if index == -1:
            obj = merged[-1]
        elif 1 <= index <= len(merged):
            obj = merged[index - 1]
        else:
            return ResolvedReference()
        return ResolvedReference(
            matched=True, object=obj, kind=obj.kind, confidence=0.9,
        )

    def _resolve_recent_of_kind(self, registries: tuple[dict, ...]) -> ResolvedReference:
        for name in self.workspace.memory.active_objects:
            for reg in registries:
                if name in reg:
                    obj = reg[name]
                    return ResolvedReference(
                        matched=True, object=obj, kind=obj.kind, confidence=0.85,
                    )
        # Fall back to first entry
        for reg in registries:
            if reg:
                obj = next(iter(reg.values()))
                return ResolvedReference(
                    matched=True, object=obj, kind=obj.kind, confidence=0.7,
                    rationale="only object of that kind in workspace",
                )
        return ResolvedReference()

    def _resolve_name(self, candidate: str) -> ResolvedReference:
        cl = candidate.strip().lower()
        # Exact (post-slug) match
        for registry in (self.workspace.spreadsheets, self.workspace.documents, self.workspace.tables):
            for name, obj in registry.items():
                if name.lower() == cl:
                    return ResolvedReference(matched=True, object=obj, kind=obj.kind, confidence=0.95)
        # Slug-of-candidate match
        slug = re.sub(r"[^\w]+", "_", cl).strip("_")
        for registry in (self.workspace.spreadsheets, self.workspace.documents, self.workspace.tables):
            for name, obj in registry.items():
                if name == slug:
                    return ResolvedReference(matched=True, object=obj, kind=obj.kind, confidence=0.9)
        return ResolvedReference()

    def _resolve_fuzzy_in_text(self, text: str) -> ResolvedReference:
        text_low = text.lower()
        # Score by whether the object's name appears as a whole token
        best: tuple[float, WorkspaceObject | None] = (0.0, None)
        for obj in self.workspace.all_objects():
            n = obj.name.lower()
            if not n or len(n) < 2:
                continue
            if re.search(rf"\b{re.escape(n)}\b", text_low):
                # Tie-break: recently-touched wins
                bonus = 0.1 if obj.name == self.workspace.memory.most_recent_object else 0.0
                if 1.0 + bonus > best[0]:
                    best = (1.0 + bonus, obj)
        if best[1] is not None:
            return ResolvedReference(
                matched=True, object=best[1], kind=best[1].kind,
                confidence=0.8, rationale=f"name found in query: {best[1].name!r}",
            )
        return ResolvedReference()

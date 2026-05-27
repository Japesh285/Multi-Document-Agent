"""
core.workspace_memory — recent objects, results, and mutations.

Distinct from `memory.AnalysisMemory` (which records per-step results inside
a single query). WorkspaceMemory persists *across* queries within a session
and is what powers reference resolution like "those rows" or "that contract".
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class MutationRecord:
    """One entry in the mutation history."""

    timestamp:   str
    object_name: str         # workspace name of the object that was changed
    object_kind: str         # "spreadsheet" | "document" | "table"
    action:      str         # "add_column", "save_docx", "replace_text", …
    detail:      str = ""
    success:     bool = True
    error:       str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp":   self.timestamp,
            "object_name": self.object_name,
            "object_kind": self.object_kind,
            "action":      self.action,
            "detail":      self.detail,
            "success":     self.success,
            "error":       self.error,
        }


@dataclass
class ResultRecord:
    """One past query result kept for reference resolution."""

    timestamp:   str
    query:       str
    intent:      str = ""
    # Up to one named object the result is "about" (e.g. df from last data_query)
    result_obj:  Any = None              # pd.DataFrame / list / dict
    result_kind: str = ""                # "dataframe" | "value" | "report"
    summary:     str = ""                # one-line description

    def is_dataframe(self) -> bool:
        return isinstance(self.result_obj, pd.DataFrame)

    def to_dict(self) -> dict:
        d = {
            "timestamp":   self.timestamp,
            "query":       self.query,
            "intent":      self.intent,
            "result_kind": self.result_kind,
            "summary":     self.summary,
        }
        if self.is_dataframe():
            df: pd.DataFrame = self.result_obj
            d["dataframe_shape"] = list(df.shape)
            d["dataframe_columns"] = list(df.columns)
        return d


# ---------------------------------------------------------------------------
# Memory container
# ---------------------------------------------------------------------------


@dataclass
class WorkspaceMemory:
    """Recent-history buffer shared across queries within a session."""

    active_objects: list[str]              = field(default_factory=list)   # name stack (newest first)
    results:        deque                  = field(default_factory=lambda: deque(maxlen=25))
    mutations:      list[MutationRecord]   = field(default_factory=list)
    query_history:  deque                  = field(default_factory=lambda: deque(maxlen=50))

    # ------------------------------------------------------------------
    # Updates
    # ------------------------------------------------------------------

    def touch(self, object_name: str) -> None:
        """Mark an object as recently used. Newest moves to the front."""
        if not object_name:
            return
        if object_name in self.active_objects:
            self.active_objects.remove(object_name)
        self.active_objects.insert(0, object_name)
        if len(self.active_objects) > 20:
            self.active_objects = self.active_objects[:20]

    def record_query(self, query: str) -> None:
        if query:
            self.query_history.append({"timestamp": _now(), "query": query})

    def record_result(
        self,
        *,
        query: str,
        intent: str = "",
        result_obj: Any = None,
        summary: str = "",
    ) -> ResultRecord:
        kind = "value"
        if isinstance(result_obj, pd.DataFrame):
            kind = "dataframe"
        elif isinstance(result_obj, str) and len(result_obj) > 200:
            kind = "report"
        rec = ResultRecord(
            timestamp=_now(),
            query=query,
            intent=intent,
            result_obj=result_obj,
            result_kind=kind,
            summary=summary,
        )
        self.results.append(rec)
        return rec

    def record_mutation(
        self,
        *,
        object_name: str,
        object_kind: str,
        action: str,
        detail: str = "",
        success: bool = True,
        error: str = "",
    ) -> MutationRecord:
        rec = MutationRecord(
            timestamp=_now(),
            object_name=object_name,
            object_kind=object_kind,
            action=action,
            detail=detail,
            success=success,
            error=error,
        )
        self.mutations.append(rec)
        self.touch(object_name)
        return rec

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def last_result(self) -> ResultRecord | None:
        return self.results[-1] if self.results else None

    @property
    def last_dataframe_result(self) -> pd.DataFrame | None:
        for rec in reversed(self.results):
            if rec.is_dataframe():
                return rec.result_obj
        return None

    @property
    def most_recent_object(self) -> str:
        return self.active_objects[0] if self.active_objects else ""

    def recent_mutations(self, n: int = 5) -> list[MutationRecord]:
        return self.mutations[-n:]

    # ------------------------------------------------------------------
    # Serialisation (for UI / session persistence)
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "active_objects":   list(self.active_objects),
            "results":          [r.to_dict() for r in list(self.results)[-10:]],
            "mutations":        [m.to_dict() for m in self.mutations[-10:]],
            "query_history":    list(self.query_history)[-10:],
        }

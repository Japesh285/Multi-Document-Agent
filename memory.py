"""
memory.py — Structured analysis memory.

Tracks:
  - per-step pandas results     (StepResult)
  - web search findings         (WebFinding)
  - Excel write operations      (ExcelUpdate)

Produces compact context strings for LLM prompts — never dumps raw DataFrames.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime


# ---------------------------------------------------------------------------
# Step result (pandas execution)
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    step_id:     str
    description: str
    code:        str
    output:      str
    error:       str | None
    elapsed:     float
    chart_path:  str | None = None

    def to_dict(self) -> dict:
        return {
            "step_id":     self.step_id,
            "description": self.description,
            "code":        self.code,
            "output":      self.output,
            "error":       self.error,
            "elapsed":     self.elapsed,
            "chart_path":  self.chart_path,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StepResult":
        return cls(**d)

    def summary(self, max_chars: int = 600) -> str:
        if self.error:
            return f"[ERROR] {self.error[:300]}"
        text = self.output.strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…(truncated)"
        return text

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# Web finding (MCP / search result)
# ---------------------------------------------------------------------------

@dataclass
class WebFinding:
    search_query: str
    summary:      str
    raw_count:    int = 0
    cached:       bool = False
    timestamp:    str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return {
            "search_query": self.search_query,
            "summary":      self.summary,
            "raw_count":    self.raw_count,
            "cached":       self.cached,
            "timestamp":    self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WebFinding":
        return cls(**d)

    def context_line(self, max_chars: int = 400) -> str:
        summary = self.summary.strip()[:max_chars]
        return f"[web: {self.search_query[:50]}]\n{summary}"


# ---------------------------------------------------------------------------
# Excel update record
# ---------------------------------------------------------------------------

@dataclass
class ExcelUpdate:
    action:        str         # "add_column" | "update_rows"
    column:        str
    rows_affected: int
    timestamp:     str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    detail:        str = ""
    success:       bool = True
    error:         str | None = None

    def to_dict(self) -> dict:
        return {
            "action":        self.action,
            "column":        self.column,
            "rows_affected": self.rows_affected,
            "timestamp":     self.timestamp,
            "detail":        self.detail,
            "success":       self.success,
            "error":         self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExcelUpdate":
        return cls(**d)


# ---------------------------------------------------------------------------
# Analysis memory  (session-level accumulator)
# ---------------------------------------------------------------------------

@dataclass
class AnalysisMemory:
    query:         str
    results:       list[StepResult]  = field(default_factory=list)
    web_findings:  list[WebFinding]  = field(default_factory=list)
    excel_updates: list[ExcelUpdate] = field(default_factory=list)

    # ── mutation ───────────────────────────────────────────────────────────

    def add(self, r: StepResult) -> None:
        self.results.append(r)

    def add_web(self, w: WebFinding) -> None:
        self.web_findings.append(w)

    def add_excel(self, u: ExcelUpdate) -> None:
        self.excel_updates.append(u)

    # ── queries ────────────────────────────────────────────────────────────

    def get(self, step_id: str) -> StepResult | None:
        return next((r for r in self.results if r.step_id == step_id), None)

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed_ids(self) -> list[str]:
        return [r.step_id for r in self.results if not r.ok]

    @property
    def chart_paths(self) -> list[str]:
        return [r.chart_path for r in self.results if r.chart_path]

    # ── context strings for LLM prompts ───────────────────────────────────

    def to_context(self, max_chars_per_step: int = 600) -> str:
        """Full context for synthesizer — includes web and excel sections."""
        parts: list[str] = []

        if self.results:
            parts.append("## Analysis Steps")
            for r in self.results:
                parts.append(f"### [{r.step_id}] {r.description}\n{r.summary(max_chars_per_step)}")

        if self.web_findings:
            parts.append("## Web Research Findings")
            for w in self.web_findings:
                parts.append(w.context_line(max_chars_per_step))

        if self.excel_updates:
            parts.append("## Excel Modifications")
            for u in self.excel_updates:
                status = "OK" if u.success else "FAILED"
                parts.append(f"  [{status}] {u.action}: column '{u.column}' ({u.rows_affected} rows)")

        return "\n\n".join(parts) if parts else "(no results computed yet)"

    def step_context_for_prompt(self, max_chars_per_step: int = 300) -> str:
        """Condensed view for per-step analyzer prompts."""
        if not self.results:
            return "(none)"
        lines = []
        for r in self.results:
            status  = "OK" if r.ok else "FAILED"
            snippet = r.summary(max_chars_per_step).splitlines()[0]
            lines.append(f"  [{r.step_id}] ({status}) {snippet}")
        return "\n".join(lines)

    # ── serialisation ──────────────────────────────────────────────────────

    def to_dicts(self) -> list[dict]:
        return [r.to_dict() for r in self.results]

    @classmethod
    def from_dicts(cls, query: str, dicts: list[dict]) -> "AnalysisMemory":
        return cls(query=query, results=[StepResult.from_dict(d) for d in dicts])

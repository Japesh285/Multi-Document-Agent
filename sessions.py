"""
sessions.py — SQLite-backed session and workspace persistence.

Stores conversations, reports, charts, mutations, and dataframe references
so the desktop app can restore workspaces across launches.

Schema
======
sessions          → one row per workspace (one Excel file = one session)
messages          → chat turns (user / assistant / system / tool)
reports           → synthesized reports (markdown + html paths)
chart_history     → every chart generated, linked to a message
mutations         → Excel write history (column add, row update, backups)
recent_files      → most-recently-opened spreadsheets
settings          → key/value app settings

All paths are stored as strings; pathlib.Path is used at write time.
"""

from __future__ import annotations
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from utils import get_logger

log = get_logger("sessions")

DB_PATH = Path("output") / "spreadsheet_agent.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    file_name       TEXT NOT NULL,
    rows            INTEGER DEFAULT 0,
    columns         INTEGER DEFAULT 0,
    domain          TEXT,
    domain_confidence REAL,
    schema_json     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    role            TEXT NOT NULL,         -- user | assistant | system | tool
    content         TEXT NOT NULL,
    intent          TEXT,
    confidence      REAL,
    elapsed         REAL,
    charts_json     TEXT,                  -- JSON array of chart paths
    web_results_json TEXT,
    excel_updates_json TEXT,
    step_results_json TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS reports (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    title           TEXT NOT NULL,
    query           TEXT NOT NULL,
    markdown        TEXT NOT NULL,
    html_path       TEXT,
    md_path         TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reports_session ON reports(session_id, created_at);

CREATE TABLE IF NOT EXISTS chart_history (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    message_id      TEXT,
    path            TEXT NOT NULL,
    title           TEXT,
    step_id         TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_charts_session ON chart_history(session_id, created_at);

CREATE TABLE IF NOT EXISTS mutations (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    action          TEXT NOT NULL,
    column          TEXT,
    rows_affected   INTEGER DEFAULT 0,
    detail          TEXT,
    backup_path     TEXT,
    success         INTEGER NOT NULL,
    error           TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS recent_files (
    file_path       TEXT PRIMARY KEY,
    file_name       TEXT NOT NULL,
    last_opened     TEXT NOT NULL,
    open_count      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they don't exist."""
    with _conn() as c:
        c.executescript(_SCHEMA)
    log.info("sessions: db initialised at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SessionRecord:
    id:                str
    name:              str
    file_path:         str
    file_name:         str
    rows:              int = 0
    columns:           int = 0
    domain:            str = ""
    domain_confidence: float = 0.0
    schema_json:       str = ""
    created_at:        str = ""
    updated_at:        str = ""
    archived:          int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        # Inflate schema_json for the UI
        try:
            d["schema"] = json.loads(self.schema_json) if self.schema_json else None
        except Exception:
            d["schema"] = None
        return d


@dataclass
class MessageRecord:
    id:                  str
    session_id:          str
    role:                str
    content:             str
    intent:              str = ""
    confidence:          float = 0.0
    elapsed:             float = 0.0
    charts:              list[str]  = field(default_factory=list)
    web_results:         list[dict] = field(default_factory=list)
    excel_updates:       list[dict] = field(default_factory=list)
    step_results:        list[dict] = field(default_factory=list)
    created_at:          str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "intent": self.intent,
            "confidence": self.confidence,
            "elapsed": self.elapsed,
            "charts": self.charts,
            "web_results": self.web_results,
            "excel_updates": self.excel_updates,
            "step_results": self.step_results,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(
    file_path: str,
    name: str | None = None,
    *,
    rows: int = 0,
    columns: int = 0,
    domain: str = "",
    domain_confidence: float = 0.0,
    schema_dict: dict | None = None,
) -> SessionRecord:
    sid = uuid.uuid4().hex[:16]
    fp  = Path(file_path)
    now = _now()
    rec = SessionRecord(
        id=sid,
        name=name or fp.stem,
        file_path=str(fp),
        file_name=fp.name,
        rows=rows,
        columns=columns,
        domain=domain,
        domain_confidence=domain_confidence,
        schema_json=json.dumps(schema_dict or {}, default=str),
        created_at=now,
        updated_at=now,
    )
    with _conn() as c:
        c.execute("""
            INSERT INTO sessions (id, name, file_path, file_name, rows, columns,
                                  domain, domain_confidence, schema_json,
                                  created_at, updated_at, archived)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (rec.id, rec.name, rec.file_path, rec.file_name, rec.rows, rec.columns,
              rec.domain, rec.domain_confidence, rec.schema_json,
              rec.created_at, rec.updated_at))
    touch_recent(file_path)
    log.info("sessions: created %s for %s", sid, fp.name)
    return rec


def get_session(session_id: str) -> SessionRecord | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return _row_to_session(row) if row else None


def list_sessions(include_archived: bool = False) -> list[SessionRecord]:
    sql = "SELECT * FROM sessions"
    if not include_archived:
        sql += " WHERE archived = 0"
    sql += " ORDER BY updated_at DESC"
    with _conn() as c:
        rows = c.execute(sql).fetchall()
    return [_row_to_session(r) for r in rows]


def touch_session(session_id: str) -> None:
    with _conn() as c:
        c.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id))


def delete_session(session_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def archive_session(session_id: str, archived: bool = True) -> None:
    with _conn() as c:
        c.execute("UPDATE sessions SET archived = ?, updated_at = ? WHERE id = ?",
                  (1 if archived else 0, _now(), session_id))


def rename_session(session_id: str, name: str) -> None:
    with _conn() as c:
        c.execute("UPDATE sessions SET name = ?, updated_at = ? WHERE id = ?",
                  (name, _now(), session_id))


def update_session_schema(session_id: str, schema_dict: dict) -> None:
    """Replace the stored schema_json (and row/column counts) for a session."""
    payload = json.dumps(schema_dict or {}, default=str)
    shape   = schema_dict.get("shape") or {}
    rows    = int(shape.get("rows", 0) or 0)
    cols    = int(shape.get("columns", 0) or 0)
    domain  = str(schema_dict.get("domain") or "")
    dconf   = float(schema_dict.get("domain_confidence") or 0.0)
    with _conn() as c:
        c.execute("""
            UPDATE sessions
               SET schema_json = ?, rows = ?, columns = ?,
                   domain = ?, domain_confidence = ?, updated_at = ?
             WHERE id = ?
        """, (payload, rows, cols, domain, dconf, _now(), session_id))


def _row_to_session(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        id=row["id"], name=row["name"],
        file_path=row["file_path"], file_name=row["file_name"],
        rows=row["rows"] or 0, columns=row["columns"] or 0,
        domain=row["domain"] or "", domain_confidence=row["domain_confidence"] or 0.0,
        schema_json=row["schema_json"] or "",
        created_at=row["created_at"], updated_at=row["updated_at"],
        archived=row["archived"] or 0,
    )


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def add_message(
    session_id: str,
    role: str,
    content: str,
    *,
    intent: str = "",
    confidence: float = 0.0,
    elapsed: float = 0.0,
    charts: Iterable[str] = (),
    web_results: Iterable[dict] = (),
    excel_updates: Iterable[dict] = (),
    step_results: Iterable[dict] = (),
) -> MessageRecord:
    mid = uuid.uuid4().hex[:16]
    rec = MessageRecord(
        id=mid, session_id=session_id, role=role, content=content,
        intent=intent, confidence=confidence, elapsed=elapsed,
        charts=list(charts), web_results=list(web_results),
        excel_updates=list(excel_updates), step_results=list(step_results),
        created_at=_now(),
    )
    with _conn() as c:
        c.execute("""
            INSERT INTO messages (id, session_id, role, content, intent, confidence,
                                  elapsed, charts_json, web_results_json,
                                  excel_updates_json, step_results_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (mid, session_id, role, content, intent, confidence, elapsed,
              json.dumps(rec.charts), json.dumps(rec.web_results, default=str),
              json.dumps(rec.excel_updates, default=str),
              json.dumps(rec.step_results, default=str), rec.created_at))
    touch_session(session_id)
    return rec


def list_messages(session_id: str, limit: int = 500) -> list[MessageRecord]:
    with _conn() as c:
        rows = c.execute("""
            SELECT * FROM messages WHERE session_id = ?
            ORDER BY created_at ASC LIMIT ?
        """, (session_id, limit)).fetchall()
    return [_row_to_message(r) for r in rows]


def _row_to_message(row: sqlite3.Row) -> MessageRecord:
    def _j(s: str | None, default):
        if not s:
            return default
        try:
            return json.loads(s)
        except Exception:
            return default

    return MessageRecord(
        id=row["id"], session_id=row["session_id"],
        role=row["role"], content=row["content"],
        intent=row["intent"] or "", confidence=row["confidence"] or 0.0,
        elapsed=row["elapsed"] or 0.0,
        charts=_j(row["charts_json"], []),
        web_results=_j(row["web_results_json"], []),
        excel_updates=_j(row["excel_updates_json"], []),
        step_results=_j(row["step_results_json"], []),
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Reports / charts / mutations
# ---------------------------------------------------------------------------

def add_report(session_id: str, title: str, query: str, markdown: str,
               md_path: str = "", html_path: str = "") -> str:
    rid = uuid.uuid4().hex[:16]
    with _conn() as c:
        c.execute("""
            INSERT INTO reports (id, session_id, title, query, markdown,
                                 html_path, md_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (rid, session_id, title, query, markdown, html_path, md_path, _now()))
    touch_session(session_id)
    return rid


def list_reports(session_id: str | None = None) -> list[dict]:
    with _conn() as c:
        if session_id:
            rows = c.execute("""
                SELECT * FROM reports WHERE session_id = ? ORDER BY created_at DESC
            """, (session_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM reports ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def add_chart(session_id: str, path: str, *, title: str = "",
              step_id: str = "", message_id: str = "") -> str:
    cid = uuid.uuid4().hex[:16]
    with _conn() as c:
        c.execute("""
            INSERT INTO chart_history (id, session_id, message_id, path,
                                       title, step_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (cid, session_id, message_id, path, title, step_id, _now()))
    return cid


def list_charts(session_id: str | None = None, limit: int = 200) -> list[dict]:
    with _conn() as c:
        if session_id:
            rows = c.execute("""
                SELECT * FROM chart_history WHERE session_id = ?
                ORDER BY created_at DESC LIMIT ?
            """, (session_id, limit)).fetchall()
        else:
            rows = c.execute("""
                SELECT * FROM chart_history ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def add_mutation(session_id: str, action: str, column: str, rows_affected: int,
                 *, success: bool = True, detail: str = "", backup_path: str = "",
                 error: str = "") -> str:
    mid = uuid.uuid4().hex[:16]
    with _conn() as c:
        c.execute("""
            INSERT INTO mutations (id, session_id, action, column, rows_affected,
                                   detail, backup_path, success, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (mid, session_id, action, column, rows_affected, detail,
              backup_path, 1 if success else 0, error, _now()))
    touch_session(session_id)
    return mid


def list_mutations(session_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("""
            SELECT * FROM mutations WHERE session_id = ? ORDER BY created_at DESC
        """, (session_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Recent files
# ---------------------------------------------------------------------------

def touch_recent(file_path: str) -> None:
    fp = Path(file_path)
    with _conn() as c:
        c.execute("""
            INSERT INTO recent_files (file_path, file_name, last_opened, open_count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(file_path) DO UPDATE SET
                last_opened = excluded.last_opened,
                open_count  = open_count + 1
        """, (str(fp), fp.name, _now()))


def list_recent_files(limit: int = 20) -> list[dict]:
    with _conn() as c:
        rows = c.execute("""
            SELECT * FROM recent_files ORDER BY last_opened DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS: dict[str, Any] = {
    "ollama.model":       "qwen2.5-coder:14b",
    "ollama.url":         "http://localhost:11434",
    "ollama.context":     8192,
    "ollama.temperature": 0.05,
    "ui.theme":           "dark",
    "ui.compact":         False,
    "backup.enabled":     True,
    "backup.retention":   30,
    "report.default_format": "markdown",
    "mcp.web_search_enabled": True,
    "mcp.cache_ttl_hours": 24,
    "memory.max_steps":   8,
}


def get_setting(key: str, default: Any = None) -> Any:
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row:
        try:
            return json.loads(row["value"])
        except Exception:
            return row["value"]
    return DEFAULT_SETTINGS.get(key, default)


def set_setting(key: str, value: Any) -> None:
    with _conn() as c:
        c.execute("""
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """, (key, json.dumps(value), _now()))


def get_all_settings() -> dict[str, Any]:
    """Return merged settings = defaults overridden by user-saved values."""
    merged = dict(DEFAULT_SETTINGS)
    with _conn() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
    for r in rows:
        try:
            merged[r["key"]] = json.loads(r["value"])
        except Exception:
            merged[r["key"]] = r["value"]
    return merged


# Initialise DB on import
init_db()

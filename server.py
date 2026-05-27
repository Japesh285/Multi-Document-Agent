"""
server.py — Desktop-facing FastAPI server.

Wraps the existing analysis engine (app.process_query, schema.load_dataframe, …)
in a session-aware HTTP layer. Designed to be auto-launched by the Tauri
desktop shell as a child process.

Endpoints
=========
GET    /health                       liveness + Ollama + active session
POST   /sessions                     create session from uploaded file path
GET    /sessions                     list all sessions
GET    /sessions/{id}                fetch session detail
POST   /sessions/{id}/activate       set as the active workspace
DELETE /sessions/{id}                delete session
PATCH  /sessions/{id}                rename / archive
GET    /sessions/{id}/messages       chat history
GET    /sessions/{id}/charts         chart history
GET    /sessions/{id}/mutations      Excel mutation history
GET    /sessions/{id}/reports        reports for session
POST   /upload                       upload xlsx (multipart) → returns path
POST   /query                        chat / analysis turn
POST   /stream                       streaming chat (SSE)
POST   /report                       generate a full report
POST   /verify                       date verification pipeline
POST   /mutate                       Excel column mutation
GET    /charts                       global chart history
GET    /charts/file                  serve a chart PNG
GET    /reports                      global report list
GET    /reports/{id}                 fetch report markdown
GET    /reports/{id}/export.{fmt}    export pdf|html|xlsx
GET    /settings                     all settings (merged with defaults)
PUT    /settings                     update settings (partial)
GET    /ollama/status                check Ollama reachable + model loaded
GET    /ollama/models                list available local models

CORS is permissive (localhost only) because the Tauri shell loads the React
UI from http://localhost:5173 in dev and from tauri://localhost in prod.
"""

from __future__ import annotations
import asyncio
import io
import json
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator

import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException, UploadFile, File, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel

import app as agent_app
import sessions
from loaders import SUPPORTED_EXTENSIONS, LoaderError, load_any
from loaders.docx import DocumentLoadError, load_docx
from schema import build_schema, SchemaInfo
from utils import get_logger

# Workspace extensions = spreadsheet extensions + .docx
WORKSPACE_EXTENSIONS = SUPPORTED_EXTENSIONS | {".docx"}

log = get_logger("server")

UPLOAD_DIR  = Path("output") / "uploads"
REPORTS_DIR = Path("output") / "reports"
CHARTS_DIR  = Path("output") / "charts"
for d in (UPLOAD_DIR, REPORTS_DIR, CHARTS_DIR):
    d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Lifespan — initial setup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    log.info("server: starting up")
    sessions.init_db()
    yield
    log.info("server: shutting down")


app = FastAPI(
    title="Autonomous Spreadsheet Agent",
    version="7.0",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",       # Vite dev
        "http://127.0.0.1:5173",
        "tauri://localhost",           # Tauri production
        "https://tauri.localhost",
        "*",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Active session management — replaces ad-hoc globals in app.py
# ---------------------------------------------------------------------------

_active_session_id: str | None = None


def _activate_session(
    session_id: str,
    *,
    sheet: str | None = None,
    force: bool = False,
) -> sessions.SessionRecord:
    """Load the given session's spreadsheet/document into the workspace."""
    global _active_session_id

    rec = sessions.get_session(session_id)
    if rec is None:
        raise HTTPException(404, f"Session {session_id} not found")

    file_path = Path(rec.file_path)
    if not file_path.exists():
        raise HTTPException(
            400,
            f"Source file no longer exists: {file_path}. Re-upload the file.",
        )

    ws = agent_app.get_workspace()
    same_session = _active_session_id == session_id

    # Skip reload if same session AND sheet matches (or no sheet was requested)
    if not force and same_session and not ws.is_empty():
        active_ss = ws.active_spreadsheet
        if active_ss is None or sheet is None or sheet == active_ss.active_sheet:
            return rec

    # Always start the session with a fresh workspace
    agent_app._reset_workspace()
    ws = agent_app.get_workspace()

    try:
        suffix = file_path.suffix.lower()
        if suffix == ".docx":
            ws.register_document_from_path(str(file_path))
        else:
            ws.register_spreadsheet_from_path(str(file_path), sheet=sheet)
    except (LoaderError, DocumentLoadError) as exc:
        raise HTTPException(400, f"Failed to load file: {exc}")

    _active_session_id = session_id
    sessions.touch_session(session_id)

    log.info("server: activated session %s (%s)", session_id, file_path.name)
    return rec


def _require_active() -> str:
    if _active_session_id is None:
        raise HTTPException(400, "No active session. Upload a spreadsheet first.")
    return _active_session_id


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    file_path: str
    name:      str | None = None


class QueryRequest(BaseModel):
    query:         str
    session_id:    str | None = None
    output_format: str        = "markdown"


class MutateRequest(BaseModel):
    instruction:   str
    session_id:    str | None = None


class VerifyRequest(BaseModel):
    query:         str        = "verify the dates"
    session_id:    str | None = None


class RenameRequest(BaseModel):
    name:     str | None = None
    archived: bool | None = None


class SettingsUpdate(BaseModel):
    updates: dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _schema_to_dict(schema: SchemaInfo) -> dict:
    return {
        "file_path":          schema.file_path,
        "shape":              {"rows": schema.shape[0], "columns": schema.shape[1]},
        "columns":            schema.columns,
        "dtypes":             schema.dtypes,
        "null_counts":        schema.null_counts,
        "semantics":          schema.semantics,
        "unique_sports":      schema.unique_sports,
        "unique_results":     schema.unique_results,
        "domain":             schema.domain,
        "domain_confidence":  schema.domain_confidence,
        # Ingestion metadata
        "file_type":          schema.file_type,
        "sheets":             schema.sheets,
        "active_sheet":       schema.active_sheet,
        "encoding":           schema.encoding,
        "delimiter":          schema.delimiter,
        "workbook_metadata":  schema.workbook_metadata,
        "ingestion_warnings": schema.ingestion_warnings,
    }


def _agent_output_to_dict(out, query: str) -> dict:
    return {
        "query":        out.query or query,
        "intent":       out.intent,
        "confidence":   out.confidence,
        "report":       out.report,
        "charts":       out.charts,
        "excel_updates": out.excel_updates,
        "web_results":  out.web_results,
        "step_results": out.step_results,
        "elapsed":      out.elapsed,
        "success":      out.success,
        "error":        out.error,
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    ollama_ok    = False
    ollama_error = ""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get("http://localhost:11434/api/tags")
            ollama_ok = r.status_code == 200
    except Exception as exc:
        ollama_error = str(exc)

    ws = agent_app.get_workspace()
    return {
        "status":    "ok",
        "version":   app.version,
        "ollama":    {"reachable": ollama_ok, "error": ollama_error},
        "session":   _active_session_id,
        "workspace": {
            "spreadsheets": len(ws.spreadsheets),
            "documents":    len(ws.documents),
            "tables":       len(ws.tables),
        },
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@app.post("/sessions")
async def create_session(req: CreateSessionRequest):
    fp = Path(req.file_path)
    if not fp.exists():
        raise HTTPException(400, f"File not found: {fp}")
    suffix = fp.suffix.lower()
    if suffix not in WORKSPACE_EXTENSIONS:
        raise HTTPException(
            400,
            f"Unsupported file type '{suffix}'. "
            f"Supported: {sorted(WORKSPACE_EXTENSIONS)}",
        )

    try:
        if suffix == ".docx":
            doc_obj = load_docx(str(fp))
            schema_dict = {
                "file_path":  str(fp),
                "file_type":  "docx",
                "shape":      {"rows": len(doc_obj.paragraphs), "columns": 0},
                "columns":    [],
                "domain":     "document",
                "domain_confidence": 1.0,
                "metadata":   doc_obj.metadata,
            }
            rows, cols = len(doc_obj.paragraphs), 0
            domain, dconf = "document", 1.0
        else:
            loaded = load_any(str(fp))
            schema = build_schema(loaded.df, str(fp), loaded=loaded)
            schema_dict = _schema_to_dict(schema)
            rows, cols = schema.shape
            domain, dconf = schema.domain, schema.domain_confidence
    except (LoaderError, DocumentLoadError) as exc:
        raise HTTPException(400, f"Failed to load file: {exc}")

    rec = sessions.create_session(
        file_path=str(fp),
        name=req.name,
        rows=rows,
        columns=cols,
        domain=domain,
        domain_confidence=dconf,
        schema_dict=schema_dict,
    )
    _activate_session(rec.id)
    return {"session": rec.to_dict(), "schema": schema_dict}


@app.get("/sessions")
async def get_sessions(include_archived: bool = False):
    return {"sessions": [s.to_dict() for s in sessions.list_sessions(include_archived)]}


@app.get("/sessions/{session_id}")
async def get_session_detail(session_id: str):
    rec = sessions.get_session(session_id)
    if rec is None:
        raise HTTPException(404, "Session not found")
    return {"session": rec.to_dict()}


@app.post("/sessions/{session_id}/activate")
async def activate_session(session_id: str):
    rec = _activate_session(session_id)
    ws = agent_app.get_workspace()
    active = ws.active_spreadsheet
    schema_payload = _schema_to_dict(active.schema) if (active and active.schema) else {}
    return {
        "session":   rec.to_dict(),
        "schema":    schema_payload,
        "workspace": ws.inventory_dict(),
    }


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    global _active_session_id
    sessions.delete_session(session_id)
    if _active_session_id == session_id:
        _active_session_id = None
        agent_app._reset_workspace()
    return {"ok": True}


@app.get("/sessions/{session_id}/sheets")
async def list_sheets(session_id: str):
    """Return the sheet inventory for a session's workbook (no DataFrame parse)."""
    rec = sessions.get_session(session_id)
    if rec is None:
        raise HTTPException(404, "Session not found")
    schema_dict = rec.to_dict().get("schema") or {}
    return {
        "session_id":   session_id,
        "file_type":    schema_dict.get("file_type", ""),
        "active_sheet": schema_dict.get("active_sheet", ""),
        "sheets":       schema_dict.get("sheets", []),
    }


@app.post("/sessions/{session_id}/sheets/{sheet_name}/activate")
async def activate_sheet(session_id: str, sheet_name: str):
    """Switch the active sheet of a session and rebuild its schema."""
    _activate_session(session_id, sheet=sheet_name, force=True)
    ws = agent_app.get_workspace()
    active = ws.active_spreadsheet
    schema_dict = _schema_to_dict(active.schema) if (active and active.schema) else {}
    sessions.update_session_schema(session_id, schema_dict)
    return {"schema": schema_dict, "active_sheet": sheet_name}


@app.patch("/sessions/{session_id}")
async def patch_session(session_id: str, req: RenameRequest):
    if req.name is not None:
        sessions.rename_session(session_id, req.name)
    if req.archived is not None:
        sessions.archive_session(session_id, req.archived)
    rec = sessions.get_session(session_id)
    return {"session": rec.to_dict() if rec else None}


@app.get("/sessions/{session_id}/messages")
async def session_messages(session_id: str, limit: int = 500):
    msgs = sessions.list_messages(session_id, limit=limit)
    return {"messages": [m.to_dict() for m in msgs]}


@app.get("/sessions/{session_id}/charts")
async def session_charts(session_id: str):
    return {"charts": sessions.list_charts(session_id)}


@app.get("/sessions/{session_id}/mutations")
async def session_mutations(session_id: str):
    return {"mutations": sessions.list_mutations(session_id)}


@app.get("/sessions/{session_id}/reports")
async def session_reports(session_id: str):
    return {"reports": sessions.list_reports(session_id)}


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """
    Accept a spreadsheet upload (.xlsx/.xls/.xlsm/.csv/.tsv), save to
    output/uploads/, create a session, and activate it.
    """
    if not file.filename:
        raise HTTPException(400, "Empty filename")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in WORKSPACE_EXTENSIONS:
        raise HTTPException(
            400,
            f"Unsupported file type '{suffix}'. "
            f"Supported: {sorted(WORKSPACE_EXTENSIONS)}",
        )

    dest = UPLOAD_DIR / file.filename
    if dest.exists():
        ts = int(time.time())
        dest = UPLOAD_DIR / f"{dest.stem}_{ts}{dest.suffix}"

    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    log.info("server: upload saved %s (%.1f KB)", dest, dest.stat().st_size / 1024)

    try:
        if suffix == ".docx":
            doc_obj = load_docx(str(dest))
            schema_dict = {
                "file_path": str(dest),
                "file_type": "docx",
                "shape":     {"rows": len(doc_obj.paragraphs), "columns": 0},
                "columns":   [],
                "domain":    "document",
                "domain_confidence": 1.0,
                "metadata":  doc_obj.metadata,
                "sections":  [s["name"] for s in doc_obj.sections],
            }
            rows, cols = len(doc_obj.paragraphs), 0
            domain, dconf = "document", 1.0
            metadata_payload = {
                "file_type":  "docx",
                "paragraphs": len(doc_obj.paragraphs),
                "tables":     len(doc_obj.table_names),
                "sections":   [s["name"] for s in doc_obj.sections],
            }
        else:
            loaded = load_any(str(dest))
            schema = build_schema(loaded.df, str(dest), loaded=loaded)
            schema_dict = _schema_to_dict(schema)
            rows, cols = schema.shape
            domain, dconf = schema.domain, schema.domain_confidence
            metadata_payload = loaded.to_metadata_dict()
    except (LoaderError, DocumentLoadError) as exc:
        log.warning("server: load failed for %s — %s", dest.name, exc)
        raise HTTPException(400, f"Failed to parse file: {exc}")
    except Exception as exc:
        log.exception("server: unexpected load error")
        raise HTTPException(400, f"Failed to parse file: {exc}")

    rec = sessions.create_session(
        file_path=str(dest),
        name=dest.stem,
        rows=rows,
        columns=cols,
        domain=domain,
        domain_confidence=dconf,
        schema_dict=schema_dict,
    )
    _activate_session(rec.id)
    return {
        "session":   rec.to_dict(),
        "schema":    schema_dict,
        "file_path": str(dest),
        "metadata":  metadata_payload,
    }


# ---------------------------------------------------------------------------
# Workspace inspection / multi-object operations
# ---------------------------------------------------------------------------

@app.get("/workspace")
async def workspace_inventory():
    """Full inventory of the currently-active workspace."""
    return agent_app.get_workspace().inventory_dict()


@app.post("/workspace/add")
async def workspace_add(file: UploadFile = File(...)):
    """
    Add another spreadsheet or document to the *currently-active* workspace
    without creating a new session. Returns the updated inventory.
    """
    if _active_session_id is None:
        raise HTTPException(400, "No active session — upload via /upload first.")
    if not file.filename:
        raise HTTPException(400, "Empty filename")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in WORKSPACE_EXTENSIONS:
        raise HTTPException(
            400,
            f"Unsupported file type '{suffix}'. Supported: {sorted(WORKSPACE_EXTENSIONS)}",
        )

    dest = UPLOAD_DIR / file.filename
    if dest.exists():
        ts = int(time.time())
        dest = UPLOAD_DIR / f"{dest.stem}_{ts}{dest.suffix}"
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    ws = agent_app.get_workspace()
    try:
        if suffix == ".docx":
            obj = ws.register_document_from_path(str(dest))
            kind = "document"
        else:
            obj = ws.register_spreadsheet_from_path(str(dest))
            kind = "spreadsheet"
    except (LoaderError, DocumentLoadError) as exc:
        raise HTTPException(400, f"Failed to parse {dest.name}: {exc}")

    log.info("server: workspace.add %s:%s", kind, obj.name)
    return {
        "added":     {"kind": kind, "name": obj.name, "path": str(dest)},
        "workspace": ws.inventory_dict(),
    }


@app.post("/workspace/activate")
async def workspace_activate(name: str = Body(..., embed=True)):
    """Mark an existing workspace object as the active one (touches it in memory)."""
    ws = agent_app.get_workspace()
    obj = ws.get(name)
    if obj is None:
        raise HTTPException(404, f"No workspace object named {name!r}")
    ws.memory.touch(name)
    return {"active": name, "kind": obj.kind, "workspace": ws.inventory_dict()}


@app.delete("/workspace/objects/{name}")
async def workspace_remove(name: str):
    ws = agent_app.get_workspace()
    if not ws.remove(name):
        raise HTTPException(404, f"No workspace object named {name!r}")
    return {"removed": name, "workspace": ws.inventory_dict()}


# ---------------------------------------------------------------------------
# Recent files
# ---------------------------------------------------------------------------

@app.get("/recent")
async def recent_files():
    return {"files": sessions.list_recent_files()}


# ---------------------------------------------------------------------------
# Query / report
# ---------------------------------------------------------------------------

def _persist_result(session_id: str, query: str, out_dict: dict) -> str:
    """Save assistant turn into messages + chart_history + mutations."""
    mid = sessions.add_message(
        session_id, role="user", content=query,
    ).id
    sessions.add_message(
        session_id, role="assistant",
        content=out_dict.get("report", ""),
        intent=out_dict.get("intent", ""),
        confidence=out_dict.get("confidence", 0.0),
        elapsed=out_dict.get("elapsed", 0.0),
        charts=out_dict.get("charts", []),
        web_results=out_dict.get("web_results", []),
        excel_updates=out_dict.get("excel_updates", []),
        step_results=out_dict.get("step_results", []),
    )
    for c in out_dict.get("charts", []):
        sessions.add_chart(session_id, c, message_id=mid)
    for upd in out_dict.get("excel_updates", []):
        sessions.add_mutation(
            session_id,
            action=upd.get("action", "update"),
            column=upd.get("column", ""),
            rows_affected=int(upd.get("rows_affected", 0) or 0),
            success=bool(upd.get("success", True)),
            detail=upd.get("detail", ""),
            backup_path=upd.get("backup_path", ""),
            error=upd.get("error", "") or "",
        )
    return mid


@app.post("/query")
async def query(req: QueryRequest):
    if req.session_id:
        _activate_session(req.session_id)
    sid = _require_active()

    if not req.query.strip():
        raise HTTPException(400, "Empty query")

    try:
        out = await asyncio.to_thread(
            agent_app.process_query, req.query, req.output_format
        )
    except ConnectionError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        log.exception("query failed")
        raise HTTPException(500, str(exc))

    out_dict = _agent_output_to_dict(out, req.query)
    _persist_result(sid, req.query, out_dict)
    return out_dict


@app.post("/report")
async def report(req: QueryRequest):
    if req.session_id:
        _activate_session(req.session_id)
    sid = _require_active()

    out = await asyncio.to_thread(
        agent_app.handle_report, req.query, req.output_format
    )
    out.intent     = "report_generation"
    out.elapsed    = 0.0
    out_dict       = _agent_output_to_dict(out, req.query)

    rid = sessions.add_report(
        session_id=sid,
        title=_extract_title(out.report) or req.query[:60],
        query=req.query,
        markdown=out.report or "",
    )
    out_dict["report_id"] = rid
    _persist_result(sid, req.query, out_dict)
    return out_dict


@app.post("/verify")
async def verify(req: VerifyRequest):
    if req.session_id:
        _activate_session(req.session_id)
    sid = _require_active()
    out = await asyncio.to_thread(agent_app.handle_verification, req.query)
    out_dict = _agent_output_to_dict(out, req.query)
    out_dict["intent"] = "verification"
    _persist_result(sid, req.query, out_dict)
    return out_dict


@app.post("/mutate")
async def mutate(req: MutateRequest):
    if req.session_id:
        _activate_session(req.session_id)
    sid = _require_active()
    out = await asyncio.to_thread(
        agent_app.handle_excel_modification, req.instruction
    )
    out_dict = _agent_output_to_dict(out, req.instruction)
    _persist_result(sid, req.instruction, out_dict)
    return out_dict


def _extract_title(md: str) -> str:
    for line in md.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


# ---------------------------------------------------------------------------
# Streaming chat — token-by-token SSE
# ---------------------------------------------------------------------------

@app.post("/stream")
async def stream_query(req: QueryRequest):
    """
    Server-Sent Events stream. Each event is a JSON line:
      {"type":"status",  "data":"..."}
      {"type":"token",   "data":"..."}
      {"type":"chart",   "data":"path/to.png"}
      {"type":"step",    "data":{...step_result...}}
      {"type":"final",   "data":{full AgentOutput}}
      {"type":"error",   "data":"..."}
    """
    if req.session_id:
        _activate_session(req.session_id)
    sid = _require_active()

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(event_type: str, data: Any) -> None:
        # called from worker thread
        loop.call_soon_threadsafe(
            queue.put_nowait,
            f"data: {json.dumps({'type': event_type, 'data': data}, default=str)}\n\n",
        )

    async def worker():
        try:
            emit("status", "Processing query…")
            out = await asyncio.to_thread(
                agent_app.process_query, req.query, req.output_format
            )
            out_dict = _agent_output_to_dict(out, req.query)
            _persist_result(sid, req.query, out_dict)
            emit("final", out_dict)
        except Exception as exc:
            log.exception("stream worker error")
            emit("error", str(exc))
        finally:
            await queue.put(None)   # sentinel

    asyncio.create_task(worker())

    async def gen():
        while True:
            chunk = await queue.get()
            if chunk is None:
                yield "event: done\ndata: {}\n\n"
                break
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

@app.get("/charts")
async def all_charts(session_id: str | None = None, limit: int = 200):
    return {"charts": sessions.list_charts(session_id, limit=limit)}


@app.get("/charts/file")
async def chart_file(path: str = Query(...)):
    """Serve a chart PNG (resolved relative to CWD, must be inside output/)."""
    p = Path(path).resolve()
    output_root = Path("output").resolve()
    try:
        p.relative_to(output_root)
    except ValueError:
        raise HTTPException(400, "Chart path outside output/ directory")
    if not p.exists():
        raise HTTPException(404, "Chart not found")
    return FileResponse(p)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@app.get("/reports")
async def list_reports_endpoint(session_id: str | None = None):
    return {"reports": sessions.list_reports(session_id)}


@app.get("/reports/{report_id}")
async def fetch_report(report_id: str):
    all_reports = sessions.list_reports()
    rec = next((r for r in all_reports if r["id"] == report_id), None)
    if rec is None:
        raise HTTPException(404, "Report not found")
    return rec


@app.get("/reports/{report_id}/export.{fmt}")
async def export_report(report_id: str, fmt: str):
    rec = next((r for r in sessions.list_reports() if r["id"] == report_id), None)
    if rec is None:
        raise HTTPException(404, "Report not found")

    md = rec.get("markdown", "")
    title = rec.get("title", "report")

    if fmt == "md":
        return _text_response(md, f"{title}.md", "text/markdown")

    if fmt == "html":
        from synthesizer import _to_html  # noqa: PLC0415
        html = _to_html(md, chart_paths=[])
        return _text_response(html, f"{title}.html", "text/html")

    if fmt == "xlsx":
        # Export the active spreadsheet's DataFrame
        ws = agent_app.get_workspace()
        active = ws.active_spreadsheet
        if active is None or active.df.empty:
            raise HTTPException(400, "No active spreadsheet to export")
        buf = io.BytesIO()
        active.df.to_excel(buf, index=False, engine="openpyxl")
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{title}.xlsx"'},
        )

    if fmt == "pdf":
        # Lightweight PDF: use reportlab if available, else return error
        try:
            from reportlab.lib.pagesizes import LETTER  # noqa: PLC0415
            from reportlab.pdfgen import canvas        # noqa: PLC0415
        except ImportError:
            raise HTTPException(
                501,
                "PDF export requires 'reportlab'. Install with: pip install reportlab",
            )
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=LETTER)
        width, height = LETTER
        y = height - 50
        c.setFont("Helvetica-Bold", 14)
        c.drawString(50, y, title[:80])
        y -= 24
        c.setFont("Helvetica", 9)
        for line in md.splitlines():
            if y < 40:
                c.showPage()
                c.setFont("Helvetica", 9)
                y = height - 50
            c.drawString(50, y, line[:110])
            y -= 12
        c.save()
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{title}.pdf"'},
        )

    raise HTTPException(400, f"Unsupported format: {fmt}")


def _text_response(text: str, filename: str, mime: str) -> StreamingResponse:
    return StreamingResponse(
        io.BytesIO(text.encode("utf-8")),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/settings")
async def get_settings():
    return {"settings": sessions.get_all_settings()}


@app.put("/settings")
async def update_settings(req: SettingsUpdate):
    for k, v in req.updates.items():
        sessions.set_setting(k, v)

    # Reflect Ollama config changes immediately
    import llm  # noqa: PLC0415
    if "ollama.model" in req.updates:
        llm.OLLAMA_MODEL = str(req.updates["ollama.model"])
    if "ollama.url" in req.updates:
        url = str(req.updates["ollama.url"]).rstrip("/")
        llm.OLLAMA_CHAT_URL = f"{url}/api/chat"

    return {"settings": sessions.get_all_settings()}


# ---------------------------------------------------------------------------
# Ollama probes
# ---------------------------------------------------------------------------

@app.get("/ollama/status")
async def ollama_status():
    settings_dict = sessions.get_all_settings()
    url   = str(settings_dict.get("ollama.url", "http://localhost:11434"))
    model = str(settings_dict.get("ollama.model", "qwen2.5-coder:14b"))

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{url.rstrip('/')}/api/tags")
            r.raise_for_status()
            tags = r.json().get("models", [])
            installed = [t.get("name", "") for t in tags]
            return {
                "reachable":     True,
                "url":           url,
                "active_model":  model,
                "model_present": any(model.split(":")[0] in name for name in installed),
                "installed":     installed,
            }
    except Exception as exc:
        return {"reachable": False, "url": url, "active_model": model,
                "model_present": False, "installed": [], "error": str(exc)}


@app.get("/ollama/models")
async def ollama_models():
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get("http://localhost:11434/api/tags")
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        raise HTTPException(503, f"Ollama unreachable: {exc}")


# ---------------------------------------------------------------------------
# Entry point — `python server.py` or `uvicorn server:app`
# ---------------------------------------------------------------------------

def main():
    import argparse  # noqa: PLC0415
    import uvicorn   # noqa: PLC0415
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", "-p", type=int, default=8765)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()

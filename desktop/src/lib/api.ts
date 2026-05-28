// FastAPI client. Base URL is detected via the Rust sidecar in production,
// or falls back to http://localhost:8765 in dev.

import type {
  AgentOutput, ChartRecord, ChatMessage, HealthInfo, MutationRecord,
  OllamaStatus, RecentFile, ReportRecord, SessionRecord, SessionSchema,
  WorkspaceInventory,
} from "../types";

let BASE = "http://127.0.0.1:8765";

export function setApiBase(url: string) { BASE = url.replace(/\/$/, ""); }
export function getApiBase()             { return BASE; }

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const url = `${BASE}${path}`;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init.headers as Record<string, string> | undefined),
  };
  const r = await fetch(url, { ...init, headers });
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try {
      const body = await r.json();
      if (body?.detail) detail = body.detail;
    } catch { /* ignore */ }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}

// ── Health / Ollama ───────────────────────────────────────────────────────

export const health        = ()  => req<HealthInfo>("/health");
export const ollamaStatus  = ()  => req<OllamaStatus>("/ollama/status");
export const ollamaModels  = ()  => req<{ models: { name: string }[] }>("/ollama/models");

// ── Sessions ──────────────────────────────────────────────────────────────

export const listSessions  = ()  => req<{ sessions: SessionRecord[] }>("/sessions");
export const getSession    = (id: string) => req<{ session: SessionRecord }>(`/sessions/${id}`);
export const activateSession = (id: string) => req<{ session: SessionRecord; schema: SessionSchema }>(`/sessions/${id}/activate`, { method: "POST" });
export const deleteSession = (id: string) => req<{ ok: boolean }>(`/sessions/${id}`, { method: "DELETE" });
export const renameSession = (id: string, name: string) => req<{ session: SessionRecord }>(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify({ name }) });
export const archiveSession = (id: string, archived = true) => req<{ session: SessionRecord }>(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify({ archived }) });

export const sessionMessages = (id: string) => req<{ messages: ChatMessage[] }>(`/sessions/${id}/messages`);
export const sessionCharts   = (id: string) => req<{ charts: ChartRecord[] }>(`/sessions/${id}/charts`);
export const sessionMutations = (id: string) => req<{ mutations: MutationRecord[] }>(`/sessions/${id}/mutations`);
export const sessionReports  = (id: string) => req<{ reports: ReportRecord[] }>(`/sessions/${id}/reports`);

// ── Files ─────────────────────────────────────────────────────────────────

export async function uploadFile(file: File): Promise<{
  session: SessionRecord;
  schema: SessionSchema;
  file_path: string;
  metadata?: Record<string, unknown>;
}> {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(`${BASE}/upload`, { method: "POST", body: fd });
  if (!r.ok) {
    let msg = `upload failed: ${r.status} ${r.statusText}`;
    try { const b = await r.json(); if (b?.detail) msg = b.detail; } catch { /* ignore */ }
    throw new Error(msg);
  }
  return r.json();
}

/** Create a session from a file path already on disk (e.g. via Tauri dialog). */
export async function createSessionFromPath(file_path: string, name?: string) {
  return req<{ session: SessionRecord; schema: SessionSchema }>(
    "/sessions",
    { method: "POST", body: JSON.stringify({ file_path, name }) },
  );
}

export const listRecentFiles = () => req<{ files: RecentFile[] }>("/recent");

// ── Workspace (multi-object) ──────────────────────────────────────────────

export const getWorkspace = () => req<WorkspaceInventory>("/workspace");

export async function addToWorkspace(file: File): Promise<{
  added: { kind: string; name: string; path: string };
  workspace: WorkspaceInventory;
}> {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(`${BASE}/workspace/add`, { method: "POST", body: fd });
  if (!r.ok) {
    let msg = `add failed: ${r.status} ${r.statusText}`;
    try { const b = await r.json(); if (b?.detail) msg = b.detail; } catch { /* ignore */ }
    throw new Error(msg);
  }
  return r.json();
}

export const activateWorkspaceObject = (name: string) =>
  req<{ active: string; kind: string; workspace: WorkspaceInventory }>(
    "/workspace/activate",
    { method: "POST", body: JSON.stringify({ name }) },
  );

export const removeWorkspaceObject = (name: string) =>
  req<{ removed: string; workspace: WorkspaceInventory }>(
    `/workspace/objects/${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );

// ── Query / analysis ──────────────────────────────────────────────────────

export const sendQuery  = (query: string, session_id?: string) =>
  req<AgentOutput>("/query", { method: "POST", body: JSON.stringify({ query, session_id }) });

export const generateReport = (query: string, session_id?: string, output_format = "markdown") =>
  req<AgentOutput>("/report", { method: "POST", body: JSON.stringify({ query, session_id, output_format }) });

export const verifyDates = (query = "verify the dates", session_id?: string) =>
  req<AgentOutput>("/verify", { method: "POST", body: JSON.stringify({ query, session_id }) });

export const mutateExcel = (instruction: string, session_id?: string) =>
  req<AgentOutput>("/mutate", { method: "POST", body: JSON.stringify({ instruction, session_id }) });

// ── Streaming ─────────────────────────────────────────────────────────────

export interface StreamEvent {
  type: "status" | "token" | "chart" | "step" | "final" | "error";
  data: any;
}

export async function streamQuery(
  query: string,
  session_id: string | undefined,
  onEvent: (e: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const r = await fetch(`${BASE}/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
    body: JSON.stringify({ query, session_id }),
    signal,
  });
  if (!r.ok || !r.body) throw new Error(`stream failed: ${r.status}`);

  const reader  = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop() || "";
    for (const block of events) {
      const line = block.split("\n").find(l => l.startsWith("data:"));
      if (!line) continue;
      const json = line.slice(5).trim();
      if (!json) continue;
      try {
        onEvent(JSON.parse(json) as StreamEvent);
      } catch { /* ignore malformed */ }
    }
  }
}

// ── Charts / reports ──────────────────────────────────────────────────────

export const allCharts   = (session_id?: string) =>
  req<{ charts: ChartRecord[] }>(`/charts${session_id ? `?session_id=${session_id}` : ""}`);

export const chartFileUrl = (path: string) =>
  `${BASE}/charts/file?path=${encodeURIComponent(path)}`;

export const listReports = (session_id?: string) =>
  req<{ reports: ReportRecord[] }>(`/reports${session_id ? `?session_id=${session_id}` : ""}`);

export const reportExportUrl = (report_id: string, fmt: "md" | "html" | "pdf" | "xlsx") =>
  `${BASE}/reports/${report_id}/export.${fmt}`;

// ── Settings ──────────────────────────────────────────────────────────────

export const getSettings    = () => req<{ settings: Record<string, any> }>("/settings");
export const updateSettings = (updates: Record<string, any>) =>
  req<{ settings: Record<string, any> }>("/settings", { method: "PUT", body: JSON.stringify({ updates }) });

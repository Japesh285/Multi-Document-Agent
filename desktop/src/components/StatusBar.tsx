import { useEffect, useState } from "react";
import {
  Server, Cpu, FileSpreadsheet, FileText, FileImage, Layers,
  Activity, AlertTriangle,
} from "lucide-react";
import clsx from "clsx";
import { useStore } from "../store";
import { SPREADSHEET_EXTS, DOCUMENT_EXTS, OCR_EXTS } from "../lib/tauri";

function extOf(name: string): string {
  const m = name.toLowerCase().match(/\.([a-z0-9]+)$/);
  return m ? m[1] : "";
}

function fileIcon(name: string) {
  const ext = extOf(name);
  if (SPREADSHEET_EXTS.includes(ext)) return <FileSpreadsheet size={11} className="text-chalk-400" />;
  if (DOCUMENT_EXTS.includes(ext))    return <FileText        size={11} className="text-chalk-400" />;
  if (OCR_EXTS.includes(ext))         return <FileImage       size={11} className="text-chalk-400" />;
  return <FileSpreadsheet size={11} className="text-chalk-400" />;
}

export default function StatusBar() {
  const {
    backend, health, ollama, settings, activeSessionId, sessions, sending, workspace,
  } = useStore();
  const session = sessions.find(s => s.id === activeSessionId);
  const model = (settings["ollama.model"] as string) || "qwen2.5-coder:14b";

  const backendState = backend?.running && health?.status === "ok" ? "ok" :
                       backend?.running ? "warn" : "err";
  const ollamaState  = ollama?.reachable && ollama?.model_present ? "ok" :
                       ollama?.reachable ? "warn" : "err";

  const totalObjects = workspace
    ? workspace.spreadsheets.length + workspace.documents.length + workspace.tables.length
    : 0;

  return (
    <footer className="h-7 px-3 flex items-center gap-4 bg-ink-900 border-t border-ink-700 text-[11px] text-chalk-300 select-none">
      <div className="flex items-center gap-1.5">
        <span className={clsx("status-dot", backendState)} />
        <Server size={11} />
        <span>Backend</span>
        <span className="font-mono text-chalk-400">{backend?.base_url || "—"}</span>
      </div>

      <div className="flex items-center gap-1.5">
        <span className={clsx("status-dot", ollamaState)} />
        <Cpu size={11} />
        <span>Ollama</span>
        <span className="font-mono text-chalk-400">{model}</span>
        {!ollama?.model_present && ollama?.reachable && (
          <span className="text-signal-warn flex items-center gap-1">
            <AlertTriangle size={10} /> model not installed
          </span>
        )}
      </div>

      {session && (
        <div className="flex items-center gap-1.5">
          {fileIcon(session.file_name)}
          <span className="font-mono text-chalk-400">{session.file_name}</span>
          {session.rows > 0 && (
            <>
              <span className="text-chalk-500">·</span>
              <span>{session.rows.toLocaleString()} rows</span>
            </>
          )}
        </div>
      )}

      {totalObjects > 0 && (
        <div className="flex items-center gap-1.5">
          <Layers size={11} className="text-chalk-400" />
          <span className="text-chalk-400">
            {workspace!.spreadsheets.length} sheet{workspace!.spreadsheets.length !== 1 ? "s" : ""}
            {" · "}
            {workspace!.documents.length} doc{workspace!.documents.length !== 1 ? "s" : ""}
            {" · "}
            {workspace!.tables.length} table{workspace!.tables.length !== 1 ? "s" : ""}
          </span>
        </div>
      )}

      <div className="flex-1" />

      <Heartbeat sending={sending} />
    </footer>
  );
}

function Heartbeat({ sending }: { sending: boolean }) {
  const [t, setT] = useState(0);
  useEffect(() => {
    if (!sending) { setT(0); return; }
    const start = Date.now();
    const i = setInterval(() => setT(Date.now() - start), 250);
    return () => clearInterval(i);
  }, [sending]);

  if (!sending) {
    return (
      <div className="flex items-center gap-1.5">
        <Activity size={11} className="text-chalk-500" />
        <span className="text-chalk-500">idle</span>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-1.5 text-accent-400">
      <span className="status-dot info" />
      <Activity size={11} />
      <span>working… {(t / 1000).toFixed(1)}s</span>
    </div>
  );
}

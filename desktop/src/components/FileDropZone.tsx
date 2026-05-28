import { useState } from "react";
import {
  Upload, FileSpreadsheet, FilePlus2, FileText, FileImage, FileSearch,
} from "lucide-react";
import clsx from "clsx";
import { useStore } from "../store";
import {
  pickWorkspaceFile,
  ALL_WORKSPACE_EXTS, SPREADSHEET_EXTS, DOCUMENT_EXTS, OCR_EXTS,
} from "../lib/tauri";

const ACCEPT_ATTR = ALL_WORKSPACE_EXTS.map(e => "." + e).join(",");

function extOf(name: string): string {
  const m = name.toLowerCase().match(/\.([a-z0-9]+)$/);
  return m ? m[1] : "";
}

function iconFor(ext: string) {
  if (SPREADSHEET_EXTS.includes(ext)) return <FileSpreadsheet size={14} className="text-chalk-400" />;
  if (DOCUMENT_EXTS.includes(ext))    return <FileText        size={14} className="text-chalk-400" />;
  if (OCR_EXTS.includes(ext))         return <FileImage       size={14} className="text-chalk-400" />;
  return <FileSearch size={14} className="text-chalk-400" />;
}

export default function FileDropZone() {
  const { uploadAndOpen, openFromPath, recentFiles } = useStore();
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleFile(f: File) {
    setBusy(true); setError(null);
    try { await uploadAndOpen(f); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  async function handlePath(p: string) {
    setBusy(true); setError(null);
    try { await openFromPath(p); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  async function browse() {
    const path = await pickWorkspaceFile();
    if (path) { await handlePath(path); return; }
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ACCEPT_ATTR;
    input.onchange = async () => {
      const f = input.files?.[0];
      if (f) await handleFile(f);
    };
    input.click();
  }

  return (
    <div className="flex-1 flex flex-col items-center justify-center p-8">
      <div
        className={clsx(
          "w-full max-w-2xl rounded-xl border-2 border-dashed transition-colors",
          "px-10 py-14 text-center",
          dragging
            ? "border-accent-500 bg-accent-600/10"
            : "border-ink-700 bg-ink-900 hover:border-ink-600",
        )}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={async (e) => {
          e.preventDefault(); setDragging(false);
          const f = e.dataTransfer.files?.[0];
          if (f) await handleFile(f);
        }}
      >
        <div className="w-14 h-14 mx-auto rounded-full bg-accent-600/15 border border-accent-600/30 flex items-center justify-center mb-4">
          <Upload size={22} className="text-accent-400" />
        </div>
        <div className="text-base font-semibold text-chalk-50 mb-1">
          Drop a file to begin
        </div>
        <div className="text-[12.5px] text-chalk-400 mb-3">
          Spreadsheet · Word · PDF · Image · all processed locally
        </div>
        <div className="text-[11px] text-chalk-500 mb-5 flex flex-wrap justify-center gap-1">
          {ALL_WORKSPACE_EXTS.map(e => (
            <span key={e} className="chip">.{e}</span>
          ))}
        </div>
        <button onClick={browse} disabled={busy} className="btn-primary mx-auto">
          <FilePlus2 size={14} />
          {busy ? "Opening…" : "Choose file"}
        </button>
        {error && (
          <div className="mt-4 text-[12px] text-signal-err">{error}</div>
        )}
      </div>

      {recentFiles.length > 0 && (
        <div className="w-full max-w-2xl mt-8">
          <div className="text-[11px] uppercase tracking-wider text-chalk-400 mb-2">
            Recent
          </div>
          <div className="space-y-1">
            {recentFiles.slice(0, 5).map(f => {
              const ext = extOf(f.file_name);
              return (
                <button
                  key={f.file_path}
                  onClick={() => void handlePath(f.file_path)}
                  className="w-full pane p-2.5 flex items-center gap-3 hover:border-accent-600/30 text-left transition-colors"
                >
                  {iconFor(ext)}
                  <div className="flex-1 min-w-0">
                    <div className="text-[12.5px] font-medium truncate">{f.file_name}</div>
                    <div className="text-[10.5px] text-chalk-500 truncate">
                      {f.file_path}
                    </div>
                  </div>
                  <div className="text-[10.5px] text-chalk-400 whitespace-nowrap">
                    {new Date(f.last_opened).toLocaleDateString()}
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

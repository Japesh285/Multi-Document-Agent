import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { X, Download, FileText } from "lucide-react";
import { useStore } from "../store";
import { reportExportUrl, getApiBase } from "../lib/api";
import type { ReportRecord } from "../types";
import { openExternal } from "../lib/tauri";

export default function ReportViewer({ reportId }: { reportId: string }) {
  const { reports, showReport } = useStore();
  const [report, setReport] = useState<ReportRecord | null>(
    reports.find(r => r.id === reportId) ?? null,
  );

  useEffect(() => {
    if (report) return;
    fetch(`${getApiBase()}/reports/${reportId}`)
      .then(r => r.json())
      .then(setReport)
      .catch(console.error);
  }, [reportId, report]);

  function exportTo(fmt: "md" | "html" | "pdf" | "xlsx") {
    const url = reportExportUrl(reportId, fmt);
    if (openExternal) void openExternal(url);
    else window.open(url, "_blank");
  }

  return (
    <div
      className="fixed inset-0 z-40 bg-ink-950/80 backdrop-blur-sm flex items-center justify-center p-8 animate-fade-in"
      onClick={() => showReport(null)}
    >
      <div
        className="pane w-full max-w-4xl max-h-[92vh] flex flex-col bg-ink-900"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 py-3 border-b border-ink-800 flex items-center gap-3">
          <FileText size={16} className="text-accent-400" />
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold truncate">
              {report?.title || "Report"}
            </div>
            {report && (
              <div className="text-[10.5px] text-chalk-400 truncate">
                {report.query}
              </div>
            )}
          </div>
          <div className="flex gap-1">
            <ExportBtn label="MD"   onClick={() => exportTo("md")} />
            <ExportBtn label="HTML" onClick={() => exportTo("html")} />
            <ExportBtn label="PDF"  onClick={() => exportTo("pdf")} />
            <ExportBtn label="XLSX" onClick={() => exportTo("xlsx")} />
          </div>
          <button className="btn-icon" onClick={() => showReport(null)} title="Close">
            <X size={14} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-8 py-6">
          {report ? (
            <div className="prose-agent max-w-none">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {report.markdown || "_(empty report)_"}
              </ReactMarkdown>
            </div>
          ) : (
            <div className="text-chalk-400 text-[12px]">Loading…</div>
          )}
        </div>
      </div>
    </div>
  );
}

function ExportBtn({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button className="btn-ghost text-[11px] py-1 px-2" onClick={onClick}>
      <Download size={11} />
      {label}
    </button>
  );
}

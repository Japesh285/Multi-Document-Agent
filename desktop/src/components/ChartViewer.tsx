import { useState } from "react";
import { Maximize2, X, Download } from "lucide-react";
import { chartFileUrl } from "../lib/api";

export default function ChartViewer({ path, title }: { path: string; title?: string }) {
  const [open, setOpen] = useState(false);
  const url = chartFileUrl(path);

  return (
    <>
      <button
        type="button"
        className="block w-full pane bg-ink-850 hover:border-accent-600/30 transition-colors group relative"
        onClick={() => setOpen(true)}
        title={title || path}
      >
        <img
          src={url}
          alt={title || "chart"}
          loading="lazy"
          className="w-full h-auto rounded-md max-h-[280px] object-contain"
          onError={(e) => {
            (e.currentTarget as HTMLImageElement).style.opacity = "0.3";
          }}
        />
        <div className="absolute top-1.5 right-1.5 opacity-0 group-hover:opacity-100 transition-opacity">
          <span className="btn-icon bg-ink-900/70 backdrop-blur-sm">
            <Maximize2 size={12} />
          </span>
        </div>
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 bg-ink-950/85 backdrop-blur-sm flex items-center justify-center p-8 animate-fade-in"
          onClick={() => setOpen(false)}
        >
          <div className="relative max-w-[90vw] max-h-[90vh]" onClick={(e) => e.stopPropagation()}>
            <div className="absolute -top-9 right-0 flex gap-1">
              <a
                href={url}
                download
                className="btn-ghost"
                onClick={(e) => e.stopPropagation()}
              >
                <Download size={13} /> Download
              </a>
              <button className="btn-icon" onClick={() => setOpen(false)} title="Close">
                <X size={14} />
              </button>
            </div>
            <img src={url} alt={title || "chart"} className="max-w-full max-h-[90vh] rounded-lg shadow-pane" />
            {title && <div className="mt-2 text-center text-[12px] text-chalk-300">{title}</div>}
          </div>
        </div>
      )}
    </>
  );
}

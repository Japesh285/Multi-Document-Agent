import { useState } from "react";
import {
  FileSpreadsheet, FilePlus2, Folder, Settings, Archive, Trash2,
  ChevronDown, ChevronRight, FileText, BarChart3,
} from "lucide-react";
import clsx from "clsx";
import { useStore } from "../store";
import { pickXlsxFile } from "../lib/tauri";

type Section = "sessions" | "recent" | "reports" | "charts";

export default function Sidebar() {
  const {
    sessions, activeSessionId, recentFiles, reports, charts,
    openSession, deleteSession, openFromPath, uploadAndOpen,
    toggleSettings, showReport,
  } = useStore();

  const [open, setOpen] = useState<Record<Section, boolean>>({
    sessions: true, recent: false, reports: false, charts: false,
  });
  const toggle = (s: Section) => setOpen(o => ({ ...o, [s]: !o[s] }));

  async function onOpenFile() {
    const path = await pickXlsxFile();
    if (path) {
      try { await openFromPath(path); } catch (e) { console.error(e); }
      return;
    }
    // Fallback for plain browser dev — hidden <input>
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".xlsx,.xlsm,.xls";
    input.onchange = async () => {
      const f = input.files?.[0];
      if (f) try { await uploadAndOpen(f); } catch (e) { console.error(e); }
    };
    input.click();
  }

  return (
    <aside className="bg-ink-900 border-r border-ink-700 flex flex-col min-h-0">
      {/* brand */}
      <div className="h-12 px-4 flex items-center gap-2 border-b border-ink-800 select-none">
        <div className="w-6 h-6 rounded bg-accent-600/20 border border-accent-600/40 flex items-center justify-center text-[10px] font-mono text-accent-400">
          SA
        </div>
        <div className="text-sm font-semibold tracking-tight">Spreadsheet Agent</div>
      </div>

      {/* actions */}
      <div className="p-3 border-b border-ink-800">
        <button className="btn-primary w-full" onClick={onOpenFile}>
          <FilePlus2 size={14} />
          Open spreadsheet
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 py-2 space-y-1">
        <Group
          icon={<Folder size={14} />}
          label="Workspaces"
          count={sessions.filter(s => !s.archived).length}
          open={open.sessions}
          onToggle={() => toggle("sessions")}
        >
          {sessions.filter(s => !s.archived).length === 0 && (
            <Empty hint="Open a spreadsheet to begin." />
          )}
          {sessions.filter(s => !s.archived).map(s => (
            <SidebarRow
              key={s.id}
              active={s.id === activeSessionId}
              onClick={() => void openSession(s.id)}
              icon={<FileSpreadsheet size={13} />}
              title={s.name}
              subtitle={`${s.rows.toLocaleString()} rows · ${s.domain}`}
              right={
                <button
                  className="btn-icon w-5 h-5 text-chalk-400 hover:text-signal-err"
                  title="Delete session"
                  onClick={(e) => {
                    e.stopPropagation();
                    if (confirm(`Delete session "${s.name}"?`)) void deleteSession(s.id);
                  }}
                >
                  <Trash2 size={12} />
                </button>
              }
            />
          ))}
        </Group>

        <Group
          icon={<Archive size={14} />}
          label="Recent files"
          count={recentFiles.length}
          open={open.recent}
          onToggle={() => toggle("recent")}
        >
          {recentFiles.length === 0 && <Empty hint="No recent files yet." />}
          {recentFiles.map(f => (
            <SidebarRow
              key={f.file_path}
              icon={<FileSpreadsheet size={13} />}
              title={f.file_name}
              subtitle={new Date(f.last_opened).toLocaleString()}
              onClick={() => void openFromPath(f.file_path)}
            />
          ))}
        </Group>

        <Group
          icon={<FileText size={14} />}
          label="Reports"
          count={reports.length}
          open={open.reports}
          onToggle={() => toggle("reports")}
        >
          {reports.length === 0 && <Empty hint="Reports will appear here." />}
          {reports.map(r => (
            <SidebarRow
              key={r.id}
              icon={<FileText size={13} />}
              title={r.title || r.query.slice(0, 60)}
              subtitle={new Date(r.created_at).toLocaleString()}
              onClick={() => showReport(r.id)}
            />
          ))}
        </Group>

        <Group
          icon={<BarChart3 size={14} />}
          label="Chart history"
          count={charts.length}
          open={open.charts}
          onToggle={() => toggle("charts")}
        >
          {charts.length === 0 && <Empty hint="Charts will appear here." />}
          {charts.slice(0, 30).map(c => (
            <SidebarRow
              key={c.id}
              icon={<BarChart3 size={13} />}
              title={c.title || c.step_id || "chart"}
              subtitle={new Date(c.created_at).toLocaleString()}
            />
          ))}
        </Group>
      </div>

      <div className="p-2 border-t border-ink-800">
        <button className="btn-ghost w-full" onClick={() => toggleSettings(true)}>
          <Settings size={14} />
          Settings
        </button>
      </div>
    </aside>
  );
}

// ----- Subcomponents -------------------------------------------------------

function Group({ icon, label, count, open, onToggle, children }:
  { icon: React.ReactNode; label: string; count: number; open: boolean;
    onToggle: () => void; children: React.ReactNode }) {
  return (
    <div>
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-1.5 px-2 py-1 text-[11.5px] font-medium uppercase tracking-wider text-chalk-400 hover:text-chalk-200"
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span className="text-chalk-300">{icon}</span>
        <span className="flex-1 text-left">{label}</span>
        <span className="text-[10px] text-chalk-500">{count}</span>
      </button>
      {open && <div className="space-y-0.5 ml-1.5">{children}</div>}
    </div>
  );
}

function SidebarRow({
  active, icon, title, subtitle, right, onClick,
}: {
  active?: boolean; icon: React.ReactNode; title: string;
  subtitle?: string; right?: React.ReactNode; onClick?: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={clsx(
        "w-full flex items-start gap-2 px-2 py-1.5 rounded-md text-left transition-colors",
        active ? "bg-accent-600/15 border border-accent-600/30 text-chalk-50"
               : "hover:bg-ink-800 text-chalk-200 border border-transparent",
      )}
    >
      <span className={clsx("mt-0.5", active ? "text-accent-400" : "text-chalk-400")}>{icon}</span>
      <div className="flex-1 min-w-0">
        <div className="text-[12.5px] font-medium truncate">{title}</div>
        {subtitle && <div className="text-[10.5px] text-chalk-400 truncate">{subtitle}</div>}
      </div>
      {right}
    </button>
  );
}

function Empty({ hint }: { hint: string }) {
  return <div className="px-2 py-2 text-[11px] text-chalk-500 italic">{hint}</div>;
}

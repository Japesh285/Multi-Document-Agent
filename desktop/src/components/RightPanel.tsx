import clsx from "clsx";
import {
  Database, BarChart3, ListTree, Terminal, Wrench, ImageIcon,
} from "lucide-react";
import { useStore } from "../store";
import type { SessionSchema } from "../types";
import ExecutionLog from "./ExecutionLog";
import ChartViewer from "./ChartViewer";

const TABS = [
  { id: "context",   label: "Context",   icon: <Database  size={13} /> },
  { id: "charts",    label: "Charts",    icon: <BarChart3 size={13} /> },
  { id: "steps",     label: "Steps",     icon: <ListTree  size={13} /> },
  { id: "mutations", label: "Mutations", icon: <Wrench    size={13} /> },
  { id: "logs",      label: "Logs",      icon: <Terminal  size={13} /> },
] as const;

export default function RightPanel() {
  const { rightTab, setRightTab, activeSchema, charts, mutations, lastSteps } = useStore();

  return (
    <aside className="bg-ink-900 border-l border-ink-700 flex flex-col min-h-0">
      <div className="h-12 px-2 flex items-center gap-0.5 border-b border-ink-800 overflow-x-auto">
        {TABS.map(t => (
          <button
            key={t.id}
            onClick={() => setRightTab(t.id)}
            className={clsx(
              "px-2.5 py-1 rounded text-[12px] font-medium flex items-center gap-1.5 transition-colors",
              rightTab === t.id
                ? "bg-ink-800 text-accent-400"
                : "text-chalk-300 hover:text-chalk-100 hover:bg-ink-800/60",
            )}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto p-3 text-[12.5px]">
        {rightTab === "context" && <ContextTab schema={activeSchema} />}

        {rightTab === "charts" && (
          charts.length === 0
            ? <Empty label="No charts yet — run an analysis." />
            : <div className="space-y-3">
                {charts.map(c => (
                  <div key={c.id} className="pane p-2">
                    <ChartViewer path={c.path} title={c.title} />
                    <div className="mt-1 text-[10.5px] text-chalk-500 truncate">
                      {c.step_id} · {new Date(c.created_at).toLocaleString()}
                    </div>
                  </div>
                ))}
              </div>
        )}

        {rightTab === "steps" && (
          lastSteps.length === 0
            ? <Empty label="Step results will appear after an analysis runs." />
            : <ExecutionLog steps={lastSteps} />
        )}

        {rightTab === "mutations" && (
          mutations.length === 0
            ? <Empty label="No Excel mutations yet." />
            : <div className="space-y-1.5">
                {mutations.map(m => (
                  <div key={m.id} className="pane p-2">
                    <div className="flex items-center gap-2">
                      <span className={clsx("status-dot", m.success ? "ok" : "err")} />
                      <div className="font-mono text-[12px] text-chalk-100">{m.action}</div>
                      <div className="chip">{m.column}</div>
                      <div className="ml-auto text-[10.5px] text-chalk-400">
                        {m.rows_affected} rows
                      </div>
                    </div>
                    {m.detail && <div className="mt-1 text-[11.5px] text-chalk-300">{m.detail}</div>}
                    {m.error   && <div className="mt-1 text-[11.5px] text-signal-err">{m.error}</div>}
                    <div className="mt-1 text-[10.5px] text-chalk-500">
                      {new Date(m.created_at).toLocaleString()}
                      {m.backup_path && <> · backup: <span className="font-mono">{m.backup_path}</span></>}
                    </div>
                  </div>
                ))}
              </div>
        )}

        {rightTab === "logs" && <BackendLogs />}
      </div>
    </aside>
  );
}

// ----- Subviews ------------------------------------------------------------

function ContextTab({ schema }: { schema: SessionSchema | null }) {
  if (!schema) return <Empty label="No active workspace." />;

  const semGroups = Object.entries(schema.semantics).reduce<Record<string, string[]>>((acc, [c, s]) => {
    (acc[s] = acc[s] || []).push(c);
    return acc;
  }, {});

  return (
    <div className="space-y-3">
      <div className="pane p-3">
        <div className="text-[11px] uppercase tracking-wider text-chalk-400 mb-1">Dataset</div>
        <div className="text-[13px] text-chalk-100 truncate" title={schema.file_path}>
          {schema.file_path.split(/[\\/]/).pop()}
        </div>
        <div className="mt-2 grid grid-cols-3 gap-2 text-center">
          <Stat label="Rows" value={schema.shape.rows.toLocaleString()} />
          <Stat label="Cols" value={schema.shape.columns.toString()} />
          <Stat label="Domain" value={schema.domain} small />
        </div>
        <div className="mt-2 text-[11px] text-chalk-400">
          confidence: {Math.round(schema.domain_confidence * 100)}%
        </div>
      </div>

      <div className="pane p-3">
        <div className="text-[11px] uppercase tracking-wider text-chalk-400 mb-1.5">
          Columns ({schema.columns.length})
        </div>
        <div className="max-h-[260px] overflow-y-auto">
          <table className="w-full text-[11.5px]">
            <tbody>
              {schema.columns.map(c => (
                <tr key={c} className="border-b border-ink-800 last:border-b-0">
                  <td className="py-1 pr-2 truncate" title={c}>{c}</td>
                  <td className="py-1 text-chalk-400 font-mono text-[10.5px]">
                    {(schema.dtypes[c] || "").replace("object", "obj")}
                  </td>
                  <td className="py-1 pl-1 text-right text-[10px] text-accent-400">
                    {schema.semantics[c] || ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="pane p-3">
        <div className="text-[11px] uppercase tracking-wider text-chalk-400 mb-1.5">Column groups</div>
        <div className="space-y-1">
          {Object.entries(semGroups).map(([sem, cols]) => (
            <div key={sem} className="flex items-start gap-2">
              <span className="chip">{sem}</span>
              <div className="text-[11.5px] text-chalk-300 flex-1">{cols.join(", ")}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value, small }: { label: string; value: string; small?: boolean }) {
  return (
    <div className="pane p-2 bg-ink-850">
      <div className="text-[10px] uppercase tracking-wider text-chalk-400">{label}</div>
      <div className={clsx("font-mono text-chalk-100", small ? "text-[12px]" : "text-[14px]")}>
        {value}
      </div>
    </div>
  );
}

function Empty({ label }: { label: string }) {
  return (
    <div className="h-full flex items-center justify-center text-[12px] text-chalk-500 select-none">
      <div className="flex items-center gap-1.5"><ImageIcon size={13} /> {label}</div>
    </div>
  );
}

function BackendLogs() {
  const { backend } = useStore();
  if (!backend?.log_tail?.length) {
    return <Empty label="Backend logs are empty." />;
  }
  return (
    <pre className="font-mono text-[11px] text-chalk-300 leading-relaxed whitespace-pre-wrap">
      {backend.log_tail.join("\n")}
    </pre>
  );
}

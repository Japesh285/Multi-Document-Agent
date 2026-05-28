import clsx from "clsx";
import {
  Database, BarChart3, ListTree, Terminal, Wrench, ImageIcon,
  FileSpreadsheet, FileText, Table, Layers, AlertTriangle, Sparkles,
} from "lucide-react";
import { useStore } from "../store";
import type {
  SessionSchema, WorkspaceInventory, WorkspaceObjectMeta, WorkspaceObjectKind,
} from "../types";
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
  const {
    rightTab, setRightTab, activeSchema, workspace,
    charts, mutations, lastSteps,
  } = useStore();

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
        {rightTab === "context" && (
          <ContextTab schema={activeSchema} workspace={workspace} />
        )}

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
            ? <Empty label="No mutations yet." />
            : <div className="space-y-1.5">
                {mutations.map(m => (
                  <div key={m.id} className="pane p-2">
                    <div className="flex items-center gap-2">
                      <span className={clsx("status-dot", m.success ? "ok" : "err")} />
                      <div className="font-mono text-[12px] text-chalk-100">{m.action}</div>
                      {m.column && <div className="chip">{m.column}</div>}
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

// ---------------------------------------------------------------------------
// Context tab
// ---------------------------------------------------------------------------

function ContextTab({ schema, workspace }: {
  schema: SessionSchema | null;
  workspace: WorkspaceInventory | null;
}) {
  if (!schema && !workspace) {
    return <Empty label="No active workspace." />;
  }

  return (
    <div className="space-y-3">
      {workspace && <WorkspaceCard ws={workspace} />}
      {schema && schema.shape && schema.shape.columns > 0 && <DatasetCard schema={schema} />}
      {schema && schema.source_kind && <OcrCard schema={schema} />}
      {schema && schema.shape && schema.shape.columns > 0 && <ColumnsCard schema={schema} />}
      {schema && Object.keys(schema.semantics ?? {}).length > 0 && (
        <ColumnGroupsCard schema={schema} />
      )}
    </div>
  );
}

function iconForKind(kind: WorkspaceObjectKind, className?: string) {
  const sz = 13;
  switch (kind) {
    case "spreadsheet": return <FileSpreadsheet size={sz} className={className} />;
    case "document":    return <FileText        size={sz} className={className} />;
    case "table":       return <Table           size={sz} className={className} />;
  }
}

function WorkspaceCard({ ws }: { ws: WorkspaceInventory }) {
  const totalObjects =
    ws.spreadsheets.length + ws.documents.length + ws.tables.length;
  if (totalObjects === 0) {
    return (
      <div className="pane p-3">
        <CardLabel icon={<Layers size={12} />} label="Workspace" />
        <Empty label="Workspace is empty." />
      </div>
    );
  }

  return (
    <div className="pane p-3">
      <CardLabel icon={<Layers size={12} />} label={`Workspace (${totalObjects})`} />

      <div className="mt-2 grid grid-cols-3 gap-2 text-center">
        <Stat label="Sheets" value={ws.spreadsheets.length.toString()} />
        <Stat label="Docs"   value={ws.documents.length.toString()} />
        <Stat label="Tables" value={ws.tables.length.toString()} />
      </div>

      {ws.active.most_recent && (
        <div className="mt-2 text-[11px] text-chalk-400">
          active → <span className="text-accent-400 font-mono">{ws.active.most_recent}</span>
        </div>
      )}

      <div className="mt-2 space-y-1">
        <ObjectList title="Spreadsheets" objs={ws.spreadsheets} activeName={ws.active.most_recent} />
        <ObjectList title="Documents"    objs={ws.documents}    activeName={ws.active.most_recent} />
        <ObjectList title="Tables"       objs={ws.tables}       activeName={ws.active.most_recent} />
      </div>

      {ws.memory.mutations.length > 0 && (
        <div className="mt-3 pt-2 border-t border-ink-800">
          <div className="text-[10.5px] uppercase tracking-wider text-chalk-500 mb-1">
            Recent mutations
          </div>
          {ws.memory.mutations.slice(-3).reverse().map((m, i) => (
            <div key={i} className="text-[11px] text-chalk-300 flex items-center gap-1.5">
              <span className={clsx("status-dot", m.success ? "ok" : "err")} />
              <span className="font-mono">{m.action}</span>
              <span className="text-chalk-500">→</span>
              <span className="text-accent-400 font-mono truncate">{m.object_name}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ObjectList({
  title, objs, activeName,
}: {
  title: string;
  objs: WorkspaceObjectMeta[];
  activeName: string;
}) {
  if (!objs.length) return null;
  return (
    <div>
      <div className="text-[10.5px] uppercase tracking-wider text-chalk-500 mt-1">{title}</div>
      <div className="space-y-0.5">
        {objs.map(o => {
          const active = o.name === activeName;
          return (
            <div
              key={o.name}
              className={clsx(
                "flex items-start gap-1.5 text-[11.5px] py-1 px-1.5 rounded",
                active ? "bg-accent-600/15" : "",
              )}
            >
              <span className={active ? "text-accent-400 mt-0.5" : "text-chalk-400 mt-0.5"}>
                {iconForKind(o.kind)}
              </span>
              <div className="flex-1 min-w-0">
                <div className={clsx("font-medium truncate", active ? "text-chalk-50" : "text-chalk-200")}>
                  {o.name}
                </div>
                <div className="text-[10.5px] text-chalk-500 truncate" title={o.summary}>
                  {o.summary}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function DatasetCard({ schema }: { schema: SessionSchema }) {
  return (
    <div className="pane p-3">
      <CardLabel icon={<FileSpreadsheet size={12} />} label="Active dataset" />
      <div className="text-[13px] text-chalk-100 truncate" title={schema.file_path}>
        {schema.file_path?.split(/[\\/]/).pop()}
      </div>
      <div className="mt-2 grid grid-cols-3 gap-2 text-center">
        <Stat label="Rows" value={schema.shape.rows.toLocaleString()} />
        <Stat label="Cols" value={schema.shape.columns.toString()} />
        <Stat label="Type" value={schema.file_type ?? schema.domain ?? "—"} small />
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 text-[11px]">
        {schema.domain && (
          <Meta label="Domain" value={`${schema.domain} (${Math.round((schema.domain_confidence ?? 0) * 100)}%)`} />
        )}
        {schema.encoding && <Meta label="Encoding" value={schema.encoding} />}
        {schema.delimiter && <Meta label="Delimiter" value={JSON.stringify(schema.delimiter)} />}
        {schema.active_sheet && <Meta label="Sheet" value={schema.active_sheet} />}
      </div>
      {schema.sheets && schema.sheets.length > 1 && (
        <div className="mt-2">
          <div className="text-[10.5px] uppercase tracking-wider text-chalk-500 mb-1">
            Sheets in workbook
          </div>
          <div className="flex flex-wrap gap-1">
            {schema.sheets.map(s => (
              <span key={s.name}
                className={clsx(
                  "chip", s.is_primary ? "ring-1 ring-accent-600/40" : "",
                )}
                title={`${s.rows} rows × ${s.columns} cols`}
              >
                {s.name}
                {s.is_primary && <Sparkles size={9} className="ml-1 inline" />}
              </span>
            ))}
          </div>
        </div>
      )}
      {schema.ingestion_warnings && schema.ingestion_warnings.length > 0 && (
        <Warnings list={schema.ingestion_warnings} />
      )}
    </div>
  );
}

function OcrCard({ schema }: { schema: SessionSchema }) {
  const conf = Math.round(schema.ocr_confidence ?? 0);
  const tier =
    conf >= 85 ? "high" :
    conf >= 70 ? "good" :
    conf >= 55 ? "fair" : "poor";
  return (
    <div className="pane p-3">
      <CardLabel icon={<FileText size={12} />} label="OCR / document" />
      <div className="grid grid-cols-3 gap-2 text-center">
        <Stat label="Pages"   value={(schema.page_count ?? 0).toString()} />
        <Stat label="Tables"  value={(schema.table_count ?? 0).toString()} />
        <Stat label="Conf"    value={`${conf}%`} small />
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 text-[11px]">
        <Meta label="Source" value={schema.source_kind ?? "—"} />
        <Meta label="Quality" value={tier} />
      </div>
      {schema.warnings && schema.warnings.length > 0 && <Warnings list={schema.warnings} />}
    </div>
  );
}

function ColumnsCard({ schema }: { schema: SessionSchema }) {
  return (
    <div className="pane p-3">
      <CardLabel
        icon={<Database size={12} />}
        label={`Columns (${schema.columns.length})`}
      />
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
  );
}

function ColumnGroupsCard({ schema }: { schema: SessionSchema }) {
  const semGroups = Object.entries(schema.semantics).reduce<Record<string, string[]>>((acc, [c, s]) => {
    (acc[s] = acc[s] || []).push(c);
    return acc;
  }, {});

  return (
    <div className="pane p-3">
      <CardLabel icon={<ListTree size={12} />} label="Column groups" />
      <div className="space-y-1">
        {Object.entries(semGroups).map(([sem, cols]) => (
          <div key={sem} className="flex items-start gap-2">
            <span className="chip">{sem}</span>
            <div className="text-[11.5px] text-chalk-300 flex-1">{cols.join(", ")}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Small primitives
// ---------------------------------------------------------------------------

function CardLabel({ icon, label }: { icon: React.ReactNode; label: string }) {
  return (
    <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-chalk-400 mb-1.5">
      {icon}
      <span>{label}</span>
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

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-2 truncate">
      <span className="text-chalk-500">{label}</span>
      <span className="text-chalk-200 font-mono truncate">{value}</span>
    </div>
  );
}

function Warnings({ list }: { list: string[] }) {
  return (
    <div className="mt-2 flex items-start gap-1.5 text-[11px] text-signal-warn">
      <AlertTriangle size={12} className="mt-0.5 flex-shrink-0" />
      <ul className="space-y-0.5">
        {list.slice(0, 4).map((w, i) => <li key={i}>{w}</li>)}
      </ul>
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

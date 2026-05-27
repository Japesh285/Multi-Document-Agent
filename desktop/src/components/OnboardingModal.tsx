import { useEffect, useState } from "react";
import {
  Server, Cpu, Database, FolderTree, CheckCircle2, XCircle, Loader2, ArrowRight,
} from "lucide-react";
import clsx from "clsx";
import { useStore } from "../store";
import { openExternal } from "../lib/tauri";

type Check = {
  id: string;
  label: string;
  detail: string;
  state: "pending" | "ok" | "warn" | "err";
  hint?: string;
  action?: { label: string; url: string };
};

export default function OnboardingModal() {
  const { health, ollama, backend, toggleOnboarding, refreshOllama } = useStore();
  const [checks, setChecks] = useState<Check[]>([]);

  // Recompute checks whenever health/ollama state changes
  useEffect(() => {
    const cs: Check[] = [
      {
        id: "backend",
        label: "Local backend",
        detail: backend?.running ? `running on ${backend.base_url}` : "not running",
        state: backend?.running && health?.status === "ok" ? "ok" : "err",
        hint: "Started automatically by the desktop shell.",
      },
      {
        id: "ollama",
        label: "Ollama reachable",
        detail: ollama?.reachable ? `connected at ${ollama.url}` : "unreachable",
        state: ollama?.reachable ? "ok" : "err",
        hint: ollama?.reachable ? "" : "Install and run Ollama locally.",
        action: ollama?.reachable ? undefined : {
          label: "Install Ollama",
          url: "https://ollama.com/download",
        },
      },
      {
        id: "model",
        label: "Model installed",
        detail: ollama?.model_present
          ? `${ollama.active_model} ready`
          : `${ollama?.active_model ?? "model"} not found`,
        state: ollama?.model_present ? "ok" : (ollama?.reachable ? "warn" : "err"),
        hint: ollama?.model_present
          ? ""
          : `Pull with: ollama pull ${ollama?.active_model ?? "qwen2.5-coder:14b"}`,
      },
      {
        id: "folders",
        label: "Workspace folders",
        detail: "output/ ready",
        state: "ok",
        hint: "Charts, backups and reports are saved under output/.",
      },
      {
        id: "db",
        label: "Session database",
        detail: "sqlite initialised",
        state: "ok",
        hint: "Stored at output/spreadsheet_agent.db.",
      },
    ];
    setChecks(cs);
  }, [health, ollama, backend]);

  const allOk = checks.every(c => c.state === "ok");

  return (
    <div className="fixed inset-0 z-50 bg-ink-950/85 backdrop-blur-md flex items-center justify-center p-8 animate-fade-in">
      <div className="pane bg-ink-900 w-full max-w-2xl">
        <div className="px-6 pt-5 pb-3 border-b border-ink-800">
          <div className="text-base font-semibold">Welcome to Spreadsheet Agent</div>
          <div className="text-[12.5px] text-chalk-300 mt-1">
            Let's make sure your local environment is ready. Everything runs on this machine.
          </div>
        </div>

        <div className="p-5 space-y-2">
          {checks.map(c => (
            <CheckRow key={c.id} c={c} />
          ))}
        </div>

        <div className="px-6 py-4 border-t border-ink-800 flex items-center gap-2">
          <button className="btn-ghost" onClick={() => void refreshOllama()}>
            <Loader2 size={12} /> Re-check
          </button>
          <div className="flex-1" />
          <button
            className="btn-primary"
            onClick={() => toggleOnboarding(false)}
            disabled={!allOk && !ollama?.reachable}
          >
            {allOk ? "Get started" : "Continue anyway"} <ArrowRight size={13} />
          </button>
        </div>
      </div>
    </div>
  );
}

function CheckRow({ c }: { c: Check }) {
  const icons: Record<string, React.ReactNode> = {
    backend: <Server   size={14} />,
    ollama:  <Cpu      size={14} />,
    model:   <Cpu      size={14} />,
    folders: <FolderTree size={14} />,
    db:      <Database size={14} />,
  };
  const stateIcon =
    c.state === "ok"   ? <CheckCircle2 size={14} className="text-signal-ok" /> :
    c.state === "warn" ? <CheckCircle2 size={14} className="text-signal-warn" /> :
    c.state === "err"  ? <XCircle      size={14} className="text-signal-err" /> :
                         <Loader2 size={14} className="animate-spin text-chalk-400" />;

  return (
    <div className="pane bg-ink-850 p-3">
      <div className="flex items-center gap-3">
        <div className={clsx(
          "w-7 h-7 rounded-md flex items-center justify-center",
          c.state === "ok"   ? "bg-signal-ok/15 text-signal-ok" :
          c.state === "warn" ? "bg-signal-warn/15 text-signal-warn" :
          c.state === "err"  ? "bg-signal-err/15 text-signal-err"
                             : "bg-ink-800 text-chalk-400",
        )}>{icons[c.id]}</div>
        <div className="flex-1 min-w-0">
          <div className="text-[12.5px] font-medium text-chalk-100">{c.label}</div>
          <div className="text-[11px] text-chalk-400 truncate">{c.detail}</div>
        </div>
        {stateIcon}
      </div>
      {c.hint && (
        <div className="mt-2 ml-10 text-[11px] text-chalk-400">
          {c.hint}
          {c.action && (
            <button
              className="ml-2 text-accent-400 hover:underline"
              onClick={() => openExternal?.(c.action!.url)}
            >
              {c.action.label} →
            </button>
          )}
        </div>
      )}
    </div>
  );
}

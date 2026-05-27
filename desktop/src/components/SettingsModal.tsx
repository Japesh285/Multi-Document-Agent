import { useEffect, useState } from "react";
import { X, Save, RefreshCw, Loader2 } from "lucide-react";
import { useStore } from "../store";
import * as api from "../lib/api";

const SECTIONS = [
  { id: "model",   label: "Model & LLM" },
  { id: "tools",   label: "MCP Tools" },
  { id: "report",  label: "Reports" },
  { id: "backup",  label: "Backups" },
  { id: "ui",      label: "UI" },
  { id: "memory",  label: "Memory" },
] as const;

type SectionId = typeof SECTIONS[number]["id"];

export default function SettingsModal() {
  const { settings, toggleSettings, updateSettings, ollama, refreshOllama } = useStore();
  const [draft, setDraft] = useState<Record<string, any>>({});
  const [section, setSection] = useState<SectionId>("model");
  const [saving, setSaving] = useState(false);
  const [models, setModels] = useState<string[]>([]);

  useEffect(() => { setDraft(settings); }, [settings]);

  useEffect(() => {
    api.ollamaModels()
      .then(r => setModels((r.models || []).map(m => m.name)))
      .catch(() => setModels([]));
  }, []);

  function set(key: string, value: any) {
    setDraft(d => ({ ...d, [key]: value }));
  }

  async function save() {
    setSaving(true);
    try {
      const diff: Record<string, any> = {};
      for (const k of Object.keys(draft)) {
        if (draft[k] !== settings[k]) diff[k] = draft[k];
      }
      if (Object.keys(diff).length) await updateSettings(diff);
      await refreshOllama();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-40 bg-ink-950/80 backdrop-blur-sm flex items-center justify-center p-8 animate-fade-in"
      onClick={() => toggleSettings(false)}
    >
      <div
        className="pane bg-ink-900 w-full max-w-3xl h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="h-12 px-4 flex items-center border-b border-ink-800">
          <div className="text-sm font-semibold">Settings</div>
          <div className="flex-1" />
          <button className="btn-icon" onClick={() => toggleSettings(false)} title="Close">
            <X size={14} />
          </button>
        </div>

        <div className="flex-1 grid grid-cols-[180px_1fr] min-h-0">
          {/* sidebar */}
          <div className="bg-ink-850 border-r border-ink-800 p-2 space-y-0.5">
            {SECTIONS.map(s => (
              <button
                key={s.id}
                onClick={() => setSection(s.id)}
                className={`w-full px-2.5 py-1.5 rounded text-[12px] text-left transition-colors ${
                  section === s.id
                    ? "bg-accent-600/15 text-accent-400 border border-accent-600/30"
                    : "text-chalk-300 hover:bg-ink-800 border border-transparent"
                }`}
              >
                {s.label}
              </button>
            ))}
          </div>

          {/* body */}
          <div className="overflow-y-auto p-5 space-y-4">
            {section === "model" && (
              <>
                <Field label="Ollama URL">
                  <input
                    className="input"
                    value={draft["ollama.url"] || ""}
                    onChange={e => set("ollama.url", e.target.value)}
                  />
                </Field>

                <Field label="Active model"
                       hint={ollama?.reachable ? `${models.length} installed` : "Ollama unreachable"}>
                  <div className="flex gap-2">
                    <select
                      className="input flex-1"
                      value={draft["ollama.model"] || ""}
                      onChange={e => set("ollama.model", e.target.value)}
                    >
                      {!models.includes(draft["ollama.model"]) && (
                        <option value={draft["ollama.model"]}>
                          {draft["ollama.model"]} (custom)
                        </option>
                      )}
                      {models.map(m => <option key={m} value={m}>{m}</option>)}
                    </select>
                    <button
                      className="btn-ghost"
                      onClick={async () => {
                        const r = await api.ollamaModels().catch(() => null);
                        if (r) setModels((r.models || []).map(m => m.name));
                      }}
                      title="Refresh model list"
                    >
                      <RefreshCw size={13} />
                    </button>
                  </div>
                </Field>

                <Field label="Context size (tokens)">
                  <input
                    type="number"
                    className="input"
                    value={draft["ollama.context"] ?? 8192}
                    onChange={e => set("ollama.context", Number(e.target.value))}
                  />
                </Field>

                <Field label="Temperature">
                  <input
                    type="number" step="0.01" min="0" max="2"
                    className="input"
                    value={draft["ollama.temperature"] ?? 0.05}
                    onChange={e => set("ollama.temperature", Number(e.target.value))}
                  />
                </Field>
              </>
            )}

            {section === "tools" && (
              <>
                <Toggle
                  label="Enable web search (MCP)"
                  hint="DuckDuckGo-backed search for verification and news"
                  checked={!!draft["mcp.web_search_enabled"]}
                  onChange={(v) => set("mcp.web_search_enabled", v)}
                />
                <Field label="Search cache TTL (hours)">
                  <input
                    type="number" min={0}
                    className="input"
                    value={draft["mcp.cache_ttl_hours"] ?? 24}
                    onChange={e => set("mcp.cache_ttl_hours", Number(e.target.value))}
                  />
                </Field>
              </>
            )}

            {section === "report" && (
              <>
                <Field label="Default report format">
                  <select
                    className="input"
                    value={draft["report.default_format"] || "markdown"}
                    onChange={e => set("report.default_format", e.target.value)}
                  >
                    <option value="markdown">Markdown</option>
                    <option value="html">HTML</option>
                  </select>
                </Field>
              </>
            )}

            {section === "backup" && (
              <>
                <Toggle
                  label="Backup Excel before mutations"
                  hint="Saves a timestamped copy to output/backups/"
                  checked={!!draft["backup.enabled"]}
                  onChange={(v) => set("backup.enabled", v)}
                />
                <Field label="Retention (days)">
                  <input
                    type="number" min={1}
                    className="input"
                    value={draft["backup.retention"] ?? 30}
                    onChange={e => set("backup.retention", Number(e.target.value))}
                  />
                </Field>
              </>
            )}

            {section === "ui" && (
              <>
                <Field label="Theme">
                  <select
                    className="input"
                    value={draft["ui.theme"] || "dark"}
                    onChange={e => set("ui.theme", e.target.value)}
                  >
                    <option value="dark">Dark</option>
                    <option value="light" disabled>Light (coming soon)</option>
                  </select>
                </Field>
                <Toggle
                  label="Compact density"
                  hint="Smaller spacing for dense layouts"
                  checked={!!draft["ui.compact"]}
                  onChange={(v) => set("ui.compact", v)}
                />
              </>
            )}

            {section === "memory" && (
              <Field label="Max analysis steps per query">
                <input
                  type="number" min={2} max={16}
                  className="input"
                  value={draft["memory.max_steps"] ?? 8}
                  onChange={e => set("memory.max_steps", Number(e.target.value))}
                />
              </Field>
            )}
          </div>
        </div>

        <div className="h-12 px-4 flex items-center justify-end border-t border-ink-800 gap-2">
          <button className="btn-ghost" onClick={() => toggleSettings(false)}>Cancel</button>
          <button className="btn-primary" onClick={save} disabled={saving}>
            {saving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
            Save
          </button>
        </div>
      </div>
    </div>
  );
}

// ----- Subcomponents -------------------------------------------------------

function Field({ label, hint, children }:
  { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[11.5px] font-medium text-chalk-200 mb-1">{label}</div>
      {children}
      {hint && <div className="mt-1 text-[10.5px] text-chalk-500">{hint}</div>}
    </div>
  );
}

function Toggle({ label, hint, checked, onChange }:
  { label: string; hint?: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-start gap-3 cursor-pointer">
      <input
        type="checkbox"
        checked={checked}
        onChange={e => onChange(e.target.checked)}
        className="mt-1 w-4 h-4 accent-accent-500"
      />
      <div>
        <div className="text-[12.5px] text-chalk-100">{label}</div>
        {hint && <div className="text-[10.5px] text-chalk-500">{hint}</div>}
      </div>
    </label>
  );
}

// Zustand store — application state.

import { create } from "zustand";
import type {
  AgentOutput, BackendInfo, ChartRecord, ChatMessage, HealthInfo,
  Mode, MutationRecord, OllamaStatus, RecentFile, ReportRecord,
  SessionRecord, SessionSchema, StepResult,
} from "./types";
import * as api from "./lib/api";
import * as tauri from "./lib/tauri";

type RightTab = "context" | "charts" | "steps" | "logs" | "mutations";

interface AppState {
  // backend
  backend:        BackendInfo | null;
  health:         HealthInfo  | null;
  ollama:         OllamaStatus | null;
  ready:          boolean;
  bootError:      string | null;
  needsOnboarding: boolean;

  // sessions
  sessions:       SessionRecord[];
  activeSessionId: string | null;
  activeSchema:   SessionSchema | null;
  recentFiles:    RecentFile[];

  // chat
  messages:       ChatMessage[];
  sending:        boolean;
  streamingId:    string | null;

  // right panel
  rightTab:       RightTab;
  charts:         ChartRecord[];
  mutations:      MutationRecord[];
  reports:        ReportRecord[];
  lastSteps:      StepResult[];

  // ui
  mode:           Mode;
  showSettings:   boolean;
  showOnboarding: boolean;
  showReportId:   string | null;
  settings:       Record<string, any>;

  // ── actions ─────────────────────────────────────────────────────────────
  boot:               () => Promise<void>;
  refreshHealth:      () => Promise<void>;
  refreshOllama:      () => Promise<void>;
  refreshSessions:    () => Promise<void>;
  refreshRecent:      () => Promise<void>;
  refreshSettings:    () => Promise<void>;
  openSession:        (id: string) => Promise<void>;
  closeSession:       () => void;
  uploadAndOpen:      (file: File) => Promise<void>;
  openFromPath:       (path: string) => Promise<void>;
  deleteSession:      (id: string) => Promise<void>;
  renameSession:      (id: string, name: string) => Promise<void>;
  sendQuery:          (text: string) => Promise<void>;
  setMode:            (m: Mode) => void;
  setRightTab:        (t: RightTab) => void;
  toggleSettings:     (v?: boolean) => void;
  toggleOnboarding:   (v?: boolean) => void;
  showReport:         (id: string | null) => void;
  updateSettings:     (patch: Record<string, any>) => Promise<void>;
  appendLocalMessage: (m: ChatMessage) => void;
}

const _id = () => Math.random().toString(36).slice(2, 12);

function statusToHealth(o: OllamaStatus | null): boolean {
  return !!o?.reachable && !!o?.model_present;
}

export const useStore = create<AppState>((set, get) => ({
  backend:        null,
  health:         null,
  ollama:         null,
  ready:          false,
  bootError:      null,
  needsOnboarding: false,

  sessions:       [],
  activeSessionId: null,
  activeSchema:   null,
  recentFiles:    [],

  messages:       [],
  sending:        false,
  streamingId:    null,

  rightTab:       "context",
  charts:         [],
  mutations:      [],
  reports:        [],
  lastSteps:      [],

  mode:           "chat",
  showSettings:   false,
  showOnboarding: false,
  showReportId:   null,
  settings:       {},

  // ─────────────────────────────────────────────────────────────────────

  boot: async () => {
    // 1. Pick up backend base url from Tauri (if present)
    const info = await tauri.backendInfo();
    if (info?.base_url) api.setApiBase(info.base_url);
    set({ backend: info });

    // 2. Wait briefly for /health to respond (Rust sidecar already does ~30s)
    let h: HealthInfo | null = null;
    for (let i = 0; i < 30; i++) {
      try {
        h = await api.health();
        break;
      } catch {
        await new Promise(r => setTimeout(r, 500));
      }
    }
    if (!h) {
      set({ bootError: "Backend did not respond at /health. Check Python is installed and dependencies are in place.", ready: false });
      return;
    }
    set({ health: h });

    // 3. Pull settings, sessions, ollama status in parallel
    const [olRes, sessRes, sRes, rRes] = await Promise.allSettled([
      api.ollamaStatus(),
      api.listSessions(),
      api.getSettings(),
      api.listRecentFiles(),
    ]);
    if (olRes.status === "fulfilled")  set({ ollama: olRes.value });
    if (sessRes.status === "fulfilled") set({ sessions: sessRes.value.sessions });
    if (sRes.status === "fulfilled")    set({ settings: sRes.value.settings });
    if (rRes.status === "fulfilled")    set({ recentFiles: rRes.value.files });

    const okOllama = statusToHealth(get().ollama);
    set({
      ready: true,
      needsOnboarding: !okOllama,
      showOnboarding: !okOllama,
    });
  },

  refreshHealth: async () => {
    try { set({ health: await api.health() }); } catch { /* ignore */ }
  },

  refreshOllama: async () => {
    try { set({ ollama: await api.ollamaStatus() }); } catch { /* ignore */ }
  },

  refreshSessions: async () => {
    try { set({ sessions: (await api.listSessions()).sessions }); } catch { /* ignore */ }
  },

  refreshRecent: async () => {
    try { set({ recentFiles: (await api.listRecentFiles()).files }); } catch { /* ignore */ }
  },

  refreshSettings: async () => {
    try { set({ settings: (await api.getSettings()).settings }); } catch { /* ignore */ }
  },

  openSession: async (id) => {
    const { session, schema } = await api.activateSession(id);
    const [m, c, mu, r] = await Promise.all([
      api.sessionMessages(id),
      api.sessionCharts(id),
      api.sessionMutations(id),
      api.sessionReports(id),
    ]);
    set({
      activeSessionId: session.id,
      activeSchema:    schema,
      messages:        m.messages,
      charts:          c.charts,
      mutations:       mu.mutations,
      reports:         r.reports,
      lastSteps:       [],
    });
    await get().refreshSessions();
  },

  closeSession: () => set({
    activeSessionId: null, activeSchema: null,
    messages: [], charts: [], mutations: [], reports: [], lastSteps: [],
  }),

  uploadAndOpen: async (file) => {
    const res = await api.uploadFile(file);
    await get().refreshSessions();
    await get().openSession(res.session.id);
  },

  openFromPath: async (path) => {
    const res = await api.createSessionFromPath(path);
    await get().refreshSessions();
    await get().openSession(res.session.id);
  },

  deleteSession: async (id) => {
    await api.deleteSession(id);
    if (get().activeSessionId === id) get().closeSession();
    await get().refreshSessions();
  },

  renameSession: async (id, name) => {
    await api.renameSession(id, name);
    await get().refreshSessions();
  },

  sendQuery: async (text) => {
    const sid = get().activeSessionId;
    if (!sid || !text.trim() || get().sending) return;

    const userMsg: ChatMessage = {
      id: _id(), session_id: sid, role: "user", content: text.trim(),
      created_at: new Date().toISOString(),
    };
    const placeholder: ChatMessage = {
      id: _id(), session_id: sid, role: "assistant", content: "",
      created_at: new Date().toISOString(), pending: true,
    };
    set({
      messages:  [...get().messages, userMsg, placeholder],
      sending:   true,
      streamingId: placeholder.id,
    });

    const mode = get().mode;
    let out: AgentOutput | null = null;
    let error: string | null = null;
    try {
      if (mode === "report")        out = await api.generateReport(text.trim(), sid);
      else if (mode === "verify")   out = await api.verifyDates(text.trim(), sid);
      else if (mode === "mutate")   out = await api.mutateExcel(text.trim(), sid);
      else                          out = await api.sendQuery(text.trim(), sid);
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    }

    set(state => {
      const msgs = state.messages.map(m =>
        m.id === placeholder.id
          ? {
              ...m,
              pending: false,
              content: out?.report ?? `**Error.** ${error || "Unknown failure."}`,
              intent: out?.intent ?? "error",
              confidence: out?.confidence ?? 0,
              elapsed: out?.elapsed ?? 0,
              charts: out?.charts ?? [],
              web_results: out?.web_results ?? [],
              excel_updates: out?.excel_updates ?? [],
              step_results: out?.step_results ?? [],
            }
          : m,
      );
      return {
        messages: msgs,
        sending: false,
        streamingId: null,
        lastSteps: out?.step_results ?? state.lastSteps,
      };
    });

    // Refresh secondary data
    void get().refreshSessions();
    if (sid) {
      try {
        const [c, mu, r] = await Promise.all([
          api.sessionCharts(sid),
          api.sessionMutations(sid),
          api.sessionReports(sid),
        ]);
        set({ charts: c.charts, mutations: mu.mutations, reports: r.reports });
      } catch { /* ignore */ }
    }
  },

  setMode:    (m) => set({ mode: m }),
  setRightTab: (t) => set({ rightTab: t }),
  toggleSettings:   (v) => set({ showSettings:   v ?? !get().showSettings }),
  toggleOnboarding: (v) => set({ showOnboarding: v ?? !get().showOnboarding }),
  showReport:       (id) => set({ showReportId: id }),

  updateSettings: async (patch) => {
    const res = await api.updateSettings(patch);
    set({ settings: res.settings });
  },

  appendLocalMessage: (m) => set({ messages: [...get().messages, m] }),
}));

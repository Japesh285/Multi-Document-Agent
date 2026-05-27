# Spreadsheet Agent — Desktop

Local-first Windows desktop client for the Autonomous Spreadsheet Intelligence
backend (`../server.py`).

* **Shell:** Tauri 2 (Rust)
* **UI:**    React + Vite + TypeScript + TailwindCSS
* **Backend:** FastAPI launched as a managed child process
* **LLM:**   Local Ollama (`qwen2.5-coder:14b` by default)
* **Storage:** SQLite (`../output/spreadsheet_agent.db`)

---

## Prerequisites

Install these once on your Windows machine:

1. **Node.js 20+** — https://nodejs.org/
2. **Rust (stable)** — https://www.rust-lang.org/tools/install
   * After install, in a fresh terminal: `rustup default stable`
3. **Microsoft Visual Studio 2022 Build Tools** with the *Desktop development
   with C++* workload — needed for compiling the Tauri binary.
4. **Tauri CLI v2** — installed automatically via `npm install` (it's in
   `devDependencies`).
5. **Python 3.10+** — https://www.python.org/
6. **Ollama** — https://ollama.com/download
   * After install: `ollama pull qwen2.5-coder:14b`

---

## One-time backend setup

From `d:\AI_Work` (the repo root):

```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

The Tauri shell will look for `venv\Scripts\python.exe` first; if it's not
there, it falls back to whatever `python` is on `PATH`.

---

## Develop the desktop app

From `d:\AI_Work\desktop`:

```powershell
npm install
npm run tauri:dev
```

What happens on launch:

1. Vite serves the React UI on `http://localhost:5173`.
2. Tauri spawns the Python FastAPI backend on `http://127.0.0.1:8765`
   using the bundled venv if present.
3. The React app polls `/health` until the backend responds, then renders
   the workspace UI. If Ollama or the model is missing, an onboarding
   modal appears.

When you close the window, the Rust shell terminates the Python process
tree (`taskkill /T /F` on Windows) so no orphan uvicorn workers are left.

---

## App architecture at a glance

```
desktop/
├── src-tauri/              # Rust shell + process manager
│   ├── src/main.rs         # Tauri entry, commands, lifecycle
│   ├── src/backend.rs      # Spawns + monitors the Python child
│   ├── tauri.conf.json     # Window, CSP, bundle config
│   ├── capabilities/       # Tauri 2 capability allowlist
│   └── Cargo.toml
└── src/
    ├── main.tsx            # React bootstrap
    ├── App.tsx             # Top-level layout
    ├── store.ts            # Zustand store (sessions, chat, settings, …)
    ├── types.ts            # Shared TypeScript types
    ├── lib/
    │   ├── api.ts          # FastAPI client
    │   └── tauri.ts        # Tauri command bridge
    ├── components/
    │   ├── Sidebar.tsx     # Workspaces, recent files, reports, charts
    │   ├── ChatPanel.tsx   # Main chat surface + mode switcher
    │   ├── RightPanel.tsx  # Context, charts, steps, mutations, logs
    │   ├── StatusBar.tsx   # Backend / Ollama / session indicators
    │   ├── Message.tsx     # One chat turn (user/assistant)
    │   ├── ChartViewer.tsx # Inline + fullscreen chart preview
    │   ├── ExecutionLog.tsx# Per-step pandas code + output
    │   ├── ReportViewer.tsx# Markdown report + MD/HTML/PDF/XLSX export
    │   ├── FileDropZone.tsx# Drag-and-drop xlsx onboarding
    │   ├── SettingsModal.tsx
    │   ├── OnboardingModal.tsx
    │   └── BootSplash.tsx
    └── styles/index.css    # Tailwind + custom components
```

---

## Backend endpoints used by the UI

All on `http://127.0.0.1:8765`:

| Method | Path                                    | Purpose                          |
|--------|-----------------------------------------|----------------------------------|
| GET    | `/health`                               | liveness + ollama + session      |
| GET    | `/ollama/status` / `/ollama/models`     | onboarding checks                |
| POST   | `/upload`                               | multipart xlsx upload            |
| POST   | `/sessions`                             | create session from file path    |
| GET    | `/sessions`                             | list sessions                    |
| POST   | `/sessions/{id}/activate`               | switch active workspace          |
| DELETE | `/sessions/{id}`                        | delete session                   |
| PATCH  | `/sessions/{id}`                        | rename / archive                 |
| GET    | `/sessions/{id}/messages`               | chat history                     |
| GET    | `/sessions/{id}/charts`                 | chart history                    |
| GET    | `/sessions/{id}/mutations`              | excel mutation history           |
| GET    | `/sessions/{id}/reports`                | report list                      |
| POST   | `/query`                                | conversational analysis turn     |
| POST   | `/stream`                               | streaming SSE chat               |
| POST   | `/report`                               | full report pipeline             |
| POST   | `/verify`                               | date verification                |
| POST   | `/mutate`                               | Excel column mutation            |
| GET    | `/charts/file?path=…`                   | serve a chart PNG                |
| GET    | `/reports/{id}/export.{md,html,pdf,xlsx}` | export                         |
| GET/PUT| `/settings`                             | persistent app settings          |
| GET    | `/recent`                               | recent files                     |

---

## Packaging a Windows installer

From `d:\AI_Work\desktop`:

```powershell
npm run tauri:build
```

Output (NSIS + MSI installers):

```
desktop/src-tauri/target/release/bundle/nsis/Spreadsheet Agent_0.1.0_x64-setup.exe
desktop/src-tauri/target/release/bundle/msi/Spreadsheet Agent_0.1.0_x64_en-US.msi
```

### Bundling the Python backend

The current `tauri.conf.json` declares the Python files as `resources`, so
they're copied next to the installed `.exe`. For a fully self-contained
installer you'll also need to ship Python itself. Two common approaches:

1. **Embedded Python** — bundle `python-3.x.x-embed-amd64.zip` and a
   pre-populated `Lib/site-packages/` alongside the exe; update
   `backend.rs::pick_python()` to point at it.
2. **System Python** — require the user to install Python and run
   `pip install -r requirements.txt` once; document this in the installer
   README. The Rust shell already falls back to `python.exe` on PATH.

Either way, `ollama` itself stays a separate install — that's by design.

### Icons

Tauri expects icons under `src-tauri/icons/`. To regenerate from a 1024×1024
PNG source:

```powershell
npx @tauri-apps/cli icon path\to\source-icon.png
```

If you skip this step, the build will fail looking for `icon.ico`.

---

## Known limitations / TODOs

* **Streaming**: `/stream` currently emits `status` + `final` events.
  Token-level streaming requires refactoring `llm.call_chat` to expose a
  generator — wire it through `_lg_synthesize` for the best UX.
* **PDF export** uses `reportlab` (text-only). For a fully styled PDF,
  consider `weasyprint` or `playwright` to print the HTML report.
* **Multi-session backend state**: the Python engine still keeps the
  active DataFrame in module globals. Activating a session via the API
  swaps those globals atomically, but two queries to two different
  sessions cannot run concurrently. Refactor `app._df` / `_schema` into a
  session-keyed store if you need parallel workspaces.
* **Light theme**: token slots exist in Tailwind but no light palette is
  wired up yet.
* **Token streaming** in the UI: when the backend `/stream` is upgraded
  to emit `token` events, `store.ts::sendQuery` should switch from
  `api.sendQuery` to `api.streamQuery` and append tokens to the pending
  message.

---

## Quick troubleshooting

| Symptom                                | Fix |
|----------------------------------------|-----|
| "Backend did not respond" on splash    | Activate venv and run `pip install -r ../requirements.txt`. Watch `Logs` tab in the right panel for the failing Python output. |
| Ollama unreachable                     | `ollama serve` in a separate terminal. Default URL is `http://localhost:11434`. |
| Model not installed                    | `ollama pull qwen2.5-coder:14b` (or whatever model you set in Settings). |
| Charts show broken images              | The chart file path must live under `output/`. Check the right panel → Logs for backend errors. |
| `tauri:build` fails on Windows         | Install the "Desktop development with C++" workload in Visual Studio 2022 Build Tools. |

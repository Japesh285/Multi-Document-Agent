// Thin wrapper around Tauri APIs with safe fallbacks for plain web dev.

import type { BackendInfo } from "../types";

declare global {
  interface Window {
    __TAURI__?: unknown;
    __TAURI_INTERNALS__?: unknown;
  }
}

export const isTauri =
  typeof window !== "undefined" &&
  (window.__TAURI__ !== undefined || window.__TAURI_INTERNALS__ !== undefined);

async function invokeSafe<T>(cmd: string, args?: Record<string, unknown>): Promise<T | null> {
  if (!isTauri) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return (await invoke(cmd, args)) as T;
  } catch (err) {
    console.error(`tauri invoke ${cmd} failed`, err);
    return null;
  }
}

export const backendInfo    = ()             => invokeSafe<BackendInfo>("backend_info");
export const restartBackend = ()             => invokeSafe<boolean>("backend_restart");
export const backendLogs    = (lines = 200)  => invokeSafe<string[]>("backend_logs", { lines });
export const openExternal   = (url: string)  => invokeSafe<void>("open_external", { url });

// ── Native file picker ────────────────────────────────────────────────────

/** Extensions that the backend's workspace layer understands. Kept in sync with
 *  server.WORKSPACE_EXTENSIONS. */
export const ALL_WORKSPACE_EXTS = [
  // spreadsheets
  "xlsx", "xls", "xlsm", "csv", "tsv", "txt",
  // documents
  "docx",
  // OCR (PDFs + images)
  "pdf", "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff",
];

export const SPREADSHEET_EXTS = ["xlsx", "xls", "xlsm", "csv", "tsv"];
export const DOCUMENT_EXTS    = ["docx"];
export const OCR_EXTS         = ["pdf", "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"];

/** Open the native dialog for any supported file type. */
export async function pickWorkspaceFile(): Promise<string | null> {
  if (!isTauri) return null;
  try {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const result = await open({
      multiple: false,
      filters: [
        { name: "All supported",  extensions: ALL_WORKSPACE_EXTS },
        { name: "Spreadsheets",   extensions: SPREADSHEET_EXTS },
        { name: "Word documents", extensions: DOCUMENT_EXTS },
        { name: "PDFs & images",  extensions: OCR_EXTS },
      ],
    });
    return typeof result === "string" ? result : null;
  } catch (err) {
    console.error("dialog open failed", err);
    return null;
  }
}

/** Back-compat alias used by call sites that historically only opened xlsx. */
export const pickXlsxFile = pickWorkspaceFile;

export async function saveFileDialog(defaultName: string, ext: string): Promise<string | null> {
  if (!isTauri) return null;
  try {
    const { save } = await import("@tauri-apps/plugin-dialog");
    return await save({
      defaultPath: defaultName,
      filters: [{ name: ext.toUpperCase(), extensions: [ext] }],
    });
  } catch (err) {
    console.error("save dialog failed", err);
    return null;
  }
}

// Prevents additional console window on Windows in release
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;

use serde::Serialize;
use std::sync::Mutex;
use tauri::{Manager, State};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

pub struct AppState {
    pub backend: Mutex<backend::BackendManager>,
}

#[derive(Serialize)]
struct BackendInfo {
    running:  bool,
    pid:      Option<u32>,
    port:     u16,
    base_url: String,
    log_tail: Vec<String>,
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

#[tauri::command]
async fn backend_info(state: State<'_, AppState>) -> Result<BackendInfo, String> {
    let mgr = state.backend.lock().map_err(|e| e.to_string())?;
    Ok(BackendInfo {
        running:  mgr.is_running(),
        pid:      mgr.pid(),
        port:     mgr.port(),
        base_url: mgr.base_url(),
        log_tail: mgr.tail_logs(80),
    })
}

#[tauri::command]
async fn backend_restart(state: State<'_, AppState>) -> Result<bool, String> {
    let mut mgr = state.backend.lock().map_err(|e| e.to_string())?;
    mgr.restart().map_err(|e| e.to_string())
}

#[tauri::command]
async fn backend_logs(state: State<'_, AppState>, lines: usize) -> Result<Vec<String>, String> {
    let mgr = state.backend.lock().map_err(|e| e.to_string())?;
    Ok(mgr.tail_logs(lines))
}

#[tauri::command]
async fn open_external(url: String) -> Result<(), String> {
    open::that(url).map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let resource_dir = app
                .path()
                .resource_dir()
                .unwrap_or_else(|_| std::path::PathBuf::from("."));
            let backend = backend::BackendManager::launch(resource_dir);
            app.manage(AppState {
                backend: Mutex::new(backend),
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                if let Some(state) = window.app_handle().try_state::<AppState>() {
                    if let Ok(mut mgr) = state.backend.lock() {
                        mgr.shutdown();
                    }
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            backend_info,
            backend_restart,
            backend_logs,
            open_external
        ])
        .run(tauri::generate_context!())
        .expect("error while running spreadsheet-agent");
}

// Tiny helper for opening URLs without pulling another full crate
mod open {
    pub fn that(url: impl AsRef<str>) -> std::io::Result<()> {
        let url = url.as_ref();
        #[cfg(target_os = "windows")]
        {
            std::process::Command::new("cmd")
                .args(["/C", "start", "", url])
                .spawn()?;
        }
        #[cfg(target_os = "macos")]
        {
            std::process::Command::new("open").arg(url).spawn()?;
        }
        #[cfg(all(unix, not(target_os = "macos")))]
        {
            std::process::Command::new("xdg-open").arg(url).spawn()?;
        }
        Ok(())
    }
}

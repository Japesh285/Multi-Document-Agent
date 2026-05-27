// backend.rs — Process manager for the embedded Python FastAPI server.
//
// On launch:
//   1. Locate the project root (parent of src-tauri/ in dev, resource_dir in prod).
//   2. Pick a Python interpreter — prefer the bundled venv, fall back to system "python".
//   3. Spawn `python -u server.py --port 8765` with stdout/stderr captured to ring buffers.
//   4. Health-check /health for up to ~30s and surface readiness via tail_logs().
//
// On window close → kill the child process tree.

use std::collections::VecDeque;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const DEFAULT_PORT: u16 = 8765;
const LOG_RING_SIZE: usize = 500;

pub struct BackendManager {
    child:        Option<Child>,
    port:         u16,
    project_root: PathBuf,
    logs:         Arc<Mutex<VecDeque<String>>>,
}

impl BackendManager {
    pub fn launch(resource_dir: PathBuf) -> Self {
        let port = pick_port();
        let project_root = resolve_project_root(&resource_dir);

        let logs: Arc<Mutex<VecDeque<String>>> = Arc::new(Mutex::new(VecDeque::with_capacity(LOG_RING_SIZE)));
        push_log(&logs, format!("[mgr] project_root = {}", project_root.display()));
        push_log(&logs, format!("[mgr] starting backend on port {port}"));

        let mut mgr = BackendManager {
            child: None,
            port,
            project_root,
            logs,
        };

        match mgr.spawn() {
            Ok(()) => {
                push_log(&mgr.logs, "[mgr] backend spawned, waiting for /health".into());
                mgr.wait_until_ready(Duration::from_secs(30));
            }
            Err(err) => {
                push_log(&mgr.logs, format!("[mgr] FAILED to spawn backend: {err}"));
            }
        }

        mgr
    }

    pub fn is_running(&self) -> bool {
        self.child.is_some()
    }

    pub fn pid(&self) -> Option<u32> {
        self.child.as_ref().map(|c| c.id())
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    pub fn base_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    pub fn tail_logs(&self, lines: usize) -> Vec<String> {
        if let Ok(guard) = self.logs.lock() {
            let n = guard.len().min(lines);
            guard.iter().rev().take(n).rev().cloned().collect()
        } else {
            vec![]
        }
    }

    pub fn restart(&mut self) -> Result<bool, String> {
        self.shutdown();
        self.spawn().map_err(|e| e.to_string())?;
        Ok(true)
    }

    pub fn shutdown(&mut self) {
        if let Some(mut c) = self.child.take() {
            push_log(&self.logs, format!("[mgr] killing pid {}", c.id()));
            #[cfg(target_os = "windows")]
            {
                // taskkill kills the whole tree (uvicorn + workers)
                let _ = Command::new("taskkill")
                    .args(["/PID", &c.id().to_string(), "/T", "/F"])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();
            }
            #[cfg(not(target_os = "windows"))]
            {
                let _ = c.kill();
            }
            let _ = c.wait();
        }
    }

    // ---------------------------------------------------------------------
    // internals
    // ---------------------------------------------------------------------

    fn spawn(&mut self) -> std::io::Result<()> {
        let py = pick_python(&self.project_root);
        let server = self.project_root.join("server.py");

        push_log(&self.logs, format!("[mgr] python = {}", py.display()));
        push_log(&self.logs, format!("[mgr] server = {}", server.display()));

        if !server.exists() {
            push_log(&self.logs, "[mgr] server.py NOT FOUND — backend cannot start".into());
            return Err(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                format!("server.py not found at {}", server.display()),
            ));
        }

        let mut cmd = Command::new(&py);
        cmd.arg("-u")
            .arg(server.as_os_str())
            .arg("--host")
            .arg("127.0.0.1")
            .arg("--port")
            .arg(self.port.to_string())
            .current_dir(&self.project_root)
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        let mut child = cmd.spawn()?;
        if let Some(stdout) = child.stdout.take() {
            spawn_log_pump(stdout, self.logs.clone(), "out");
        }
        if let Some(stderr) = child.stderr.take() {
            spawn_log_pump(stderr, self.logs.clone(), "err");
        }
        self.child = Some(child);
        Ok(())
    }

    fn wait_until_ready(&self, total: Duration) {
        let deadline = Instant::now() + total;
        let url = format!("{}/health", self.base_url());
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_millis(800))
            .build()
            .unwrap();

        while Instant::now() < deadline {
            if let Ok(resp) = client.get(&url).send() {
                if resp.status().is_success() {
                    push_log(&self.logs, "[mgr] backend ready".into());
                    return;
                }
            }
            thread::sleep(Duration::from_millis(400));
        }
        push_log(&self.logs, "[mgr] backend did not become ready in time".into());
    }
}

impl Drop for BackendManager {
    fn drop(&mut self) {
        self.shutdown();
    }
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

fn pick_port() -> u16 {
    DEFAULT_PORT
}

fn resolve_project_root(resource_dir: &Path) -> PathBuf {
    // Dev: src-tauri/ → walk up to AI_Work.
    if let Ok(cwd) = std::env::current_dir() {
        // tauri dev runs with CWD = src-tauri/, so parent twice gets us to d:\AI_Work
        let candidate = cwd.join("..").join("..");
        if candidate.join("server.py").exists() {
            return canonicalize_or(candidate);
        }
        if cwd.join("server.py").exists() {
            return canonicalize_or(cwd);
        }
    }

    // Bundled: resources are placed alongside the exe via tauri.conf.json -> resources.
    let bundled = resource_dir.join("server.py");
    if bundled.exists() {
        return resource_dir.to_path_buf();
    }

    // Fallback to CWD
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

fn canonicalize_or(p: PathBuf) -> PathBuf {
    std::fs::canonicalize(&p).unwrap_or(p)
}

fn pick_python(project_root: &Path) -> PathBuf {
    // Preferred: bundled venv
    #[cfg(target_os = "windows")]
    {
        let venv_py = project_root.join("venv").join("Scripts").join("python.exe");
        if venv_py.exists() {
            return venv_py;
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        let venv_py = project_root.join("venv").join("bin").join("python");
        if venv_py.exists() {
            return venv_py;
        }
    }

    // Fall back to PATH
    #[cfg(target_os = "windows")]
    {
        return PathBuf::from("python.exe");
    }
    #[cfg(not(target_os = "windows"))]
    {
        return PathBuf::from("python3");
    }
}

fn push_log(logs: &Arc<Mutex<VecDeque<String>>>, line: String) {
    if let Ok(mut guard) = logs.lock() {
        if guard.len() >= LOG_RING_SIZE {
            guard.pop_front();
        }
        guard.push_back(line);
    }
}

fn spawn_log_pump<R: std::io::Read + Send + 'static>(
    stream: R,
    logs: Arc<Mutex<VecDeque<String>>>,
    tag: &'static str,
) {
    thread::spawn(move || {
        let reader = BufReader::new(stream);
        for line in reader.lines().flatten() {
            push_log(&logs, format!("[{tag}] {line}"));
        }
    });
}

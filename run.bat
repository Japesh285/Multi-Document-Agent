@echo off
REM Launch the built desktop app. Sets cwd correctly so the Tauri shell's
REM resolve_project_root() picks up d:\AI_Work\venv + d:\AI_Work\server.py.

setlocal
pushd "%~dp0"

REM Sanity — verify the things the Tauri shell needs.
if not exist "server.py" (
    echo ERROR: server.py not found at %CD%
    echo This script must live in the project root next to server.py.
    pause
    exit /b 1
)
if not exist "venv\Scripts\python.exe" (
    echo ERROR: venv missing. Run setup.bat first.
    pause
    exit /b 1
)
if not exist "desktop\src-tauri\target\release\spreadsheet-agent.exe" (
    echo ERROR: app not built yet. Run build-installer.bat first.
    pause
    exit /b 1
)

REM Free port 8765 if a previous backend got stuck.
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8765 " ^| findstr "LISTENING"') do (
    echo Killing stale backend pid %%P listening on 8765
    taskkill /F /PID %%P >nul 2>nul
)

REM Make sure Ollama is up.
tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul
if errorlevel 1 (
    start "" /B ollama serve
    timeout /t 2 /nobreak >nul
)

REM Launch the Tauri desktop shell. It auto-spawns the Python backend
REM using THIS directory's venv + server.py.
start "" /D "%CD%" "%CD%\desktop\src-tauri\target\release\spreadsheet-agent.exe"

popd
endlocal

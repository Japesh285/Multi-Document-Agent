@echo off
setlocal
cd /d "%~dp0\desktop"

REM Make sure ollama is running in the background.
tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul
if errorlevel 1 (
    start "" /B ollama serve
    timeout /t 2 /nobreak >nul
)

REM Launch the Tauri desktop shell (this auto-starts the Python backend).
call npm run tauri:dev

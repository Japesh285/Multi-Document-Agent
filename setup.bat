@echo off
setlocal
cd /d "%~dp0"

echo.
echo === Spreadsheet Agent — one-time setup ===
echo.

echo [1/4] Creating Python virtual environment...
if not exist "venv\Scripts\python.exe" (
    python -m venv venv || goto :err
) else (
    echo     venv already exists, skipping.
)

echo [2/4] Installing Python dependencies...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt || goto :err

echo [3/4] Installing desktop dependencies (this takes a few minutes)...
cd desktop
call npm install || goto :err
cd ..

echo [4/4] Pulling default Ollama model (qwen2.5-coder:14b ^~9 GB)...
ollama list 2>nul | findstr /i "qwen2.5-coder" >nul
if errorlevel 1 (
    echo     Downloading model...
    ollama pull qwen2.5-coder:14b || echo     (skipped — pull manually later if it failed)
) else (
    echo     Model already present.
)

echo.
echo === Setup complete. ===
echo.
echo Next: double-click  start-app.bat  to launch.
echo.
pause
exit /b 0

:err
echo.
echo *** Setup FAILED. See messages above. ***
pause
exit /b 1

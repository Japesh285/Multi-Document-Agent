@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo === Spreadsheet Agent — environment check ===
echo.

set FAIL=0

call :check "Python"             "python --version"
call :check "Node.js"             "node --version"
call :check "npm"                 "npm --version"
call :check "Rust (rustc)"        "rustc --version"
call :check "Cargo"               "cargo --version"
call :check "Ollama"              "ollama --version"
call :check "MSVC linker"         "where link.exe"

echo.
echo --- Project files ---
call :exists "venv\Scripts\python.exe"        "Python venv"
call :exists "requirements.txt"               "requirements.txt"
call :exists "server.py"                      "server.py"
call :exists "sessions.py"                    "sessions.py"
call :exists "desktop\package.json"           "desktop\package.json"
call :exists "desktop\node_modules"           "desktop\node_modules"
call :exists "desktop\src-tauri\Cargo.toml"   "Cargo.toml"
call :exists "desktop\src-tauri\icons\icon.ico" "desktop icons"

echo.
echo --- Ollama model ---
ollama list 2>nul | findstr /i "qwen2.5-coder" >nul
if errorlevel 1 (
    echo   [MISSING] qwen2.5-coder model not pulled
    echo             Run:  ollama pull qwen2.5-coder:14b
    set FAIL=1
) else (
    echo   [OK]      qwen2.5-coder model present
)

echo.
if "%FAIL%"=="1" (
    echo *** Some checks FAILED. Fix the items marked [MISSING] before running start-app.bat. ***
) else (
    echo *** All checks passed. Run start-app.bat to launch. ***
)
echo.
pause
exit /b %FAIL%

:check
set "name=%~1"
set "cmd=%~2"
%cmd% >nul 2>nul
if errorlevel 1 (
    echo   [MISSING] %name%
    set FAIL=1
) else (
    for /f "tokens=*" %%v in ('%cmd% 2^>nul') do (
        echo   [OK]      %name%  ^(%%v^)
        goto :eof
    )
)
goto :eof

:exists
if exist "%~1" (
    echo   [OK]      %~2
) else (
    echo   [MISSING] %~2  ^(expected at %~1^)
    set FAIL=1
)
goto :eof

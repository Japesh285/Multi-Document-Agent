@echo off
setlocal
cd /d "%~dp0"

echo.
echo === Building Spreadsheet Agent installer (.exe) ===
echo.
echo This will take 3-8 minutes the first time.
echo.

if not exist "desktop\src-tauri\icons\icon.ico" (
    echo Icons missing — regenerating...
    python generate_icons.py || goto :err
)

if not exist "desktop\node_modules" (
    echo node_modules missing — running npm install first...
    cd desktop
    call npm install || goto :err
    cd ..
)

cd desktop
echo Compiling release build...
call npm run tauri:build
if errorlevel 1 goto :err
cd ..

echo.
echo === Build complete. ===
echo.
echo Installer location:
echo   desktop\src-tauri\target\release\bundle\nsis\
echo.
dir /b "desktop\src-tauri\target\release\bundle\nsis\*.exe" 2>nul
echo.
echo Double-click the .exe to install. The installer will offer to create
echo a Desktop shortcut on the last screen.
echo.
pause
exit /b 0

:err
echo.
echo *** Build FAILED. See messages above. ***
pause
exit /b 1

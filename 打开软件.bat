@echo off
set "APP_DIR=%~dp0"
set "BUNDLED_PY=C:\Users\cy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
cd /d "%APP_DIR%"

if exist "%BUNDLED_PY%" (
    start "" "%BUNDLED_PY%" "%APP_DIR%app.py"
    exit /b
)

where py >nul 2>nul
if not errorlevel 1 (
    start "" py -3 "%APP_DIR%app.py"
    exit /b
)

where python >nul 2>nul
if not errorlevel 1 (
    start "" python "%APP_DIR%app.py"
    exit /b
)

echo Could not find Python. Please run build_windows.ps1 to create an exe.
pause

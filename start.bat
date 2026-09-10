@echo off
setlocal
set "PROJECT_ROOT=%~dp0"
set "BACKEND_PYTHON=%PROJECT_ROOT%backend\venv\Scripts\python.exe"

if exist "%BACKEND_PYTHON%" (
    "%BACKEND_PYTHON%" --version >nul 2>&1
    if errorlevel 1 set "BACKEND_PYTHON="
)

if not defined BACKEND_PYTHON (
    where python >nul 2>&1
    if errorlevel 1 (
        echo Python was not found. Install Python or recreate backend\venv.
        pause
        exit /b 1
    )
    set "BACKEND_PYTHON=python"
)

start "Flask" cmd /k "cd /d %PROJECT_ROOT% && %BACKEND_PYTHON% scripts\start_backend.py"
start "Vue"  cmd /k "cd /d %PROJECT_ROOT%frontend && npm run dev"
echo Services starting, wait a few seconds then open http://localhost:5173
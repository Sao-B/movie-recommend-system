@echo off
setlocal
set "PROJECT_ROOT=%~dp0"
set "BACKEND_PYTHON=%PROJECT_ROOT%backend\venv\Scripts\python.exe"
if not exist "%BACKEND_PYTHON%" set "BACKEND_PYTHON=python"

rem 两个服务各开一个独立 cmd 窗口：关闭 WorkBuddy、关掉本窗口都不会杀掉它们。
rem 前端直接用 node 跑 vite，避开 npm 在 Git Bash 下的 bash shim 报错。
start "Flask" cmd /k "cd /d %PROJECT_ROOT%backend && "%BACKEND_PYTHON%" scripts\start_backend.py" & if errorlevel 1 (echo [ERROR] backend exited, check backend/venv and config.yaml & pause)
start "Vue"   cmd /k "cd /d %PROJECT_ROOT%frontend && node node_modules\vite\bin\vite.js --port 5173 --strictPort"

echo Services starting, wait a few seconds then open http://localhost:5173
echo To stop: close the two cmd windows that just popped up.

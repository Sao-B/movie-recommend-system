@echo off
start "Flask" cmd /k "cd /d %~dp0backend && venv\Scripts\python app.py"
start "Vue"  cmd /k "cd /d %~dp0frontend && npm run dev"
echo 服务启动中，稍等几秒后打开浏览器访问 http://localhost:5173

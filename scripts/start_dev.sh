#!/usr/bin/env bash
# start_dev.sh — 同时启动 Flask 后端和 Vue 开发服务器
# 用法：bash scripts/start_dev.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}/.."
LOG_DIR="${PROJECT_ROOT}/logs"

# 创建日志目录
mkdir -p "${LOG_DIR}"

echo "==> 启动 Flask 后端 (http://localhost:5000)..."
cd "${PROJECT_ROOT}/backend"
# 优先使用虚拟环境中的 Python，日志重定向到独立文件
if [ -f "../venv/bin/python" ]; then
    ../venv/bin/python app.py > "${LOG_DIR}/flask.log" 2>&1 &
else
    python app.py > "${LOG_DIR}/flask.log" 2>&1 &
fi
FLASK_PID=$!

echo "==> 启动 Vue 前端 (http://localhost:5173)..."
cd "${PROJECT_ROOT}/frontend"
npm run dev > "${LOG_DIR}/vue.log" 2>&1 &
VUE_PID=$!

echo "==> 两个服务已在后台运行"
echo "    Flask PID: ${FLASK_PID}  (日志: logs/flask.log)"
echo "    Vue   PID: ${VUE_PID}  (日志: logs/vue.log)"
echo "    按 Ctrl+C 同时停止"
echo ""
echo "    实时查看日志："
echo "      tail -f logs/flask.log"
echo "      tail -f logs/vue.log"

# 等待任意子进程退出
trap "kill ${FLASK_PID} ${VUE_PID} 2>/dev/null; exit 0" INT TERM
wait

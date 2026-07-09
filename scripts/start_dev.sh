#!/usr/bin/env bash
# start_dev.sh — 同时启动 Flask 后端和 Vue 开发服务器
# 用法：bash scripts/start_dev.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}/.."

echo "==> 启动 Flask 后端 (http://localhost:5000)..."
cd "${PROJECT_ROOT}/backend"
python app.py &
FLASK_PID=$!

echo "==> 启动 Vue 前端 (http://localhost:5173)..."
cd "${PROJECT_ROOT}/frontend"
npm run dev &
VUE_PID=$!

echo "==> 两个服务已在后台运行"
echo "    Flask PID: ${FLASK_PID}"
echo "    Vue   PID: ${VUE_PID}"
echo "    按 Ctrl+C 同时停止"

# 等待任意子进程退出
trap "kill ${FLASK_PID} ${VUE_PID} 2>/dev/null; exit 0" INT TERM
wait

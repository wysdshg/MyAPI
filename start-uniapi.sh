#!/usr/bin/env bash
# 启动 uni-api 主服务（前台运行，Ctrl+C 停止；端口 9377）
cd "$(dirname "$0")" || exit 1
PORT=9377
if ss -tln "sport = :$PORT" | grep -q ":$PORT "; then
    echo "Main service is ALREADY running."
    echo "API    : http://localhost:$PORT/v1"
    echo "Config : http://localhost:9378"
    echo "Log    : tail -f uni-api-run.log"
    exit 0
fi
PYTHON="python3"
[ -x uni-api/.venv/bin/python ] && PYTHON="uni-api/.venv/bin/python"
export PORT
exec "$PYTHON" uni-api/main.py

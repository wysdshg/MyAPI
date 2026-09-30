#!/usr/bin/env bash
# 启动图形配置页面（后台运行，日志 config-ui-run.log；端口 9378）
cd "$(dirname "$0")" || exit 1
PORT=9378
if ss -tln "sport = :$PORT" | grep -q ":$PORT "; then
    echo "Config UI is ALREADY running.  http://localhost:$PORT"
    exit 0
fi
PYTHON="python3"
[ -x uni-api/.venv/bin/python ] && PYTHON="uni-api/.venv/bin/python"
export UI_PORT=$PORT
nohup "$PYTHON" config-ui.py >> config-ui-run.log 2>&1 &
echo "Config UI started.  http://localhost:$PORT  (log: config-ui-run.log)"

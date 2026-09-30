#!/usr/bin/env bash
# 停止 uni-api 主服务（端口 9377；先优雅退出，5 秒超时强杀）
PORT=9377
PIDS=$(ss -tlnpH "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K\d+' | sort -u)
if [ -z "$PIDS" ]; then
    echo "Main service is not running."
    exit 0
fi
for pid in $PIDS; do kill "$pid" 2>/dev/null; done
for i in $(seq 1 50); do
    ss -tln "sport = :$PORT" | grep -q ":$PORT " || { echo "Stopped."; exit 0; }
    sleep 0.1
done
for pid in $PIDS; do kill -9 "$pid" 2>/dev/null; done
echo "Force stopped."

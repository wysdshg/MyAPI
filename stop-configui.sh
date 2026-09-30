#!/usr/bin/env bash
# 停止图形配置页面（端口 9378；主服务不受影响）
PORT=9378
PIDS=$(ss -tlnpH "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K\d+' | sort -u)
if [ -z "$PIDS" ]; then
    echo "Config UI is not running."
    exit 0
fi
for pid in $PIDS; do kill "$pid" 2>/dev/null; done
for i in $(seq 1 50); do
    ss -tln "sport = :$PORT" | grep -q ":$PORT " || { echo "Stopped."; exit 0; }
    sleep 0.1
done
for pid in $PIDS; do kill -9 "$pid" 2>/dev/null; done
echo "Force stopped."

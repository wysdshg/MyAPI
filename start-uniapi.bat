@echo off
rem uni-api Windows 原生启动脚本（无需 Docker）
rem 服务地址: http://localhost:9377/v1
cd /d E:\MyAPI
netstat -ano | findstr ":9377 .*LISTENING" >nul
if not errorlevel 1 (
    echo 主服务已在运行，无需重复启动。
    echo 调用入口: http://localhost:9377/v1   配置页面: http://localhost:9378
    echo 查看日志: type uni-api-run.log
    pause
    exit /b 0
)
set PORT=9377
python uni-api\main.py
pause

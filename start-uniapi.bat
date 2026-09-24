@echo off
rem Start uni-api main service (console mode, port 9377)
cd /d E:\MyAPI
netstat -ano | findstr /C:":9377 " | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo Main service is ALREADY running.
    echo API    : http://localhost:9377/v1
    echo Config : http://localhost:9378
    echo Log    : type uni-api-run.log
    pause
    exit /b 0
)
set PORT=9377
python uni-api\main.py
pause

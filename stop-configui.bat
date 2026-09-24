@echo off
rem 停止配置页面（端口 9378，主服务不受影响）
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":9378 .*LISTENING"') do taskkill /F /PID %%a 2>nul
echo 配置页面已停止（主服务继续运行）
pause

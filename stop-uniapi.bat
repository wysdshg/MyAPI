@echo off
rem 停止 uni-api 主服务（端口 9377）
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":9377 .*LISTENING"') do taskkill /F /PID %%a 2>nul
echo uni-api 已停止
pause

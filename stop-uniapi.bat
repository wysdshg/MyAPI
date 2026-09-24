@echo off
rem Stop uni-api main service (port 9377)
set KILLED=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:":9377 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1 && set KILLED=1
)
if "%KILLED%"=="1" (echo uni-api stopped.) else (echo uni-api was not running.)
pause

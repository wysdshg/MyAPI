@echo off
rem Stop config UI (port 9378)
set KILLED=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:":9378 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1 && set KILLED=1
)
if "%KILLED%"=="1" (echo Config UI stopped.) else (echo Config UI was not running.)
pause

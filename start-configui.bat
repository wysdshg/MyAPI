@echo off
rem 启动图形配置页面（pythonw 后台运行，本窗口几秒后自动关闭）
cd /d E:\MyAPI
netstat -ano | findstr /C:"127.0.0.1:9378" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo 配置页面已在运行: http://localhost:9378
    timeout /t 3 >nul
    exit /b 0
)
start "" pythonw config-ui.py
timeout /t 3 >nul
netstat -ano | findstr /C:"127.0.0.1:9378" | findstr "LISTENING" >nul
if errorlevel 1 (
    echo 启动失败，错误详情见 configui.log
) else (
    echo 启动成功: http://localhost:9378
)
timeout /t 3 >nul

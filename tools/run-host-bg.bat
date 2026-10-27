@echo off
REM 后台启动 mouse-kvm 主机（无控制台窗口）。
REM 由计划任务调用，也可以直接双击。
cd /d "%~dp0.."

where pythonw >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pythonw not found in PATH.
    pause
    exit /b 1
)

pythonw kvm.py host --name winbox --peer linux=right >> host.log 2>&1

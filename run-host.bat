@echo off
chcp 65001 >nul
cd /d "%~dp0"
title mouse-kvm HOST - do not close this window

REM ============================================================
REM  Run this on the PC that has the physical keyboard + mouse.
REM
REM  --name winbox        : this machine's label (display only)
REM  --peer linux=right   : a slave named "linux" sits on the right
REM  Add more: --peer pc2=left --peer pc3=bottom
REM
REM  Shut down: press Ctrl+C, or just close this window.
REM  Emergency: Ctrl+Alt+Esc brings control back to this PC.
REM ============================================================

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo Install Python 3.9+ and make sure "python" works in cmd.
    pause
    exit /b 1
)

python kvm.py host --name winbox --peer linux=right

echo.
echo Host stopped.
pause

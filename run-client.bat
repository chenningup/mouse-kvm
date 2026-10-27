@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ============================================================
REM  Run this on the PC being controlled (no keyboard needed).
REM
REM  --name ubuntu        : must match the --peer name on the host
REM  --host 192.168.1.20  : IP of the host machine
REM ============================================================

python kvm.py client --name ubuntu --host 192.168.1.20

echo.
echo Client stopped.
pause

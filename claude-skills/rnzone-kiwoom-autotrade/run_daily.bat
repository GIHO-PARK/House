@echo off
REM RN-zone daily auto-trade runner for Kiwoom REST API (Windows Task Scheduler).
REM Run from this folder (rnzone-kiwoom-autotrade). .env must be here too.
REM Schedule: Mon-Fri 23:45 KST, i.e. after the US regular session opens (summer 22:30 / winter 23:30).

setlocal

cd /d "%~dp0"
set LOGFILE=%~dp0daily_run.log
set PYTHONIOENCODING=utf-8

echo. >> "%LOGFILE%"
echo ===== %date% %time% run started ===== >> "%LOGFILE%"

if not exist ".env" (
    echo [ERROR] .env not found. Aborting. >> "%LOGFILE%"
    exit /b 1
)

for /f "usebackq tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"

REM Double safety switch: this variable AND --live must both be set for real orders.
set KIWOOM_LIVE_TRADING=YES

REM Per-order cap in USD. Override with KIWOOM_MAX_ORDER_USD in .env.
REM Default 7000 covers the largest planned order (index 3rd tranche, 9,000,000 KRW).
if not defined KIWOOM_MAX_ORDER_USD set KIWOOM_MAX_ORDER_USD=7000

set PYEXE=python
if exist ".venv\Scripts\python.exe" set PYEXE=.venv\Scripts\python.exe

"%PYEXE%" scripts\kiwoom_autotrade.py --live >> "%LOGFILE%" 2>&1

echo ===== %date% %time% run finished (exit=%errorlevel%) ===== >> "%LOGFILE%"

endlocal

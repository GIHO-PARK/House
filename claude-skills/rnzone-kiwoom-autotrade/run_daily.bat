@echo off
REM RN-zone daily auto-trade runner for Kiwoom REST API (Windows Task Scheduler).
REM Run from this folder (rnzone-kiwoom-autotrade). .env must be here too.

setlocal

cd /d "%~dp0"
set LOGFILE=%~dp0daily_run.log

echo. >> "%LOGFILE%"
echo ===== %date% %time% run started ===== >> "%LOGFILE%"

if not exist ".env" (
    echo [ERROR] .env not found. Aborting. >> "%LOGFILE%"
    exit /b 1
)

for /f "tokens=1,2 delims==" %%a in (.env) do set %%a=%%b

REM Double safety switch: this variable AND --live must both be set for real orders.
set KIWOOM_LIVE_TRADING=YES

REM Per-order cap in USD. Edit the number below to change it.
python scripts\kiwoom_autotrade.py --live --max-order-usd 50 >> "%LOGFILE%" 2>&1

echo ===== %date% %time% run finished (exit=%errorlevel%) ===== >> "%LOGFILE%"

endlocal

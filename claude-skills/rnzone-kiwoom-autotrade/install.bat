@echo off
REM RN-zone auto-trade installer (double-click this file once).
REM 1) find or install Python 3.13  2) create .venv and install kwcli
REM 3) enable wake timers  4) desktop shortcut  5) open the app

setlocal
cd /d "%~dp0"

echo ============================================
echo   RN-zone Kiwoom auto-trade - setup
echo ============================================
echo.

set PY=
py -3.13 -c "import sys" >nul 2>&1 && set PY=py -3.13
if not defined PY (
    py -3.14 -c "import sys" >nul 2>&1 && set PY=py -3.14
)
if not defined PY (
    echo [1/5] Python 3.13 not found. Installing with winget...
    winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements
    py -3.13 -c "import sys" >nul 2>&1 && set PY=py -3.13
)
if not defined PY (
    echo.
    echo [ERROR] Python 3.13 could not be installed automatically.
    echo Install it from the page that opens now, tick "Add python.exe to PATH",
    echo then run install.bat again.
    start https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [1/5] Python OK: %PY%

if not exist ".venv\Scripts\python.exe" (
    echo [2/5] Creating virtual environment...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create .venv
        pause
        exit /b 1
    )
)
echo [2/5] Installing Kiwoom package (kwcli)...
".venv\Scripts\python.exe" -m pip install --upgrade pip kwcli
if errorlevel 1 (
    echo [ERROR] pip install failed. Check the internet connection and run again.
    pause
    exit /b 1
)

echo [3/5] Allowing wake timers (so the PC can wake from sleep at 23:45)...
powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 >nul 2>&1
powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 >nul 2>&1
powercfg /SETACTIVE SCHEME_CURRENT >nul 2>&1

echo [4/5] Creating desktop shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcut.ps1" -AppDir "%~dp0."

echo [5/5] Opening the app...
start "" ".venv\Scripts\pythonw.exe" "%~dp0RNZoneTrader.pyw"

echo.
echo Done. Next time, open "RNZone AutoTrade" on the desktop.
echo.
pause
endlocal

@echo off
setlocal
chcp 65001 >nul
title PawPilot - Launcher
cd /d "%~dp0"

echo.
echo   ============================================
echo    PawPilot  /  first-time launcher
echo   ============================================
echo.

set "PY="

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    goto :run
)

python -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    goto :run
)

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PY=py -3"
    goto :run
)

echo   [x] Python not found on this machine.
echo.

where winget >nul 2>nul
if not errorlevel 1 (
    echo   [*] Trying to install Python 3.12 via winget ...
    echo.
    winget install -e --id Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements
    echo.
    echo   [*] Done. Close this window and run first.bat again.
    echo       ^(PATH needs a fresh shell to pick up Python^)
    pause
    exit /b 0
)

echo   [!] winget not available. Opening python.org for manual install.
echo       Install Python 3.10+ and CHECK "Add python.exe to PATH".
start "" https://www.python.org/downloads/
pause
exit /b 1

:run
echo   [*] Using: %PY%
echo.
%PY% setup_env.py %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo   [!] setup exited with code %RC%
    pause
)
exit /b %RC%

@echo off
setlocal
chcp 65001 >nul
title PawPilot
cd /d "%~dp0"

rem ============================================================
rem  start.bat  -  daily launcher
rem  usage:
rem     start.bat            Web UI in browser (default), auto-opens 16800
rem     start.bat gui        GUI window, silent (no console)
rem     start.bat cli        CLI, opens its own console window
rem     start.bat tui        TUI, opens its own console window
rem     start.bat web        same as default
rem     start.bat -debug     Web with console, keep open on error
rem
rem  Web is the default front end. If it cannot start, agent.py
rem  automatically falls back to the GUI.
rem ============================================================

set "PY=.venv\Scripts\python.exe"
set "PYW=.venv\Scripts\pythonw.exe"

if not exist "%PY%" goto :nosetup
if not exist "data\config.json" goto :noconfig
if not exist "%PYW%" set "PYW=%PY%"

if /i "%~1"=="-debug"  goto :debug
if /i "%~1"=="--debug" goto :debug
if /i "%~1"=="debug"   goto :debug

if /i "%~1"=="gui" goto :gui
if /i "%~1"=="cli" goto :console
if /i "%~1"=="tui" goto :console

rem ---------- Web, default ----------
start "PawPilot - Web" cmd /c chcp 65001 ^>nul ^&^& %PY% agent.py web
exit /b 0

rem ---------- GUI, silent ----------
:gui
start "" "%PYW%" agent.py gui
exit /b 0

rem ---------- console modes ----------
:console
start "PawPilot - %~1" cmd /k chcp 65001 ^>nul ^&^& %PY% agent.py %~1
exit /b 0

rem ---------- debug ----------
:debug
echo.
echo   ==========================================
echo    PawPilot  /  debug console
echo   ==========================================
echo.
echo   [*] Python : %PY%
echo   [*] Config : data\config.json
echo   [*] Closing this window stops the agent.
echo.
"%PY%" agent.py
set "RC=%ERRORLEVEL%"
echo.
echo   [*] agent exited with code %RC%
pause
exit /b %RC%

rem ---------- errors ----------
:nosetup
echo.
echo   [x] .venv not found.
echo       Run first.bat once to build the environment.
echo.
choice /c YN /n /m "  Run first.bat now? [Y/N] "
if errorlevel 2 exit /b 1
call "%~dp0first.bat"
exit /b %ERRORLEVEL%

:noconfig
echo.
echo   [x] data\config.json not found.
echo       Run first.bat to fill in your API settings.
echo.
choice /c YN /n /m "  Run first.bat now? [Y/N] "
if errorlevel 2 exit /b 1
call "%~dp0first.bat"
exit /b %ERRORLEVEL%

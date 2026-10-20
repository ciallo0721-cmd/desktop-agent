@echo off
setlocal
chcp 65001 >nul
title Desktop Agent
cd /d "%~dp0"

rem ============================================================
rem  start.bat  -  daily launcher
rem  usage:
rem     start.bat            GUI, silent (no console window)
rem     start.bat cli        CLI, opens its own console window
rem     start.bat tui        TUI, opens its own console window
rem     start.bat web        web, opens console + http://127.0.0.1:16800
rem     start.bat -debug     GUI with console, keep open on error
rem ============================================================

set "PY=.venv\Scripts\python.exe"
set "PYW=.venv\Scripts\pythonw.exe"

if not exist "%PY%" goto :nosetup
if not exist "data\config.json" goto :noconfig
if not exist "%PYW%" set "PYW=%PY%"

if /i "%~1"=="-debug"  goto :debug
if /i "%~1"=="--debug" goto :debug
if /i "%~1"=="debug"   goto :debug

if /i "%~1"=="cli" goto :console
if /i "%~1"=="tui" goto :console
if /i "%~1"=="web" goto :console

rem ---------- GUI, silent ----------
start "" "%PYW%" agent.py
exit /b 0

rem ---------- console modes ----------
:console
start "Desktop Agent - %~1" cmd /k chcp 65001 ^>nul ^&^& %PY% agent.py %~1
exit /b 0

rem ---------- debug ----------
:debug
echo.
echo   ==========================================
echo    Desktop Agent  /  debug console
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

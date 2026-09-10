@echo off
cd /d "%~dp0"
title Kefu Wechat Bot

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if not "%PYTHON%"=="" set "PY=%PYTHON%"
if not defined PY where python >nul 2>nul && set "PY=python"

if not defined PY (
    echo [ERROR] Python not found. Run the one-click installer first.
    pause
    exit /b 1
)

echo Starting Kefu Wechat Bot ...
echo Panel: http://127.0.0.1:43991  (default password: kefu2026, change it)
echo Close this window to stop the bot.
echo.
"%PY%" run.py
pause

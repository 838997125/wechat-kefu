@echo off
setlocal
cd /d "%~dp0"
title Kefu Wechat Bot (Guard)

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    where python >nul 2>nul && set "PY=python"
)

echo Starting Kefu Wechat Bot ...
echo The bot runs in foreground. It will NOT auto-restart on exit.
echo If it stops because WeChat failed to connect, log into WeChat and start this again manually.
echo.
"%PY%" run.py
echo.
echo Bot has stopped. Check the messages above. Press a key to close.
pause >nul
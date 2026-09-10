@echo off
setlocal
cd /d "%~dp0"
title Kefu Wechat Bot (Guard)

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    where python >nul 2>nul && set "PY=python"
)

:loop
echo [%date% %time%] Starting Kefu Wechat Bot ...
"%PY%" run.py
echo [%date% %time%] Bot exited, restart in 5 seconds ...
timeout /t 5 /nobreak >nul
goto loop
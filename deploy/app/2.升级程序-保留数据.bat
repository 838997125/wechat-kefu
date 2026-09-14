@echo off
chcp 65001 >nul
title Kefu Bot Upgrade (keep data)
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" upgrade.py
if errorlevel 1 pause
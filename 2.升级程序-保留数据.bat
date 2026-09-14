@echo off
chcp 65001 >nul
cd /d "%~dp0app"
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" upgrade.py
if errorlevel 1 pause
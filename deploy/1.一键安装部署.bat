@echo off
chcp 65001 >nul
title Kefu Wechat Bot - Install
cd /d "%~dp0app"

echo ================================================================
echo            Kefu Wechat Bot - One Click Installer
echo ================================================================
echo.

set "PYCMD="
py -3.12 --version >nul 2>nul && set "PYCMD=py -3.12"
if not defined PYCMD py -3.11 --version >nul 2>nul && set "PYCMD=py -3.11"
if not defined PYCMD py -3.10 --version >nul 2>nul && set "PYCMD=py -3.10"
if not defined PYCMD python --version >nul 2>nul && set "PYCMD=python"

if not defined PYCMD (
    echo Python not found. Installing bundled Python 3.12 silently ...
    "%~dp0runtime\python-3.12-amd64.exe" /quiet InstallAllUsers=1 PrependPath=1 Include_test=0 Include_pip=1
    echo Done. Refreshing PATH ...
    set "PATH=%ProgramFiles%\Python312;%ProgramFiles%\Python312\Scripts;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
    if exist "%ProgramFiles%\Python312\python.exe" set "PYCMD=%ProgramFiles%\Python312\python.exe"
)

if not defined PYCMD (
    echo [ERROR] Python install failed. Run runtime\python-3.12-amd64.exe manually, check "Add to PATH", then retry.
    pause
    exit /b 1
)

echo Python: %PYCMD%
echo.
%PYCMD% install.py
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo Install finished OK.) else (echo Install finished with errors, code %RC%.)
pause
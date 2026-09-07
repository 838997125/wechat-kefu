@echo off
chcp 65001 >nul
title 客服微信助手-守护进程
rem ============================================
rem 客服微信助手 · 开机自启守护脚本
rem - 开机/登录后自动运行（由 Windows 计划任务调用）
rem - 先拉起微信，再启动机器人；机器人崩溃退出会自动重启
rem ============================================
cd /d "%~dp0"

set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

rem 1) 确保微信已启动（主窗口）
start "" "D:\APPS\Weixin\Weixin.exe" 2>nul
timeout /t 8 /nobreak >nul

rem 2) 守护循环：机器人退出则自动重启
:loop
echo [%date% %time%] 启动客服微信助手...
"%PYEXE%" "%~dp0run.py"
echo [%date% %time%] 机器人退出，5 秒后自动重启...
timeout /t 5 /nobreak >nul
goto loop

@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 客服微信助手

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    where python >nul 2>nul && set "PY=python"
)

if "%PY%"=="" (
    echo [错误] 未找到 Python，请先安装 Python 3.10-3.12，或联系技术支持使用打包版。
    pause
    exit /b 1
)

echo 正在启动客服微信助手...
echo 启动后浏览器会自动打开管理面板，如未打开请手动访问 http://127.0.0.1:43991
echo 关闭本窗口即停止机器人。
echo.
"%PY%" run.py
pause

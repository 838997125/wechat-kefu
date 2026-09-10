@echo off
setlocal
cd /d "%~dp0"
title Kefu Bot - Cloudflare Tunnel
echo Starting Cloudflare tunnel ...
echo An https://xxxx.trycloudflare.com URL will be shown below.
echo Send that URL to customer-service staff for remote panel access.
echo They will need the panel password to log in.
echo.
if not exist "%~dp0bin\cloudflared.exe" (
    echo [ERROR] Cannot find bin\cloudflared.exe
    pause
    exit /b 1
)
"%~dp0bin\cloudflared.exe" tunnel --url http://127.0.0.1:43991 --no-autoupdate
pause
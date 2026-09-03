# 客服微信助手 - 绿色免安装打包脚本
# 用法（PowerShell 7 / Windows PowerShell 均可）：
#   powershell -ExecutionPolicy Bypass -File build\build.ps1
# 产物：dist\客服微信助手\ 目录（单 exe + 启动 bat），压缩后可直接下发

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

# wxauto4 仅支持 Python 3.9-3.13（3.14 无可用发行版），优先使用项目自带 .venv
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host '创建 .venv 虚拟环境（需要 Python 3.10-3.13）...' -ForegroundColor Cyan
    $candidate = 'C:\Users\Administrator\AppData\Roaming\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe'
    if (Test-Path $candidate) {
        & $candidate -m venv (Join-Path $root '.venv')
    } else {
        py -3.12 -m venv (Join-Path $root '.venv')
    }
    if (-not (Test-Path $py)) { throw '未找到 Python 3.12，请先安装 Python 3.10-3.13' }
}
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet flask wxauto4 pyinstaller

Write-Host '开始打包（PyInstaller onefile）...' -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean --onefile `
    --name "kefu-wechat" `
    --collect-all wxauto4 `
    --collect-all httpx `
    --collect-all httpcore `
    --collect-all certifi `
    --add-data "app;app" `
    --hidden-import flask `
    --hidden-import httpx `
    run.py

$out = Join-Path $root 'dist\客服微信助手'
New-Item -ItemType Directory -Force $out | Out-Null
Copy-Item (Join-Path $root 'dist\kefu-wechat.exe') (Join-Path $out '客服微信助手.exe') -Force

$bat = @'
@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 客服微信助手
echo 正在启动客服微信助手...
echo 浏览器将自动打开管理面板 http://127.0.0.1:43991
echo 关闭本窗口即停止机器人。
"%~dp0客服微信助手.exe"
pause
'@
Set-Content -Path (Join-Path $out '启动客服助手.bat') -Encoding UTF8 -Value $bat

Write-Host ''
Write-Host '打包完成:' -ForegroundColor Green
Write-Host "  $out"
Write-Host '  把该目录压缩成 zip 即可下发：解压 -> 双击「启动客服助手.bat」'

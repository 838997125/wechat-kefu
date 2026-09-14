@echo off
chcp 65001 >nul
echo ============================================
echo   Kefu Bot - Keep PC awake / disable lock
echo ============================================
echo.
net session >nul 2>&1
if %errorlevel% neq 0 echo [NOTE] Not admin: NoLockScreen policy skipped, other settings still applied.

REM Disable sleep / monitor-off / hibernate on AC power
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
REM Do not require password/sign-in after wakeup (AC)
powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_NONE CONSOLELOCK 0
powercfg /SETACTIVE SCHEME_CURRENT

REM Disable screensaver lock (current user)
reg add "HKCU\Control Panel\Desktop" /v ScreenSaverIsSecure /t REG_SZ /d 0 /f >nul
reg add "HKCU\Control Panel\Desktop" /v ScreenSaveActive /t REG_SZ /d 0 /f >nul
REM Disable lock screen entirely (needs admin)
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization" /v NoLockScreen /t REG_DWORD /d 1 /f >nul

echo.
echo Done: sleep=off, wake-unlock=off, screensaver-lock=off.
echo Keep WeChat main window open and logged in.
echo Blank screen is OK; the desktop session must stay unlocked.
echo.
pause
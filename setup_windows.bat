@echo off
setlocal
PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1"
if errorlevel 1 (
    echo.
    echo Setup failed. Read the error above and try again.
    pause
    exit /b 1
)
echo.
pause

@echo off
REM Double-click wrapper for install.ps1.
REM Bypasses the ExecutionPolicy without changing it globally.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 (
    echo.
    echo === Installation interrupted with errors. ===
    pause
    exit /b 1
)
echo.
pause

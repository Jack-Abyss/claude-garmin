@echo off
REM Double-click wrapper for uninstall.ps1.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
if errorlevel 1 (
    echo.
    echo === Uninstall interrupted with errors. ===
    pause
    exit /b 1
)
echo.
pause

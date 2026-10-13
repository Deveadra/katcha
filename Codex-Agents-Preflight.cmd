@echo off
rem Windows host launcher; run after Codex Desktop starts, before WSL agents.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\codex-wsl-agent-preflight.ps1" %*
set "exitCode=%ERRORLEVEL%"
if not "%exitCode%"=="0" (
    echo.
    echo Codex WSL preflight failed. Review the STOP message above.
    pause
)
exit /b %exitCode%

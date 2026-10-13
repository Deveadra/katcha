@echo off
rem Windows host launcher; run after Codex Desktop starts, before WSL agents.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\codex-wsl-agent-preflight.ps1" %*
exit /b %ERRORLEVEL%

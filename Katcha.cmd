@echo off
rem %~dp0 resolves the launcher location even when opened from a WSL UNC path.
powershell.exe -NoProfile -File "%~dp0Katcha.ps1"
if errorlevel 1 pause

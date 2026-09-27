@echo off
rem Place this launcher in a Windows or WSL checkout; WSL runs the application.
wsl.exe --cd "%~dp0" bash ./Katcha.sh
if errorlevel 1 pause

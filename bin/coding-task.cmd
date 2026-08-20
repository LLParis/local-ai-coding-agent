@echo off
setlocal
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0coding-task.ps1" -Task "%~1"
exit /b %ERRORLEVEL%

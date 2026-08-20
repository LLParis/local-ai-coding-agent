@echo off
setlocal
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0doctor.ps1" %*
exit /b %ERRORLEVEL%

@echo off
setlocal
set "PACKAGE_ROOT=%~dp0.."
if defined PYTHONPATH (
  set "PYTHONPATH=%PACKAGE_ROOT%;%PACKAGE_ROOT%\src;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%PACKAGE_ROOT%;%PACKAGE_ROOT%\src"
)

if defined CODING_INTELLIGENCE_PYTHON (
  "%CODING_INTELLIGENCE_PYTHON%" -m agent_continuity.production_worker %*
  exit /b %ERRORLEVEL%
)

where py >nul 2>nul
if not errorlevel 1 (
  py -3 -m agent_continuity.production_worker %*
  exit /b %ERRORLEVEL%
)

python -m agent_continuity.production_worker %*
exit /b %ERRORLEVEL%

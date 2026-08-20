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
if errorlevel 1 goto python_fallback
py -3 -m agent_continuity.production_worker %*
exit /b %ERRORLEVEL%

:python_fallback
python -m agent_continuity.production_worker %*

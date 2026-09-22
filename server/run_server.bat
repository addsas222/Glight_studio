@echo off
REM ============================================================
REM  Start local depth/normal service (Windows)
REM  Run setup_deployment.bat first.
REM  NOTE: ASCII-only on purpose (see setup_deployment.bat).
REM ============================================================
setlocal
cd /d "%~dp0.."

REM Activate the service-only environment created by setup_deployment.bat
if exist .venv-server\Scripts\activate.bat (
  call .venv-server\Scripts\activate.bat
) else (
  echo .venv-server not found. Run setup_deployment.bat first.
  pause
  exit /b 1
)

echo.
echo  Service  : http://127.0.0.1:8765
echo  Health   : http://127.0.0.1:8765/health
echo  Docs     : see the docs\ folder ^(Chinese manuals^)
echo  Press Ctrl+C to stop.
echo.

uvicorn server.app:app --host 127.0.0.1 --port 8765
pause

@echo off
REM ============================================================
REM  Horizon Light Studio - Windows build script
REM  Produces:
REM    dist\HorizonLightStudio.exe   (standalone app, single file)
REM    dist\hls_cli.exe              (CLI renderer, optional)
REM
REM  Optional: with Inno Setup installed, run
REM    packaging\make_installer_windows.bat
REM  to also produce a Setup.exe with Start Menu shortcuts.
REM
REM  NOTE: this file is intentionally ASCII-only. cmd.exe parses .bat using
REM  the system ANSI codepage (GBK/936 on Simplified Chinese Windows), so
REM  UTF-8 Chinese here would be mis-parsed and run as garbage commands.
REM  Chinese documentation lives in the docs\ folder.
REM
REM  Uses a dedicated .venv-build, separate from the dev .venv:
REM  a uv-managed .venv has no pip, so reusing it would fail at pip install.
REM ============================================================
setlocal
cd /d "%~dp0.."

set VENV=.venv-build
echo [1/6] Create/activate build venv (%VENV%)...
if not exist "%VENV%\Scripts\pip.exe" (
  if exist "%VENV%" rmdir /s /q "%VENV%"
  python -m venv "%VENV%"
  if errorlevel 1 goto :fail
)
call "%VENV%\Scripts\activate.bat"

echo [2/6] Install dependencies (WebUI + bundled ffmpeg)...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

echo [3/6] Build frontend...
where node >nul 2>nul
if errorlevel 1 (
  echo.
  echo ERROR: Node.js not found. Node 18+ is required to BUILD the frontend
  echo        ^(not needed at runtime^). Install from https://nodejs.org/
  echo.
  goto :fail
)
pushd webui\frontend
if not exist node_modules (
  call npm install
  if errorlevel 1 ( popd ^& goto :fail )
)
call npm run build
if errorlevel 1 ( popd ^& goto :fail )
popd
if not exist webui\frontend\dist\index.html (
  echo ERROR: frontend build output missing ^(webui\frontend\dist\index.html^).
  goto :fail
)

echo [4/6] Generate app icons...
python packaging\make_icon.py
if errorlevel 1 goto :fail

echo [5/6] Package desktop app and CLI...
pyinstaller packaging\horizon_light_studio.spec --noconfirm --clean
if errorlevel 1 goto :fail

echo [6/6] Done
echo.
echo  Desktop app : dist\HorizonLightStudio.exe
echo  CLI         : dist\hls_cli.exe
echo.
echo  Double-click HorizonLightStudio.exe to run ^(no Python/Node needed^).
echo  User manual ships with the app: see docs\ folder.
pause
exit /b 0

:fail
echo.
echo Build FAILED. See the messages above.
pause
exit /b 1

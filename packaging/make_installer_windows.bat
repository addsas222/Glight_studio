@echo off
REM ============================================================
REM  Build Windows installer with Inno Setup 6 (optional)
REM  Prerequisite: run packaging\build_windows.bat first.
REM  Output: dist\HorizonLightStudio-1.0.0-Setup.exe
REM  NOTE: ASCII-only on purpose (see build_windows.bat).
REM ============================================================
setlocal
cd /d "%~dp0"

set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% (
  echo Inno Setup 6 not found ^(ISCC.exe^).
  echo Install it first: https://jrsoftware.org/isdl.php
  echo After installing, re-run this script.
  echo Alternatively, ship dist\HorizonLightStudio.exe as a portable app.
  pause
  exit /b 1
)

if not exist ..\dist\HorizonLightStudio.exe (
  echo ..\dist\HorizonLightStudio.exe not found. Run build_windows.bat first.
  pause
  exit /b 1
)

%ISCC% make_installer_windows.iss
if errorlevel 1 (
  echo Failed to build the installer.
  pause
  exit /b 1
)

echo.
echo Done. Output: dist\HorizonLightStudio-1.0.0-Setup.exe
pause

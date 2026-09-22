@echo off
REM ============================================================
REM  Local depth service deployment (Windows)
REM  NOTE: this file is intentionally ASCII-only.
REM  cmd.exe reads .bat using the system ANSI codepage (GBK/936 on
REM  Simplified Chinese Windows); UTF-8 Chinese in a .bat gets mis-parsed
REM  and its bytes are executed as commands. Keep this file ASCII.
REM
REM  Uses a dedicated .venv-server, separate from the dev .venv:
REM  .venv may be managed by uv (its own installer, no pip), and
REM  overwriting it with "python -m venv" would break the uv setup.
REM ============================================================
setlocal
cd /d "%~dp0.."

set VENV=.venv-server
echo == 1/3 Create virtual env (%VENV%) ==
if not exist "%VENV%\Scripts\pip.exe" (
  if exist "%VENV%" rmdir /s /q "%VENV%"
  python -m venv "%VENV%"
  if errorlevel 1 goto :fail
)
call "%VENV%\Scripts\activate.bat"

echo == 2/3 Install dependencies ==
python -m pip install --upgrade pip
python -m pip install -r server\requirements.txt
if errorlevel 1 goto :fail

echo == 3/3 Download depth model: Depth Anything V2 Small (~94MB) ==
REM The model is OPTIONAL: without it the app degrades to preview-only depth,
REM and it can be imported later from the app UI. Dependencies are already
REM installed at this point, so a download failure must NOT fail the deploy.
set MODEL_OK=0
python -c "import sys; sys.path.insert(0,'.'); from core.preprocess import download_depth_model; print('Model ready:', download_depth_model())"
if not errorlevel 1 set MODEL_OK=1

if "%MODEL_OK%"=="1" (
  echo.
  echo Deployment finished. Start the service: run_server.bat
) else (
  echo.
  echo ------------------------------------------------------------------
  echo WARNING: model download FAILED ^(huggingface.co is often unreachable here^).
  echo The environment and all dependencies are installed - only the
  echo optional depth model is missing.
  echo.
  echo Three ways to fix it:
  echo   1^) Use a mirror for the download:
  echo        set HLS_DEPTH_MODEL_URL=https://^<mirror^>/model.onnx
  echo        then re-run this script.  ^(MiDaS uses HLS_MODEL_URL^)
  echo   2^) Download the file manually and put it at:
  echo        %%USERPROFILE%%\.horizon_light_studio\models\depth-anything-v2-small.onnx
  echo      ^(MiDaS-small goes in the same folder as model-small.onnx^)
  echo   3^) Start the app and use "Import model..." in the AI backend settings.
  echo.
  echo Without the model the app still runs ^(preview-only depth^).
  echo ------------------------------------------------------------------
  echo.
  echo Deployment finished WITH WARNINGS. Start the service: run_server.bat
)
pause
exit /b 0

:fail
echo.
echo Deployment FAILED. See the messages above.
pause
exit /b 1

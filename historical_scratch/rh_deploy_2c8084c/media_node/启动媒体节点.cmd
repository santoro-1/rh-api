@echo off
setlocal
cd /d "%~dp0.."
set "WORKER_PYTHON=%~dp0.runtime\venv\Scripts\python.exe"
if not exist "%WORKER_PYTHON%" set "WORKER_PYTHON=%CD%\.asr-runtime\venv\Scripts\python.exe"
if not exist "%WORKER_PYTHON%" (
  echo Media node runtime is missing.
  echo Run media_node\install-media-node.ps1 first.
  pause
  exit /b 1
)
"%WORKER_PYTHON%" -m media_node.launcher
if errorlevel 1 pause
endlocal

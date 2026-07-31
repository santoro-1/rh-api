@echo off
setlocal
cd /d "%~dp0"
set "WORKER_PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%WORKER_PYTHON%" set "WORKER_PYTHON=python"
"%WORKER_PYTHON%" -m scripts.remote_media_node
if errorlevel 1 pause
endlocal

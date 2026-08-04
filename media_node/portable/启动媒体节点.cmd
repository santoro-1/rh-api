@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PORTABLE_PYTHON=%~dp0python\python.exe"
if not exist "%PORTABLE_PYTHON%" (
  echo [ERROR] Portable Python is missing. Extract the complete ZIP again.
  pause
  exit /b 1
)
if not exist "%~dp0ffmpeg\bin\ffmpeg.exe" (
  echo [ERROR] Portable FFmpeg is missing. Extract the complete ZIP again.
  pause
  exit /b 1
)
if not exist "%~dp0portable-runtime.txt" (
  echo [ERROR] Runtime version file is missing. Extract the complete ZIP again.
  pause
  exit /b 1
)
if not exist "%~dp0media_node\runtime-required.txt" (
  echo [ERROR] Code version file is missing. Extract the complete ZIP again.
  pause
  exit /b 1
)
fc /b "%~dp0portable-runtime.txt" "%~dp0media_node\runtime-required.txt" >nul
if errorlevel 1 (
  echo [ERROR] Code and runtime versions do not match. Download a new full package.
  pause
  exit /b 1
)
if not exist "%~dp0media_node\.env" (
  copy /y "%~dp0media_node\.env.example" "%~dp0media_node\.env" >nul
  echo Configuration created. Set MEDIA_WORKER_TOKEN, save it, then start again.
  start "" notepad "%~dp0media_node\.env"
  pause
  exit /b 1
)

set "PATH=%~dp0ffmpeg\bin;%PATH%"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "MEDIA_NODE_PORTABLE=1"
set "MEDIA_NODE_PYTHON=%PORTABLE_PYTHON%"

"%PORTABLE_PYTHON%" -m media_node.launcher
if errorlevel 1 pause
endlocal

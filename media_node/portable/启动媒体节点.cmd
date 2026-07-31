@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PORTABLE_PYTHON=%~dp0python\python.exe"
if not exist "%PORTABLE_PYTHON%" (
  echo [错误] 便携包中的 Python 不完整，请重新解压完整 ZIP。
  pause
  exit /b 1
)
if not exist "%~dp0ffmpeg\bin\ffmpeg.exe" (
  echo [错误] 便携包中的 FFmpeg 不完整，请重新解压完整 ZIP。
  pause
  exit /b 1
)
if not exist "%~dp0media_node\.env" (
  copy /y "%~dp0media_node\.env.example" "%~dp0media_node\.env" >nul
  echo 已创建配置文件，请先填写服务器令牌。
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

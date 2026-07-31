@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0media_node\.env" (
  copy /y "%~dp0media_node\.env.example" "%~dp0media_node\.env" >nul
)
start "" notepad "%~dp0media_node\.env"
endlocal

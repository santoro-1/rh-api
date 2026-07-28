@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=D:\Myanaconda\python.exe"
if not exist "%PYTHON%" set "PYTHON=python.exe"
"%PYTHON%" -m scripts.local_services stop
timeout /t 2 /nobreak >nul
endlocal

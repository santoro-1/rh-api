@echo off
setlocal
cd /d "%~dp0"
set "PYTHONW=D:\Myanaconda\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=pythonw.exe"
start "" "%PYTHONW%" -m scripts.local_services run
endlocal

@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
set "PYTHON_EXE=D:\Myanaconda\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python.exe"
"%PYTHON_EXE%" -m scripts.run_h3_prompt_template_test %*
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%EXIT_CODE%"=="0" echo H3 prompt test failed. See the error above.
pause
exit /b %EXIT_CODE%

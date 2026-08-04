@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist "%~dp0python\python.exe" (
  echo [ERROR] This is not a complete portable media-node directory.
  pause
  exit /b 1
)

set "UPDATE_ZIP="
for /f "delims=" %%F in ('dir /b /a-d /o-d "%~dp0rh-media-update-*.zip" 2^>nul') do (
  set "UPDATE_ZIP=%~dp0%%F"
  goto :found
)

echo Stop the media node and copy rh-media-update-*.zip into this directory first.
pause
exit /b 1

:found
echo Applying: %UPDATE_ZIP%
"%~dp0python\python.exe" -s -m media_node.apply_portable_update "%UPDATE_ZIP%" "%~dp0"
if errorlevel 1 (
  pause
  exit /b 1
)
echo Update completed. You can start the media node again.
pause
endlocal

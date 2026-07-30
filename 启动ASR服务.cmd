@echo off
setlocal
cd /d "%~dp0"
set "ASR_PYTHON=%CD%\.asr-runtime\venv\Scripts\python.exe"
if not exist "%ASR_PYTHON%" (
  echo ASR environment is missing.
  echo Read asr_service\README.md and install packages first.
  pause
  exit /b 1
)
set "MODELSCOPE_CACHE=%CD%\.asr-runtime\models"
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
if not defined ASR_MODEL set "ASR_MODEL=paraformer-zh"
if not defined ASR_VAD_MODEL set "ASR_VAD_MODEL=fsmn-vad"
if not defined ASR_DEVICE set "ASR_DEVICE=cpu"
"%ASR_PYTHON%" -m uvicorn asr_service.app:app --host 127.0.0.1 --port 18084
endlocal

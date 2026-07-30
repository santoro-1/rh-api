from __future__ import annotations

import hmac
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import psutil
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import RedirectResponse


app = FastAPI(title="RunningHub Long Audio ASR", version="1")

_MODEL: Any | None = None
_MODEL_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()
_TOKEN_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff]|"
    r"[A-Za-z]+(?:'[A-Za-z]+)?|"
    r"\d+(?:\.\d+)?"
)
_MAX_UPLOAD_BYTES = int(os.getenv("ASR_MAX_AUDIO_MB", "100")) * 1024 * 1024


def _configured_device() -> str:
    return os.getenv("ASR_DEVICE", "cpu").strip().lower() or "cpu"


def _authorize(authorization: str | None) -> None:
    expected = os.getenv("ASR_SHARED_TOKEN", "").strip()
    if not expected:
        return
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization[7:].strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="ASR 访问令牌无效")


def _load_model() -> Any:
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        from funasr import AutoModel

        _MODEL = AutoModel(
            model=os.getenv("ASR_MODEL", "paraformer-zh"),
            vad_model=os.getenv("ASR_VAD_MODEL", "fsmn-vad"),
            device=_configured_device(),
            disable_update=True,
        )
        return _MODEL


def _lexical_units(text: str) -> list[str]:
    return [match.group(0) for match in _TOKEN_RE.finditer(text)]


def _tokens_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    text = str(result.get("text") or "")
    raw_timestamps = result.get("timestamp")
    if not isinstance(raw_timestamps, list) or not raw_timestamps:
        raise HTTPException(status_code=502, detail="FunASR 没有返回字词时间戳")
    units = _lexical_units(text)
    if len(units) != len(raw_timestamps):
        raise HTTPException(
            status_code=502,
            detail=(
                "FunASR 文本与时间戳数量不一致："
                f"textUnits={len(units)}, timestamps={len(raw_timestamps)}"
            ),
        )

    tokens: list[dict[str, Any]] = []
    for position, (unit, raw_timestamp) in enumerate(
        zip(units, raw_timestamps),
        start=1,
    ):
        if (
            not isinstance(raw_timestamp, (list, tuple))
            or len(raw_timestamp) < 2
        ):
            raise HTTPException(
                status_code=502,
                detail=f"FunASR 第 {position} 个时间戳格式错误",
            )
        try:
            start_ms = float(raw_timestamp[0])
            end_ms = float(raw_timestamp[1])
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=502,
                detail=f"FunASR 第 {position} 个时间戳格式错误",
            ) from exc
        tokens.append(
            {
                "text": unit,
                "startSeconds": round(start_ms / 1000, 3),
                "endSeconds": round(end_ms / 1000, 3),
            }
        )
    return tokens


def _save_upload(upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "audio.wav").suffix or ".wav"
    handle = tempfile.NamedTemporaryFile(
        prefix="runninghub-asr-",
        suffix=suffix,
        delete=False,
    )
    path = Path(handle.name)
    written = 0
    try:
        with handle:
            while chunk := upload.file.read(1024 * 1024):
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="音频超过 ASR 上传上限")
                handle.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="音频不能为空")
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "modelLoaded": _MODEL is not None,
        "model": os.getenv("ASR_MODEL", "paraformer-zh"),
        "device": _configured_device(),
    }


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.post("/v1/transcribe")
def transcribe(
    audio: UploadFile = File(...),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _authorize(authorization)
    source = _save_upload(audio)
    process = psutil.Process()
    started = time.perf_counter()
    try:
        with _INFERENCE_LOCK:
            model = _load_model()
            results = model.generate(
                input=str(source),
                batch_size_s=300,
                merge_vad=True,
                merge_length_s=15,
            )
        if not isinstance(results, list) or not results:
            raise HTTPException(status_code=502, detail="FunASR 没有返回识别结果")
        tokens: list[dict[str, Any]] = []
        texts: list[str] = []
        for raw_result in results:
            if not isinstance(raw_result, dict):
                raise HTTPException(status_code=502, detail="FunASR 返回格式错误")
            texts.append(str(raw_result.get("text") or ""))
            tokens.extend(_tokens_from_result(raw_result))
        elapsed = time.perf_counter() - started
        memory = process.memory_info()
        response: dict[str, Any] = {
            "text": "".join(texts),
            "tokens": tokens,
            "processingSeconds": round(elapsed, 3),
            "rssMb": round(memory.rss / 1024 / 1024, 1),
            "model": os.getenv("ASR_MODEL", "paraformer-zh"),
            "device": _configured_device(),
        }
        try:
            import torch

            if torch.cuda.is_available():
                response["cudaPeakMb"] = round(
                    torch.cuda.max_memory_allocated() / 1024 / 1024,
                    1,
                )
        except ImportError:
            pass
        return response
    finally:
        source.unlink(missing_ok=True)

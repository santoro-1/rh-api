from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

import requests

from app.services.alignment.base import AlignmentResult
from app.services.alignment.script_timestamps import (
    RecognizedToken,
    align_script_timeline,
)
from app.services.audio import inspect_audio_duration
from app.services.media_segmentation import MediaSegmentationError


class FunASRHTTPProvider:
    """Timestamp provider backed by the isolated local FunASR service."""

    name = "funasr_http"

    def __init__(
        self,
        *,
        base_url: str,
        shared_token: str = "",
        timeout_seconds: int = 1800,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.shared_token = shared_token.strip()
        self.timeout_seconds = timeout_seconds

    def align(self, audio_path: Path, script: str) -> AlignmentResult:
        if not self.base_url:
            raise MediaSegmentationError("尚未配置 ASR_BASE_URL")
        headers = (
            {"Authorization": f"Bearer {self.shared_token}"}
            if self.shared_token
            else {}
        )
        media_type = (
            mimetypes.guess_type(audio_path.name)[0]
            or "application/octet-stream"
        )
        try:
            with audio_path.open("rb") as audio_stream:
                response = requests.post(
                    f"{self.base_url}/v1/transcribe",
                    headers=headers,
                    files={
                        "audio": (
                            audio_path.name,
                            audio_stream,
                            media_type,
                        )
                    },
                    timeout=(10, self.timeout_seconds),
                )
        except (OSError, requests.RequestException) as exc:
            raise MediaSegmentationError(
                f"ASR 服务连接失败：{exc}"
            ) from exc
        if response.status_code >= 400:
            raise MediaSegmentationError(
                f"ASR 服务返回错误：{_response_error(response)}"
            )
        try:
            payload = response.json()
        except requests.JSONDecodeError as exc:
            raise MediaSegmentationError("ASR 服务返回的不是有效 JSON") from exc
        tokens = _parse_tokens(payload)
        duration = inspect_audio_duration(audio_path)
        alignment = align_script_timeline(script, duration, tokens)
        return AlignmentResult(
            provider=self.name,
            plans=alignment.plans,
            tokens=alignment.tokens,
            match_ratio=alignment.match_ratio,
        )


def _response_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except requests.JSONDecodeError:
        return (response.text or f"HTTP {response.status_code}")[:500]
    if isinstance(payload, dict):
        return str(payload.get("detail") or payload.get("error") or payload)[:500]
    return str(payload)[:500]


def _parse_tokens(payload: Any) -> list[RecognizedToken]:
    if not isinstance(payload, dict) or not isinstance(payload.get("tokens"), list):
        raise MediaSegmentationError("ASR 响应中缺少 tokens")
    tokens: list[RecognizedToken] = []
    for position, raw in enumerate(payload["tokens"], start=1):
        if not isinstance(raw, dict):
            raise MediaSegmentationError(f"ASR 第 {position} 个 token 格式错误")
        try:
            text = str(raw["text"]).strip()
            start = float(raw["startSeconds"])
            end = float(raw["endSeconds"])
            confidence = (
                float(raw["confidence"])
                if raw.get("confidence") is not None
                else None
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaSegmentationError(
                f"ASR 第 {position} 个 token 格式错误"
            ) from exc
        tokens.append(
            RecognizedToken(
                text=text,
                start_seconds=start,
                end_seconds=end,
                confidence=confidence,
            )
        )
    return tokens

from __future__ import annotations

import base64
import binascii
import json
import re
from pathlib import Path
from typing import Any

import requests


SUPPORTED_INPUT_FORMATS = {".mp3", ".m4a", ".wav"}
SUPPORTED_OUTPUT_FORMATS = {"mp3", "wav", "flac"}
MAX_CLONE_FILE_BYTES = 20 * 1024 * 1024
VOICE_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{6,254}[A-Za-z0-9]$")
VOICE_TYPES = {"system", "voice_cloning", "voice_generation", "all"}
MAX_PRONUNCIATION_RULES = 100
MAX_PRONUNCIATION_RULE_CHARACTERS = 500
MAX_PRONUNCIATION_TOTAL_CHARACTERS = 5_000


class MiniMaxAPIError(RuntimeError):
    """MiniMax returned an HTTP, business-status, or response-format error."""


def parse_pronunciation_tones(value: str | None) -> list[str]:
    """Validate the optional batch-level MiniMax ``tone`` JSON array."""

    raw = str(value or "").strip()
    if not raw:
        return []
    if len(raw) > MAX_PRONUNCIATION_TOTAL_CHARACTERS:
        raise ValueError("读音标注总长度不能超过 5,000 个字符")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            '读音标注必须使用 JSON 数组，例如：["燕少飞/(yan4)(shao3)(fei1)"]'
        ) from exc
    if not isinstance(payload, list) or any(
        not isinstance(item, str) for item in payload
    ):
        raise ValueError("读音标注必须是只包含字符串的 JSON 数组")

    rules = [rule.strip() for rule in payload if rule.strip()]
    if len(rules) > MAX_PRONUNCIATION_RULES:
        raise ValueError("一个批次的读音标注不能超过 100 条")
    for rule in rules:
        if len(rule) > MAX_PRONUNCIATION_RULE_CHARACTERS:
            raise ValueError("每条读音标注不能超过 500 个字符")
        if "/" not in rule:
            raise ValueError(
                "读音标注每行必须使用“文字/读音”格式，例如：草地/(cao3)(di1)"
            )
        source, pronunciation = rule.split("/", 1)
        if not source.strip() or not pronunciation.strip():
            raise ValueError(
                "读音标注每行必须同时填写文字和读音，例如：草地/(cao3)(di1)"
            )
    return rules


def validate_voice_id(voice_id: str) -> None:
    if not VOICE_ID_PATTERN.fullmatch(voice_id):
        raise ValueError(
            "voice_id 必须为 8–256 位，以英文字母开头，只含字母、数字、"
            "连字符或下划线，且不能以连字符或下划线结尾"
        )


def validate_clone_audio(path: Path) -> None:
    if not path.is_file():
        raise ValueError("找不到声音参考文件")
    if path.suffix.lower() not in SUPPORTED_INPUT_FORMATS:
        raise ValueError("声音参考文件只支持 MP3、M4A 或 WAV")
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("声音参考文件不能为空")
    if size > MAX_CLONE_FILE_BYTES:
        raise ValueError("声音参考文件不能超过 20 MB")


def validate_synthesis_options(
    *,
    text: str,
    weight_a: int | None = None,
    weight_b: int | None = None,
    speed: float,
    volume: float,
    pitch: int,
    output_format: str,
    maximum_text_characters: int = 9_999,
) -> None:
    if not text.strip():
        raise ValueError("口播脚本不能为空")
    if len(text) > maximum_text_characters:
        raise ValueError(
            f"单条口播脚本不能超过 {maximum_text_characters:,} 个字符"
        )
    if (weight_a is None) != (weight_b is None):
        raise ValueError("融合音色必须同时提供 A、B 权重")
    if weight_a is not None and weight_b is not None:
        if not 1 <= weight_a <= 100 or not 1 <= weight_b <= 100:
            raise ValueError("两个音色权重都必须是 1–100 的整数")
    if not 0.5 <= speed <= 2.0:
        raise ValueError("语速必须在 0.5–2.0 之间")
    if not 0 < volume <= 10:
        raise ValueError("音量必须大于 0 且不超过 10")
    if not -12 <= pitch <= 12:
        raise ValueError("音调必须在 -12–12 之间")
    if output_format not in SUPPORTED_OUTPUT_FORMATS:
        raise ValueError("输出格式只能为 mp3、wav 或 flac")


def decode_audio(encoded_audio: str) -> bytes:
    """Decode documented hex output while retaining base64 compatibility."""

    try:
        return bytes.fromhex(encoded_audio)
    except ValueError:
        try:
            return base64.b64decode(encoded_audio, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise MiniMaxAPIError(
                "语音数据既不是有效的 hex，也不是有效的 base64"
            ) from exc


class MiniMaxClient:
    """Small server-side client extracted from the verified local prototype."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        timeout: float = 120.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("MiniMax API Key 不能为空")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("MiniMax Base URL 必须以 http:// 或 https:// 开头")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def list_voices(self, voice_type: str = "system") -> list[dict[str, Any]]:
        """Read voices available to the configured MiniMax account."""

        if voice_type not in VOICE_TYPES:
            raise ValueError("不支持的 MiniMax 音色类型")
        try:
            response = self.session.post(
                f"{self.base_url}/v1/get_voice",
                json={"voice_type": voice_type},
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = self._check_result(response.json(), "查询可用音色")
        except requests.RequestException as exc:
            raise MiniMaxAPIError(f"查询可用音色请求失败：{exc}") from exc
        except ValueError as exc:
            raise MiniMaxAPIError("查询可用音色返回了无效 JSON") from exc

        key = {
            "system": "system_voice",
            "voice_cloning": "voice_cloning",
            "voice_generation": "voice_generation",
        }.get(voice_type)
        if key is not None:
            values = payload.get(key) or []
        else:
            values = [
                *(payload.get("system_voice") or []),
                *(payload.get("voice_cloning") or []),
                *(payload.get("voice_generation") or []),
            ]
        if not isinstance(values, list):
            raise MiniMaxAPIError("查询可用音色响应格式不正确")
        return [
            value
            for value in values
            if isinstance(value, dict) and str(value.get("voice_id") or "").strip()
        ]

    @staticmethod
    def _check_result(payload: dict[str, Any], operation: str) -> dict[str, Any]:
        base_resp = payload.get("base_resp") or {}
        status_code = int(base_resp.get("status_code", 0) or 0)
        if status_code != 0:
            message = str(base_resp.get("status_msg") or "未知错误")
            trace_id = str(payload.get("trace_id") or "")
            trace = f"，trace_id={trace_id}" if trace_id else ""
            raise MiniMaxAPIError(
                f"{operation}失败：[{status_code}] {message}{trace}"
            )
        return payload

    def upload_clone_audio(self, audio_path: Path) -> int:
        validate_clone_audio(audio_path)
        try:
            with audio_path.open("rb") as audio_file:
                response = self.session.post(
                    f"{self.base_url}/v1/files/upload",
                    data={"purpose": "voice_clone"},
                    files={
                        "file": (
                            audio_path.name,
                            audio_file,
                            "application/octet-stream",
                        )
                    },
                    timeout=self.timeout,
                )
            response.raise_for_status()
            payload = self._check_result(response.json(), "上传声音参考文件")
        except requests.RequestException as exc:
            raise MiniMaxAPIError(f"上传声音参考文件请求失败：{exc}") from exc
        except ValueError as exc:
            raise MiniMaxAPIError("上传声音参考文件返回了无效 JSON") from exc
        try:
            return int(payload["file"]["file_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MiniMaxAPIError("上传成功响应中缺少 file.file_id") from exc

    def _clone_voice_request(
        self,
        file_id: int,
        voice_id: str,
        *,
        noise_reduction: bool = True,
        volume_normalization: bool = True,
        preview_text: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        validate_voice_id(voice_id)
        body: dict[str, Any] = {
            "file_id": file_id,
            "voice_id": voice_id,
            "need_noise_reduction": noise_reduction,
            "need_volume_normalization": volume_normalization,
        }
        if preview_text is not None:
            if not preview_text.strip():
                raise ValueError("试听文案不能为空")
            body["text"] = preview_text
            body["model"] = model or "speech-2.8-turbo"
        try:
            response = self.session.post(
                f"{self.base_url}/v1/voice_clone",
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return self._check_result(
                response.json(), f"克隆音色 {voice_id}"
            )
        except requests.RequestException as exc:
            raise MiniMaxAPIError(f"克隆音色请求失败：{exc}") from exc
        except ValueError as exc:
            raise MiniMaxAPIError("克隆音色返回了无效 JSON") from exc

    def clone_voice(
        self,
        file_id: int,
        voice_id: str,
        *,
        noise_reduction: bool = True,
        volume_normalization: bool = True,
    ) -> str:
        self._clone_voice_request(
            file_id,
            voice_id,
            noise_reduction=noise_reduction,
            volume_normalization=volume_normalization,
        )
        return voice_id

    def clone_voice_with_preview(
        self,
        file_id: int,
        voice_id: str,
        *,
        preview_text: str,
        model: str,
        noise_reduction: bool = True,
        volume_normalization: bool = True,
    ) -> tuple[str, bytes, str]:
        payload = self._clone_voice_request(
            file_id,
            voice_id,
            noise_reduction=noise_reduction,
            volume_normalization=volume_normalization,
            preview_text=preview_text,
            model=model,
        )
        demo_url = str(payload.get("demo_audio") or "")
        if not demo_url:
            raise MiniMaxAPIError("声音克隆成功响应中缺少 demo_audio")
        try:
            response = self.session.get(demo_url, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise MiniMaxAPIError(f"下载克隆试听失败：{exc}") from exc
        return (
            voice_id,
            response.content,
            response.headers.get("content-type") or "audio/mpeg",
        )

    def synthesize_voice(
        self,
        *,
        text: str,
        voice_id: str,
        model: str,
        speed: float,
        volume: float,
        pitch: int,
        language_boost: str,
        output_format: str,
        sample_rate: int = 32000,
        bitrate: int = 128000,
    ) -> tuple[bytes, dict[str, Any]]:
        validate_voice_id(voice_id)
        validate_synthesis_options(
            text=text,
            speed=speed,
            volume=volume,
            pitch=pitch,
            output_format=output_format,
        )
        body = {
            "model": model,
            "text": text,
            "stream": False,
            "output_format": "hex",
            "language_boost": language_boost,
            "voice_setting": {
                "voice_id": voice_id,
                "speed": speed,
                "vol": volume,
                "pitch": pitch,
            },
            "audio_setting": {
                "sample_rate": sample_rate,
                "bitrate": bitrate,
                "format": output_format,
                "channel": 1,
            },
        }
        return self._synthesize(body, "语音生成")

    def create_async_speech_task(
        self,
        *,
        text: str,
        voice_id: str,
        model: str,
        speed: float,
        volume: float,
        pitch: int,
        language_boost: str,
        output_format: str,
        pronunciation_tones: list[str] | None = None,
        sample_rate: int = 32000,
        bitrate: int = 128000,
    ) -> tuple[str, str | None, dict[str, Any]]:
        """Submit one long-form TTS job and return its durable provider IDs."""

        validate_voice_id(voice_id)
        validate_synthesis_options(
            text=text,
            speed=speed,
            volume=volume,
            pitch=pitch,
            output_format=output_format,
            maximum_text_characters=50_000,
        )
        body = {
            "model": model,
            "text": text,
            "language_boost": language_boost,
            "voice_setting": {
                "voice_id": voice_id,
                "speed": speed,
                "vol": volume,
                "pitch": pitch,
            },
            "audio_setting": {
                "audio_sample_rate": sample_rate,
                "bitrate": bitrate,
                "format": output_format,
                "channel": 1,
            },
        }
        if pronunciation_tones:
            body["pronunciation_dict"] = {"tone": list(pronunciation_tones)}
        try:
            response = self.session.post(
                f"{self.base_url}/v1/t2a_async_v2",
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = self._check_result(
                response.json(), "提交异步语音生成任务"
            )
        except requests.RequestException as exc:
            raise MiniMaxAPIError(f"提交异步语音生成请求失败：{exc}") from exc
        except ValueError as exc:
            raise MiniMaxAPIError("提交异步语音生成返回了无效 JSON") from exc
        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            raise MiniMaxAPIError("异步语音生成响应中缺少 task_id")
        file_id = str(payload.get("file_id") or "").strip() or None
        return task_id, file_id, payload

    def query_async_speech_task(
        self,
        task_id: str,
    ) -> tuple[str, str | None, dict[str, Any]]:
        if not str(task_id).strip():
            raise ValueError("MiniMax 异步语音任务 ID 不能为空")
        try:
            response = self.session.get(
                f"{self.base_url}/v1/query/t2a_async_query_v2",
                params={"task_id": task_id},
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = self._check_result(
                response.json(), "查询异步语音生成任务"
            )
        except requests.RequestException as exc:
            raise MiniMaxAPIError(f"查询异步语音生成请求失败：{exc}") from exc
        except ValueError as exc:
            raise MiniMaxAPIError("查询异步语音生成返回了无效 JSON") from exc
        status = str(payload.get("status") or "").strip().lower()
        if status not in {"processing", "success", "failed", "expired"}:
            raise MiniMaxAPIError(f"MiniMax 返回了未知异步任务状态：{status or '空'}")
        file_id = str(payload.get("file_id") or "").strip() or None
        return status, file_id, payload

    def download_file_content(self, file_id: str) -> bytes:
        if not str(file_id).strip():
            raise ValueError("MiniMax 文件 ID 不能为空")
        try:
            response = self.session.get(
                f"{self.base_url}/v1/files/retrieve_content",
                params={"file_id": file_id},
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise MiniMaxAPIError(f"下载异步语音结果失败：{exc}") from exc
        if not response.content:
            raise MiniMaxAPIError("MiniMax 返回了空的异步语音结果")
        return response.content

    def _synthesize(
        self,
        body: dict[str, Any],
        operation: str,
    ) -> tuple[bytes, dict[str, Any]]:
        try:
            response = self.session.post(
                f"{self.base_url}/v1/t2a_v2",
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = self._check_result(response.json(), operation)
        except requests.RequestException as exc:
            raise MiniMaxAPIError(f"{operation}请求失败：{exc}") from exc
        except ValueError as exc:
            raise MiniMaxAPIError("语音生成返回了无效 JSON") from exc
        encoded_audio = str((payload.get("data") or {}).get("audio") or "")
        if not encoded_audio:
            raise MiniMaxAPIError("语音生成成功响应中缺少 data.audio")
        return decode_audio(encoded_audio), payload

    def synthesize_blended_voice(
        self,
        *,
        text: str,
        voice_id_a: str,
        voice_id_b: str,
        weight_a: int,
        weight_b: int,
        model: str,
        speed: float,
        volume: float,
        pitch: int,
        language_boost: str,
        output_format: str,
        sample_rate: int = 32000,
        bitrate: int = 128000,
    ) -> tuple[bytes, dict[str, Any]]:
        validate_voice_id(voice_id_a)
        validate_voice_id(voice_id_b)
        validate_synthesis_options(
            text=text,
            weight_a=weight_a,
            weight_b=weight_b,
            speed=speed,
            volume=volume,
            pitch=pitch,
            output_format=output_format,
        )
        body = {
            "model": model,
            "text": text,
            "stream": False,
            "output_format": "hex",
            "language_boost": language_boost,
            "voice_setting": {
                "voice_id": voice_id_a,
                "speed": speed,
                "vol": volume,
                "pitch": pitch,
            },
            "timbre_weights": [
                {"voice_id": voice_id_a, "weight": weight_a},
                {"voice_id": voice_id_b, "weight": weight_b},
            ],
            "audio_setting": {
                "sample_rate": sample_rate,
                "bitrate": bitrate,
                "format": output_format,
                "channel": 1,
            },
        }
        return self._synthesize(body, "融合音色语音生成")

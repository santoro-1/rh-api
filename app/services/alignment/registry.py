from __future__ import annotations

from app.config import get_settings
from app.services.alignment.base import AudioAlignmentProvider
from app.services.alignment.funasr_http import FunASRHTTPProvider
from app.services.alignment.heuristic import HeuristicAlignmentProvider


_PROVIDERS: dict[str, AudioAlignmentProvider] = {
    HeuristicAlignmentProvider.name: HeuristicAlignmentProvider(),
}


def register_alignment_provider(provider: AudioAlignmentProvider) -> None:
    """Allow a future local or remote ASR adapter without changing routes."""

    name = str(provider.name).strip()
    if not name:
        raise ValueError("对齐服务名称不能为空")
    _PROVIDERS[name] = provider


def get_alignment_provider(name: str) -> AudioAlignmentProvider:
    if name in _PROVIDERS:
        return _PROVIDERS[name]
    if name == FunASRHTTPProvider.name:
        settings = get_settings()
        return FunASRHTTPProvider(
            base_url=settings.asr_base_url,
            shared_token=settings.asr_shared_token,
            timeout_seconds=settings.asr_request_timeout_seconds,
        )
    raise ValueError(f"未知的音频对齐服务：{name}")

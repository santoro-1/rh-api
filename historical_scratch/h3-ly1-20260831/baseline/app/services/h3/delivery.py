from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit


H3_DIRECT_DELIVERY_MODE = "runninghub_direct"
H3_DIRECT_DELIVERY_CONTRACT_VERSION = "h3.output.runninghub-direct.v1"


def validate_h3_provider_video_url(value: object) -> str:
    """Accept one HTTPS provider result without exposing provider credentials."""

    url = str(value or "").strip()
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("RunningHub 返回了不安全的 H3 视频地址")
    return url


def build_h3_direct_output_metadata(
    *,
    provider_task_id: str | None,
    provider_url: str,
    output_type: str,
    source_metadata: dict[str, Any],
    received_at: datetime,
) -> dict[str, Any]:
    clean_url = validate_h3_provider_video_url(provider_url)
    identity_payload = {
        "provider_task_id": str(provider_task_id or ""),
        "provider_url": clean_url,
        "output_type": str(output_type or "mp4").lower().lstrip("."),
    }
    result_signature = hashlib.sha256(
        json.dumps(
            identity_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "quality_variant": "h3_provider_direct",
        "output_contract_version": H3_DIRECT_DELIVERY_CONTRACT_VERSION,
        "provider_audio_preserved": True,
        "provider_audio_head_trimmed": False,
        "provider_duration_preserved": True,
        "head_trim": {
            "enabled": False,
            "mode": "disabled",
            "trim_seconds": 0.0,
            "fallback_reason": "feature_disabled",
        },
        "delivery": {
            "mode": H3_DIRECT_DELIVERY_MODE,
            "download_url": clean_url,
            "result_signature": result_signature,
            "provider_task_id": str(provider_task_id or "") or None,
            "output_type": identity_payload["output_type"],
            "received_at": received_at.isoformat(),
        },
        "source": source_metadata,
    }


def parse_h3_direct_delivery(value: object) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    delivery = value.get("delivery")
    if not isinstance(delivery, dict):
        return None
    if str(delivery.get("mode") or "") != H3_DIRECT_DELIVERY_MODE:
        return None
    try:
        download_url = validate_h3_provider_video_url(delivery.get("download_url"))
    except ValueError:
        return None
    result_signature = str(delivery.get("result_signature") or "").strip().lower()
    if len(result_signature) != 64 or any(
        character not in "0123456789abcdef" for character in result_signature
    ):
        return None
    return {
        **delivery,
        "mode": H3_DIRECT_DELIVERY_MODE,
        "download_url": download_url,
        "result_signature": result_signature,
    }

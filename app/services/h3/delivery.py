from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit


H3_DIRECT_DELIVERY_MODE = "runninghub_direct"
H3_DIRECT_DELIVERY_CONTRACT_VERSION = "h3.output.runninghub-direct.v2"


def resolve_h3_provider_video_ips(value: object) -> tuple[str, ...]:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    hostname = str(parsed.hostname or "").rstrip(".").lower()
    if not hostname:
        raise ValueError("RunningHub H3 视频地址缺少主机")
    try:
        answers = socket.getaddrinfo(
            hostname, parsed.port or 443, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise ValueError("RunningHub H3 视频地址暂时无法解析") from exc
    approved: list[str] = []
    for answer in answers:
        resolved = ipaddress.ip_address(answer[4][0])
        if not resolved.is_global:
            raise ValueError(
                "RunningHub H3 视频地址解析到了本机、内网或保留地址"
            )
        approved.append(str(resolved))
    return tuple(dict.fromkeys(approved))


def validate_h3_provider_video_url(
    value: object,
    *,
    allowed_hosts: tuple[str, ...] = (),
    resolve_dns: bool = False,
) -> str:
    """Accept one HTTPS provider result without exposing provider credentials."""

    url = str(value or "").strip()
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("RunningHub 返回了不安全的 H3 视频地址")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("RunningHub 返回了无效的 H3 视频端口") from exc
    if port not in {None, 443}:
        raise ValueError("RunningHub H3 视频地址只能使用 HTTPS 标准端口")
    hostname = parsed.hostname.rstrip(".").lower()
    normalized_allowed = tuple(
        str(item or "").strip().lower().lstrip(".")
        for item in allowed_hosts
        if str(item or "").strip()
    )
    if normalized_allowed and not any(
        hostname == allowed or hostname.endswith("." + allowed)
        for allowed in normalized_allowed
    ):
        raise ValueError("RunningHub H3 视频地址不在允许的供应商主机范围内")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("RunningHub H3 视频地址不能指向本机、内网或保留地址")
    if resolve_dns:
        resolve_h3_provider_video_ips(url)
    return url


def build_h3_direct_output_metadata(
    *,
    provider_task_id: str | None,
    provider_url: str,
    output_type: str,
    source_metadata: dict[str, Any],
    received_at: datetime,
    allowed_hosts: tuple[str, ...] = (),
    resolve_dns: bool = False,
) -> dict[str, Any]:
    clean_url = validate_h3_provider_video_url(
        provider_url,
        allowed_hosts=allowed_hosts,
        resolve_dns=resolve_dns,
    )
    content_hash = next(
        (
            str(source_metadata.get(key) or "").strip().lower()
            for key in ("sha256", "contentSha256", "content_hash", "md5")
            if str(source_metadata.get(key) or "").strip()
        ),
        "",
    )
    output_node_id = source_metadata.get("nodeId")
    if output_node_id is None:
        output_node_id = source_metadata.get("node_id")
    output_index = source_metadata.get("outputIndex")
    if output_index is None:
        output_index = source_metadata.get("output_index")
    if output_index is None:
        output_index = source_metadata.get("index")
    identity_payload = {
        "provider_task_id": str(provider_task_id or ""),
        "output_node_id": str(output_node_id if output_node_id is not None else ""),
        "output_index": str(output_index if output_index is not None else ""),
        "output_type": str(output_type or "mp4").lower().lstrip("."),
        "provider_content_hash": content_hash,
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
            "delivery_url_revision": hashlib.sha256(
                clean_url.encode("utf-8")
            ).hexdigest(),
            "provider_task_id": str(provider_task_id or "") or None,
            "output_node_id": identity_payload["output_node_id"] or None,
            "output_index": identity_payload["output_index"] or None,
            "output_type": identity_payload["output_type"],
            "provider_content_hash": content_hash or None,
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

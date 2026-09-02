from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.h3.delivery import (
    build_h3_direct_output_metadata,
    parse_h3_direct_delivery,
    validate_h3_provider_video_url,
)


def test_h3_direct_delivery_metadata_round_trip() -> None:
    metadata = build_h3_direct_output_metadata(
        provider_task_id="rh-task-1",
        provider_url="https://files.example/output/h3.mp4",
        output_type="mp4",
        source_metadata={"nodeId": "387", "outputType": "mp4"},
        received_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    delivery = parse_h3_direct_delivery(metadata)
    assert delivery is not None
    assert delivery["mode"] == "runninghub_direct"
    assert delivery["provider_task_id"] == "rh-task-1"
    assert len(delivery["result_signature"]) == 64
    assert len(delivery["delivery_url_revision"]) == 64


def test_signed_url_refresh_does_not_change_stable_result_signature() -> None:
    common = {
        "provider_task_id": "rh-task-1",
        "output_type": "mp4",
        "source_metadata": {
            "nodeId": "387",
            "outputIndex": 0,
            "outputType": "mp4",
        },
        "received_at": datetime(2026, 8, 29, tzinfo=timezone.utc),
    }
    first = build_h3_direct_output_metadata(
        provider_url="https://files.example/output/h3.mp4?token=old",
        **common,
    )["delivery"]
    refreshed = build_h3_direct_output_metadata(
        provider_url="https://files.example/output/h3.mp4?token=new",
        **common,
    )["delivery"]
    assert first["result_signature"] == refreshed["result_signature"]
    assert first["delivery_url_revision"] != refreshed["delivery_url_revision"]


@pytest.mark.parametrize(
    "url",
    [
        "http://files.example/h3.mp4",
        "file:///tmp/h3.mp4",
        "https://user:password@files.example/h3.mp4",
        "https://127.0.0.1/h3.mp4",
        "https://10.0.0.2/h3.mp4",
        "https://files.example:8443/h3.mp4",
        "https://files.example/h3.mp4#fragment",
        "",
    ],
)
def test_h3_direct_delivery_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError):
        validate_h3_provider_video_url(url)


def test_h3_direct_delivery_enforces_configured_provider_hosts() -> None:
    assert validate_h3_provider_video_url(
        "https://bucket.cos.ap-beijing.myqcloud.com/h3.mp4",
        allowed_hosts=("myqcloud.com",),
    )
    with pytest.raises(ValueError, match="主机范围"):
        validate_h3_provider_video_url(
            "https://attacker.example/h3.mp4",
            allowed_hosts=("myqcloud.com",),
        )


def test_h3_direct_delivery_rejects_private_dns_answer(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.h3.delivery.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("169.254.169.254", 443))
        ],
    )
    with pytest.raises(ValueError, match="内网或保留地址"):
        validate_h3_provider_video_url(
            "https://files.example/h3.mp4", resolve_dns=True
        )

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


@pytest.mark.parametrize(
    "url",
    [
        "http://files.example/h3.mp4",
        "file:///tmp/h3.mp4",
        "https://user:password@files.example/h3.mp4",
        "",
    ],
)
def test_h3_direct_delivery_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError):
        validate_h3_provider_video_url(url)

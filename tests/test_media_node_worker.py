from __future__ import annotations

from contextlib import nullcontext

from app.services.media_segmentation import SegmentPlan
from media_node import worker


class _Client:
    def __init__(self) -> None:
        self.completed: tuple[SegmentPlan, ...] | None = None

    def download(self, _url, target):
        target.write_bytes(b"ID3audio")
        return target.stat().st_size

    def complete_analysis(
        self,
        _job_id,
        _lease_id,
        _provider,
        plans,
        _metrics,
        **_kwargs,
    ):
        self.completed = plans

    def fail(self, *_args, **_kwargs):
        raise AssertionError("数字人分析不应失败")


def test_remote_digital_human_keeps_its_own_segment_limits(
    monkeypatch,
    tmp_path,
):
    captured: dict[str, float] = {}

    def fake_plan(_duration, _silences, **kwargs):
        captured.update(kwargs)
        return [
            SegmentPlan(
                index=1,
                script_text="",
                start_seconds=0.0,
                end_seconds=30.0,
                alignment_method="vad_silence",
            )
        ]

    monkeypatch.setattr(worker, "HeartbeatLoop", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(worker, "detect_silence_midpoints", lambda _path: [29.8])
    monkeypatch.setattr(worker, "plan_silence_segments", fake_plan)

    client = _Client()
    worker.process_job(
        client,
        {
            "jobId": "digital-human-segment-limit",
            "leaseId": "lease-1",
            "action": "analysis",
            "workflowType": "digital_human",
            "durationSeconds": 70.0,
            "source": {
                "audioUrl": "/source/audio",
                "audioName": "source.mp3",
            },
        },
        work_root=tmp_path,
        heartbeat_seconds=60,
    )

    assert captured == {
        "target_segment_seconds": 30.0,
        "max_segment_seconds": 32.8,
    }
    assert client.completed is not None
    assert client.completed[0].duration_seconds == 30.0

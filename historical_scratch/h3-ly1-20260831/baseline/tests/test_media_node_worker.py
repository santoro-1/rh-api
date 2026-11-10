from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

from app.services.media_segmentation import SegmentPlan
from app.services.h3.postprocess import H3HeadTrimDecision
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


def test_remote_h3_head_trim_downloads_video_and_returns_decision(
    monkeypatch,
    tmp_path,
):
    class _H3Client:
        def __init__(self) -> None:
            self.decision = None

        def download(self, _url, target):
            target.write_bytes(b"h3-video")
            return target.stat().st_size

        def complete_h3_asr(
            self, _job_id, _lease_id, decision, _metrics, *, action
        ):
            assert action == "h3_head_trim"
            self.decision = decision

        def fail(self, *_args, **_kwargs):
            raise AssertionError("H3 片头 ASR 不应失败")

    class _Provider:
        def __init__(self, **_kwargs):
            pass

        def align(self, _audio, _script):
            return object()

    monkeypatch.setattr(worker, "HeartbeatLoop", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(
        worker,
        "extract_h3_audio_for_alignment",
        lambda _video, audio: audio.write_bytes(b"wav"),
    )
    monkeypatch.setattr(worker, "FunASRHTTPProvider", _Provider)
    monkeypatch.setattr(
        worker,
        "decide_h3_head_trim",
        lambda _script, _alignment: H3HeadTrimDecision(
            mode="asr_adaptive",
            trim_seconds=0.18,
            first_script_token_start_seconds=0.22,
            alignment_provider="funasr_http",
            alignment_match_ratio=1.0,
            matched_prefix_tokens=3,
            fallback_reason=None,
        ),
    )
    client = _H3Client()
    worker.process_job(
        client,
        {
            "jobId": "h3-head-trim-job",
            "leaseId": "lease-1",
            "action": "h3_head_trim",
            "scriptText": "你好世界",
            "source": {
                "videoUrl": "/source/video",
                "videoName": "result.mp4",
            },
        },
        work_root=tmp_path,
        heartbeat_seconds=60,
    )
    assert client.decision["mode"] == "asr_adaptive"
    assert client.decision["trimSeconds"] == 0.18


def test_remote_h3_audio_alignment_uses_local_asr_and_returns_tokens(
    monkeypatch,
    tmp_path,
):
    class _AlignmentClient:
        def __init__(self) -> None:
            self.alignment = None

        def download(self, _url, target):
            target.write_bytes(b"ID3audio")
            return target.stat().st_size

        def complete_h3_asr(
            self, _job_id, _lease_id, alignment, _metrics, *, action
        ):
            assert action == "h3_audio_alignment"
            self.alignment = alignment

        def fail(self, *_args, **_kwargs):
            raise AssertionError("H3 音频对齐不应失败")

    class _Provider:
        def __init__(self, **kwargs):
            assert kwargs["base_url"] == "http://127.0.0.1:18084"

        def align(self, _audio, script):
            assert script == "你好世界"
            return SimpleNamespace(
                provider="funasr_http",
                match_ratio=1.0,
                plans=(),
                tokens=(
                    SimpleNamespace(
                        text="你好",
                        script_start=0,
                        script_end=2,
                        start_seconds=0.1,
                        end_seconds=1.0,
                        confidence=0.99,
                    ),
                    SimpleNamespace(
                        text="世界",
                        script_start=2,
                        script_end=4,
                        start_seconds=1.0,
                        end_seconds=2.0,
                        confidence=0.98,
                    ),
                ),
            )

    monkeypatch.setattr(worker, "HeartbeatLoop", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(worker, "FunASRHTTPProvider", _Provider)
    monkeypatch.setenv("ASR_BASE_URL", "http://127.0.0.1:18084")
    client = _AlignmentClient()
    worker.process_job(
        client,
        {
            "jobId": "h3-audio-alignment-job",
            "leaseId": "lease-2",
            "action": "h3_audio_alignment",
            "scriptText": "你好世界",
            "source": {
                "audioUrl": "/source/audio",
                "audioName": "final.mp3",
            },
        },
        work_root=tmp_path,
        heartbeat_seconds=60,
    )
    assert client.alignment["schema"] == "runninghub.h3-audio-alignment-result.v1"
    assert len(client.alignment["tokens"]) == 2

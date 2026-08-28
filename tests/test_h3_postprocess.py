from __future__ import annotations

from pathlib import Path

import pytest

from app.services.alignment.base import AlignmentResult
from app.services.alignment.script_timestamps import AlignedScriptToken
from app.services.h3 import postprocess as h3_postprocess


SCRIPT = "今天天气很好"


def _alignment(*, include_prefix: int = 3) -> AlignmentResult:
    tokens = tuple(
        AlignedScriptToken(
            text=char,
            script_start=index,
            script_end=index + 1,
            start_seconds=0.230 + index * 0.120,
            end_seconds=0.310 + index * 0.120,
            confidence=0.99,
        )
        for index, char in enumerate(SCRIPT[:include_prefix])
    )
    return AlignmentResult(
        provider="funasr_http",
        plans=(),
        tokens=tokens,
        match_ratio=0.95,
    )


class _AlignmentProvider:
    name = "funasr_http"

    def __init__(self, result: AlignmentResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple[Path, str]] = []

    def align(self, audio_path: Path, script: str) -> AlignmentResult:
        self.calls.append((audio_path, script))
        assert audio_path.suffix == ".wav"
        assert audio_path.is_file()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_h3_postprocess_uses_asr_prefix_to_trim_audio_and_video_then_extracts_anchor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "provider.mp4"
    source.write_bytes(b"provider-video")
    commands: list[list[str]] = []

    def fake_run(command: list[str], message: str) -> None:
        del message
        commands.append(command)
        Path(command[-1]).write_bytes(b"generated-output")

    provider = _AlignmentProvider(_alignment())
    monkeypatch.setattr(h3_postprocess, "_run", fake_run)
    monkeypatch.setattr(h3_postprocess, "inspect_media_duration", lambda _path: 1.04)
    result = h3_postprocess.postprocess_h3_result(
        source,
        script_text=SCRIPT,
        alignment_provider=provider,
        needs_continuity_anchor=True,
        head_trim_enabled=True,
    )

    extract_command, normalize_command, anchor_command = commands
    assert extract_command[extract_command.index("-map") + 1] == "0:a:0"
    filter_graph = normalize_command[
        normalize_command.index("-filter_complex") + 1
    ]
    assert "trim=start=0.190000" in filter_graph
    assert "atrim=start=0.190000" in filter_graph
    assert normalize_command[normalize_command.index("-map") + 1] == "[v]"
    second_map = normalize_command.index(
        "-map", normalize_command.index("-map") + 1
    )
    assert normalize_command[second_map + 1] == "[a]"
    assert anchor_command[anchor_command.index("-sseof") + 1] == "-0.050"
    assert anchor_command[anchor_command.index("-i") + 1] == str(result.video_path)
    assert result.head_trim.mode == "asr_adaptive"
    assert result.head_trim.trim_seconds == pytest.approx(0.19)
    assert result.head_trim.matched_prefix_tokens == 3
    assert result.normalized_duration_seconds == pytest.approx(1.04)
    assert result.video_path.is_file()
    assert result.anchor_path is not None and result.anchor_path.is_file()
    assert not source.with_name("provider.head-align.wav").exists()


def test_h3_postprocess_falls_back_to_fixed_300ms_when_asr_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "provider.mp4"
    source.write_bytes(b"provider-video")
    commands: list[list[str]] = []

    def fake_run(command: list[str], message: str) -> None:
        del message
        commands.append(command)
        Path(command[-1]).write_bytes(b"generated-output")

    provider = _AlignmentProvider(RuntimeError("ASR offline"))
    monkeypatch.setattr(h3_postprocess, "_run", fake_run)
    monkeypatch.setattr(h3_postprocess, "inspect_media_duration", lambda _path: 0.9)
    result = h3_postprocess.postprocess_h3_result(
        source,
        script_text=SCRIPT,
        alignment_provider=provider,
        needs_continuity_anchor=False,
        head_trim_enabled=True,
    )

    assert len(commands) == 2
    filter_graph = commands[1][commands[1].index("-filter_complex") + 1]
    assert "trim=start=0.300000" in filter_graph
    assert "atrim=start=0.300000" in filter_graph
    assert result.head_trim.mode == "fallback_300ms"
    assert result.head_trim.trim_seconds == pytest.approx(0.3)
    assert result.head_trim.alignment_provider == "funasr_http"
    assert result.head_trim.fallback_reason == "RuntimeError"
    assert result.normalized_duration_seconds == pytest.approx(0.9)
    assert result.anchor_path is None


def test_h3_postprocess_preserves_the_full_segment_when_head_trim_is_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "provider.mp4"
    source.write_bytes(b"provider-video")
    commands: list[list[str]] = []

    def fake_run(command: list[str], message: str) -> None:
        del message
        commands.append(command)
        Path(command[-1]).write_bytes(b"generated-output")

    provider = _AlignmentProvider(_alignment())
    monkeypatch.setattr(h3_postprocess, "_run", fake_run)
    monkeypatch.setattr(h3_postprocess, "inspect_media_duration", lambda _path: 1.23)

    result = h3_postprocess.postprocess_h3_result(
        source,
        script_text=SCRIPT,
        alignment_provider=provider,
        needs_continuity_anchor=False,
    )

    assert provider.calls == []
    assert len(commands) == 1
    assert "-filter_complex" not in commands[0]
    assert commands[0][commands[0].index("-c") + 1] == "copy"
    assert result.head_trim.mode == "disabled"
    assert result.head_trim.trim_seconds == 0
    assert result.head_trim.fallback_reason == "feature_disabled"
    assert result.normalized_duration_seconds == pytest.approx(1.23)


def test_prefix_mismatch_uses_fixed_300ms_instead_of_failing() -> None:
    decision = h3_postprocess.decide_h3_head_trim(
        SCRIPT,
        _alignment(include_prefix=2),
    )

    assert decision.mode == "fallback_300ms"
    assert decision.trim_seconds == pytest.approx(0.3)
    assert decision.matched_prefix_tokens == 2
    assert decision.fallback_reason == "script_prefix_not_matched"


def test_reference_frame_uses_half_second_then_falls_back_to_first_frame(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "reference.mp4"
    target = tmp_path / "primary.png"
    source.write_bytes(b"reference-video")
    seeks: list[str] = []

    def fake_run(command: list[str], message: str) -> None:
        del message
        seek = command[command.index("-ss") + 1]
        seeks.append(seek)
        if seek == "0.500":
            raise h3_postprocess.H3PostprocessError("retry")
        Path(command[-1]).write_bytes(b"frame")

    monkeypatch.setattr(h3_postprocess, "_run", fake_run)
    h3_postprocess.extract_reference_frame(source, target)

    assert seeks == ["0.500", "0"]
    assert target.read_bytes() == b"frame"

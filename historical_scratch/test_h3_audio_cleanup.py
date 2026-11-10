from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
import threading
import time
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jyd_probe.caption_alignment import FunASRCaptionAligner, RecognizedToken
from jyd_probe import h3_audio_cleanup as cleanup
from jyd_probe.project_h3_media import H3MediaError, _assemble_clean_audio, _run, prepare_h3_media


def tokens(anchor=0.4):
    return [RecognizedToken(c, c, round((anchor + i * .12) * 1e6), round((anchor + (i + 1) * .12) * 1e6)) for i, c in enumerate("你的体")]


def waveform(rate, channels, anchor, *, pulses=True):
    samples = np.zeros((round((anchor + 1) * rate), channels), dtype=np.int16)
    start = round(anchor * rate)
    wave = np.rint(10000 * np.sin(np.arange(len(samples) - start) * 2 * np.pi * 440 / rate)).astype(np.int16)
    samples[start:] = wave[:, None]
    if pulses:
        for position in (.02, anchor / 2):
            index = round(position * rate)
            samples[index:index + max(1, round(.01 * rate))] = 23000
        if channels == 2:
            samples[:start, 1] *= -1
    return samples


@pytest.mark.parametrize("rate,channels", [(16000, 1), (32000, 2), (44100, 2), (48000, 1)])
@pytest.mark.parametrize("anchor", [.18, .4, 1.2, 3.5])
def test_multiple_variable_head_pulses_removed_without_timing_or_speech_change(rate, channels, anchor):
    pcm = waveform(rate, channels, anchor)
    gate = cleanup.plan_head_gate(pcm, rate, "你的体重", tokens(anchor))
    clean = cleanup.apply_head_gate(pcm, gate)
    assert clean.shape == pcm.shape
    assert clean.dtype == pcm.dtype
    assert np.count_nonzero(clean[:round(anchor * rate)]) == 0
    assert np.array_equal(clean[gate.restore_at_sample:], pcm[gate.restore_at_sample:])
    assert 0 < gate.restore_at_sample <= round(anchor * rate)


def test_already_clean_clip_stays_identical():
    pcm = waveform(32000, 2, .4, pulses=False)
    gate = cleanup.plan_head_gate(pcm, 32000, "你的体重", tokens())
    assert np.array_equal(cleanup.apply_head_gate(pcm, gate), pcm)


def test_no_head_gap_never_falls_back_to_fixed_mute():
    pcm = np.full((32000, 1), 10000, dtype=np.int16)
    gate = cleanup.plan_head_gate(pcm, 32000, "你的体重", tokens(.05))
    assert gate.reason == "NO_HEAD_GAP"
    assert np.array_equal(cleanup.apply_head_gate(pcm, gate), pcm)


def test_half_cosine_fade_has_zero_and_unity_endpoints():
    pcm = np.full((1000, 2), 100, dtype=np.int16)
    gate = cleanup.HeadGate(900, 300, 620, "HEAD_GAP_FOUND")
    clean = cleanup.apply_head_gate(pcm, gate)
    assert np.all(clean[:301] == 0)
    assert np.all(np.diff(clean[300:620, 0]) >= 0)
    assert np.array_equal(clean[619:], pcm[619:])


def test_prefix_is_exact_and_cannot_match_late_repeated_sentence():
    with pytest.raises(H3MediaError, match="精确匹配"):
        cleanup.prefix_anchor("你的体重", tokens()[1:])
    noise = [RecognizedToken("啊", "啊", i, i + 1) for i in range(4)]
    with pytest.raises(H3MediaError, match="精确匹配"):
        cleanup.prefix_anchor("你的体重", noise + tokens())
    assert cleanup.prefix_anchor("你，的体重", tokens())[0] == 400000


def test_invalid_anchor_does_not_modify_source():
    pcm = waveform(32000, 1, .4)
    with pytest.raises(H3MediaError, match="有效范围"):
        cleanup.plan_head_gate(pcm, 32000, "你的体重", tokens(6))


def test_cache_identity_covers_raw_script_and_configuration():
    base = cleanup.cleanup_key("raw-a", "你的体重")
    assert base != cleanup.cleanup_key("raw-b", "你的体重")
    assert base != cleanup.cleanup_key("raw-a", "你的体重。")
    assert base != cleanup.cleanup_key("raw-a", "你的体重", replace(cleanup.DEFAULT_CONFIG, fade_ms=15))
    assert base != cleanup.cleanup_key("raw-a", "你的体重", replace(cleanup.DEFAULT_CONFIG, version="v2"))


def test_public_asr_adapter_never_interpolates(tmp_path, monkeypatch):
    aligner = FunASRCaptionAligner("http://unused")
    monkeypatch.setattr(aligner, "_transcribe", lambda _: {"tokens": [{"text": "你", "startSeconds": .23, "endSeconds": .33}]})
    assert aligner.recognize_tokens(tmp_path / "asr.wav") == [RecognizedToken("你", "你", 230000, 330000)]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
def test_real_media_cleanup_cache_preview_and_master_use_same_pcm(tmp_path):
    raw_wav = tmp_path / "synth.wav"
    raw = tmp_path / "current.mp4"
    cleanup._write_pcm(raw_wav, waveform(32000, 2, .4), 32000)
    _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=25:d=1.4", "-i", str(raw_wav), "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-c:a", "aac", str(raw)], "fixture")
    digest = cleanup.file_sha256(raw)
    aligner = Mock()
    aligner.recognize_tokens.return_value = tokens()
    result = cleanup.clean_segment(raw, "你的体重", aligner)
    assert cleanup.file_sha256(raw) == digest
    assert result.report["speech_pcm_unchanged"] is True
    clean, rate = cleanup._read_pcm(result.audio_path)
    decoded = tmp_path / "decoded.wav"
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-vn", "-c:a", "pcm_s16le", str(decoded)], "decode")
    pcm, _ = cleanup._read_pcm(decoded)
    end = result.report["gate"]["restore_at_sample"]
    assert len(clean) == len(pcm)
    assert np.array_equal(clean[end:], pcm[end:])
    assert np.max(np.abs(clean[:round(.2 * rate)].astype(np.int32))) == 0
    assert cleanup.clean_segment(raw, "你的体重", aligner).key == result.key
    aligner.recognize_tokens.assert_called_once()
    assets = prepare_h3_media(segment_paths=[raw, raw], segment_texts=["你的体重", "你的体重"], script_text="你的体重你的体重", target_dir=tmp_path / "master", segment_audio_paths=[result.audio_path, result.audio_path])
    assembled, master_rate = cleanup._read_pcm(assets.authoritative_audio_path)
    assert master_rate == rate
    boundary = round(assets.segment_durations_seconds[0] * rate)
    assert len(assembled) == round(sum(assets.segment_durations_seconds) * rate)
    assert np.array_equal(assembled[:min(boundary, len(clean))], clean[:boundary])
    assert np.array_equal(assembled[boundary:boundary + min(boundary, len(clean))], clean[:boundary])
    # Raw download/cache survives derivative failure, and corrupt output is rebuilt.
    result.audio_path.write_bytes(b"truncated")
    assert cleanup.read_cleanup(raw, "你的体重") is None
    assert cleanup.file_sha256(raw) == digest


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
def test_pcm_assembly_pads_to_video_and_preserves_audio_offset(tmp_path):
    path = tmp_path / "piece.wav"
    pcm = np.full((3200, 1), 1000, dtype=np.int16)
    cleanup._write_pcm(path, pcm, 32000)
    output = _assemble_clean_audio([path, path], [.2, .2], [.025, -.025], tmp_path / "assembled.wav")
    result, rate = cleanup._read_pcm(output)
    assert len(result) == round(.4 * rate)
    assert np.all(result[:800] == 0)
    assert np.all(result[800:4000] == 1000)
    assert np.all(result[4000:6400] == 0)
    assert np.all(result[6400:8800] == 1000)
    assert np.all(result[8800:] == 0)


def test_local_queue_is_shared_nonblocking_and_deduplicated(tmp_path, monkeypatch):
    raw = tmp_path / "current.mp4"
    raw.write_bytes(b"source")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def work(*_args):
        calls.append(1)
        started.set()
        release.wait(3)
        finished.set()
    monkeypatch.setattr(cleanup, "clean_segment", work)
    try:
        assert cleanup.request_cleanup(raw, "你的体重", None)["status"] == "PROCESSING"
        assert started.wait(2)
        assert not finished.is_set()
        assert cleanup.request_cleanup(raw, "你的体重", None)["status"] == "PROCESSING"
        assert calls == [1]
    finally:
        release.set()
        assert finished.wait(2)


def test_failure_budget_is_persistent_and_local_retry_resets_it(tmp_path, monkeypatch):
    raw = tmp_path / "current.mp4"
    raw.write_bytes(b"source")
    directory, _, _ = cleanup._cache_directory(raw, "你的体重", cleanup.DEFAULT_CONFIG)
    directory.mkdir(parents=True)
    failure = directory / "failure.json"
    failure.write_text(json.dumps({"attempts": 3, "error": "ASR offline", "retry_at": 0}), encoding="utf-8")
    submit = Mock()
    monkeypatch.setattr(cleanup._EXECUTOR, "submit", submit)
    assert cleanup.request_cleanup(raw, "你的体重", None)["status"] == "FAILED"
    submit.assert_not_called()
    try:
        assert cleanup.request_cleanup(raw, "你的体重", None, force_retry=True)["status"] == "PROCESSING"
        submit.assert_called_once()
    finally:
        with cleanup._LOCK:
            cleanup._PENDING.discard(str(directory.resolve()))

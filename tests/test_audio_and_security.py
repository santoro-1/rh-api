from __future__ import annotations

import logging
from pathlib import Path
import wave

import pytest

from app.config import get_settings
from app.routes.operations import _read_log_chunk, _tail
from app.services import audio
from app.services.audio import (
    format_duration_timecode,
    format_timecode,
    parse_timecode,
    validate_time_range,
)
from app.services.logging_config import SecretRedactionFilter
from app.services.security import decrypt_secret, encrypt_secret, hash_password, verify_password


def test_timecode_parsing_and_formatting():
    assert parse_timecode("1:05") == 65
    assert format_timecode(65.8) == "1:05"
    assert format_timecode(0) == "0:00"
    assert format_duration_timecode(28.0) == "0:28"
    assert format_duration_timecode(28.1) == "0:29"
    assert format_duration_timecode(65.8) == "1:06"
    with pytest.raises(ValueError):
        parse_timecode("1:60")
    with pytest.raises(ValueError):
        parse_timecode("65")


def test_time_range_validation():
    assert validate_time_range("0:00", "0:15", 15.7) == (0, 15)
    assert validate_time_range("0:00", "0:16", 15.1) == (0, 16)
    with pytest.raises(ValueError):
        validate_time_range("0:15", "0:15", 20)
    with pytest.raises(ValueError):
        validate_time_range("0:00", "0:17", 15.1)


def test_password_hash_and_encrypted_secret_round_trip():
    password_hash = hash_password("password123")
    assert password_hash != "password123"
    assert verify_password("password123", password_hash)
    encrypted = encrypt_secret("real-secret-value")
    assert encrypted != "real-secret-value"
    assert decrypt_secret(encrypted) == "real-secret-value"


def test_log_filter_redacts_header_and_dictionary_secret_shapes():
    record = logging.LogRecord(
        "security-test",
        logging.INFO,
        __file__,
        1,
        (
            "headers={'Authorization': 'Bearer bearer-secret'}, "
            "payload={'api_key': 'api-secret', "
            "'access_password_encrypted': 'cipher-secret'}"
        ),
        (),
        None,
    )

    assert SecretRedactionFilter().filter(record)
    message = record.getMessage()
    assert "bearer-secret" not in message
    assert "api-secret" not in message
    assert "cipher-secret" not in message
    assert message.count("***") == 3


def test_log_filter_redacts_proxy_url_and_query_secret():
    record = logging.LogRecord(
        "security-test",
        logging.WARNING,
        __file__,
        1,
        (
            "proxy=https://proxy-user:proxy-password@proxy.example/path"
            "?apiKey=query-secret"
        ),
        (),
        None,
    )

    assert SecretRedactionFilter().filter(record)
    message = record.getMessage()
    assert "proxy-user" not in message
    assert "proxy-password" not in message
    assert "query-secret" not in message
    assert "https://***:***@proxy.example/path?apiKey=***" in message


def test_admin_log_tail_hides_successful_polling_but_keeps_raw_file():
    log_path = get_settings().data_dir / "web-display-test.log"
    meaningful = "2026-07-28 ERROR app: 语音任务失败"
    polling = (
        '2026-07-28 INFO uvicorn.access: 127.0.0.1 - '
        '"GET /api/batches/batch-id HTTP/1.1" 200 OK'
    )
    health = (
        '2026-07-28 INFO uvicorn.access: 127.0.0.1 - '
        '"GET /healthz HTTP/1.1" 200 OK'
    )
    failed_health = (
        '2026-07-28 INFO uvicorn.access: 127.0.0.1 - '
        '"GET /healthz HTTP/1.1" 503 Service Unavailable'
    )
    log_path.write_text(
        "\n".join((polling, meaningful, health, failed_health)),
        encoding="utf-8",
    )

    assert _tail(log_path) == [meaningful, failed_health]
    raw = log_path.read_text(encoding="utf-8")
    assert polling in raw
    assert health in raw


def test_admin_log_chunk_only_returns_new_operator_events():
    log_path = get_settings().data_dir / "web-incremental-display-test.log"
    first_event = (
        "2026-07-28 INFO app: "
        '[EVENT web.starting] Web 服务正在启动 {"port":8000}'
    )
    polling = (
        '2026-07-28 INFO uvicorn.access: 127.0.0.1 - '
        '"GET /admin/operations/updates?web=0 HTTP/1.1" 200 OK'
    )
    log_path.write_text(
        f"{first_event}\n{polling}\n",
        encoding="utf-8",
    )

    initial = _read_log_chunk(log_path, None)
    assert initial["lines"] == [first_event]

    second_event = (
        "2026-07-28 WARNING app: "
        '[EVENT video.failed] 视频任务失败 {"task_id":"task-1"}'
    )
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(f"{second_event}\n")

    update = _read_log_chunk(log_path, initial["cursor"])
    assert update["lines"] == [second_event]
    assert update["cursor"] > initial["cursor"]


def test_admin_log_chunk_filters_business_source_without_mixing_channels():
    log_path = get_settings().data_dir / "source-filter-display-test.log"
    legacy = (
        '2026-08-07 INFO app: [EVENT batch.created] legacy '
        '{"source_channel":"legacy_web","batch_id":"legacy-1"}'
    )
    workbench = (
        '2026-08-07 INFO app: [EVENT workbench.created] workbench '
        '{"source_channel":"new_workbench","batch_id":"workbench-1"}'
    )
    system = "2026-08-07 ERROR app: system error without a business source"
    log_path.write_text(
        "\n".join((legacy, workbench, system)) + "\n",
        encoding="utf-8",
    )

    assert _read_log_chunk(
        log_path,
        None,
        source_channel="legacy_web",
    )["lines"] == [legacy]
    assert _read_log_chunk(
        log_path,
        None,
        source_channel="new_workbench",
    )["lines"] == [workbench]
    assert _read_log_chunk(log_path, None)["lines"] == [legacy, workbench, system]


def test_audio_duration_falls_back_to_mutagen_when_ffprobe_fails(monkeypatch):
    sample = get_settings().data_dir / "sample.wav"
    with wave.open(str(sample), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * 16000)

    class FailedProcess:
        returncode = 1
        stderr = ""
        stdout = ""

    monkeypatch.setattr(audio.subprocess, "run", lambda *args, **kwargs: FailedProcess())
    assert audio.inspect_audio_duration(sample) == pytest.approx(2.0)


def test_provider_audio_tail_appends_exact_requested_silence(monkeypatch):
    working_dir = get_settings().data_dir / "provider-tail"
    working_dir.mkdir(parents=True, exist_ok=True)
    source = working_dir / "speech.mp3"
    target = working_dir / "speech-with-tail.mp3"
    source.write_bytes(b"speech")
    captured = {}

    class SuccessfulProcess:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(command, **kwargs):
        del kwargs
        captured["command"] = command
        Path(command[-1]).write_bytes(b"speech-with-tail")
        return SuccessfulProcess()

    monkeypatch.setattr(audio, "inspect_audio_duration", lambda _path: 30.8)
    monkeypatch.setattr(audio.subprocess, "run", fake_run)

    audio.add_silence_tail(source, target, padding_seconds=2.0)

    assert target.read_bytes() == b"speech-with-tail"
    assert "apad=pad_dur=2.000" in captured["command"]
    assert captured["command"][captured["command"].index("-t") + 1] == "32.800"


def test_generated_speech_mastering_adds_three_db_and_caps_peak(
    monkeypatch,
):
    working_dir = get_settings().data_dir / "speech-mastering-success"
    working_dir.mkdir(parents=True, exist_ok=True)
    source = working_dir / "provider.mp3"
    target = working_dir / "generated.mp3"
    source.write_bytes(b"provider-audio")
    commands = []

    class SuccessfulProcess:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(command, **kwargs):
        del kwargs
        commands.append(command)
        Path(command[-1]).write_bytes(b"mastered-audio")
        return SuccessfulProcess()

    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    monkeypatch.setattr(audio, "inspect_audio_duration", lambda path: 12.0)

    audio.master_generated_speech(source, target)

    assert target.read_bytes() == b"mastered-audio"
    audio_filter = commands[0][commands[0].index("-af") + 1]
    assert "volume=3.000dB" in audio_filter
    assert "alimiter=limit=0.891250938" in audio_filter
    assert "level=false" in audio_filter
    assert "latency=true" in audio_filter
    assert list(working_dir.glob(".*.mastering-*.mp3")) == []


def test_generated_speech_mastering_preserves_previous_file_on_failure(
    monkeypatch,
):
    working_dir = get_settings().data_dir / "speech-mastering-failure"
    working_dir.mkdir(parents=True, exist_ok=True)
    source = working_dir / "provider.mp3"
    target = working_dir / "generated.mp3"
    source.write_bytes(b"provider-audio")
    target.write_bytes(b"previous-good-audio")

    class FailedProcess:
        returncode = 1
        stderr = "decode failed"
        stdout = ""

    monkeypatch.setattr(
        audio.subprocess, "run", lambda *args, **kwargs: FailedProcess()
    )

    with pytest.raises(audio.AudioInspectionError, match="提升口播音量失败"):
        audio.master_generated_speech(source, target)

    assert target.read_bytes() == b"previous-good-audio"
    assert list(working_dir.glob(".*.mastering-*.mp3")) == []


def test_legacy_generated_speech_is_mastered_only_once(monkeypatch):
    working_dir = get_settings().data_dir / "speech-mastering-legacy"
    working_dir.mkdir(parents=True, exist_ok=True)
    target = working_dir / "generated.mp3"
    target.write_bytes(b"legacy-audio")
    commands = []

    class SuccessfulProcess:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(command, **kwargs):
        del kwargs
        commands.append(command)
        Path(command[-1]).write_bytes(b"mastered-legacy-audio")
        return SuccessfulProcess()

    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    monkeypatch.setattr(audio, "inspect_audio_duration", lambda path: 12.0)

    assert audio.ensure_generated_speech_mastered(target) is True
    assert audio.ensure_generated_speech_mastered(target) is False
    assert target.read_bytes() == b"mastered-legacy-audio"
    assert len(commands) == 1

    target.write_bytes(b"replacement-audio")
    assert audio.ensure_generated_speech_mastered(target) is True
    assert len(commands) == 2

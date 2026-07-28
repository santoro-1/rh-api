from __future__ import annotations

import logging
import wave

import pytest

from app.config import get_settings
from app.routes.operations import _read_log_chunk, _tail
from app.services import audio
from app.services.audio import format_timecode, parse_timecode, validate_time_range
from app.services.logging_config import SecretRedactionFilter
from app.services.security import decrypt_secret, encrypt_secret, hash_password, verify_password


def test_timecode_parsing_and_formatting():
    assert parse_timecode("1:05") == 65
    assert format_timecode(65.8) == "1:05"
    assert format_timecode(0) == "0:00"
    with pytest.raises(ValueError):
        parse_timecode("1:60")
    with pytest.raises(ValueError):
        parse_timecode("65")


def test_time_range_validation():
    assert validate_time_range("0:00", "0:15", 15.7) == (0, 15)
    with pytest.raises(ValueError):
        validate_time_range("0:15", "0:15", 20)
    with pytest.raises(ValueError):
        validate_time_range("0:00", "0:16", 15.1)


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

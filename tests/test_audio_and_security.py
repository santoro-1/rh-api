from __future__ import annotations

import wave

import pytest

from app.services import audio
from app.services.audio import format_timecode, parse_timecode, validate_time_range
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


def test_audio_duration_falls_back_to_mutagen_when_ffprobe_fails(tmp_path, monkeypatch):
    sample = tmp_path / "sample.wav"
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

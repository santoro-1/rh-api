from __future__ import annotations

from pathlib import Path

import pytest

from app.services.alignment.funasr_http import (
    FunASRHTTPProvider,
    _parse_tokens,
)
from app.services.alignment.script_timestamps import (
    RecognizedToken,
    plan_script_aligned_segments,
    tokenize_script,
)
from app.services.media_segmentation import MediaSegmentationError


def _timestamped_script(script: str) -> tuple[list[RecognizedToken], float]:
    tokens = tokenize_script(script)
    recognized: list[RecognizedToken] = []
    cursor = 0.0
    previous_end = 0
    for token in tokens:
        between = script[previous_end : token.start_offset]
        if any(char in "。！？!?" for char in between):
            cursor += 0.4
        recognized.append(
            RecognizedToken(
                text=token.key,
                start_seconds=cursor,
                end_seconds=cursor + 0.2,
            )
        )
        cursor += 0.2
        previous_end = token.end_offset
    return recognized, cursor


def test_asr_alignment_keeps_next_sentence_first_word_out_of_previous_segment():
    first = f"{'甲' * 140}。"
    second = f"{'乙' * 132}是不是特别麻烦？"
    third = f"那你就可以{'丙' * 135}。"
    script = first + second + third
    tokens, duration = _timestamped_script(script)

    plans = plan_script_aligned_segments(script, duration, tokens)

    assert "".join(plan.script_text for plan in plans) == script
    assert all(plan.duration_seconds <= 20.01 for plan in plans)
    boundary_index = next(
        index
        for index, plan in enumerate(plans[:-1])
        if plan.script_text.endswith("是不是特别麻烦？")
    )
    assert plans[boundary_index + 1].script_text.startswith("那你就可以")
    assert plans[boundary_index].alignment_method == "asr_timestamp"


def test_asr_alignment_rejects_audio_that_does_not_match_script():
    script = "今天是星期四，我要吃肯德基。"
    tokens = [
        RecognizedToken("完", 0.0, 0.2),
        RecognizedToken("全", 0.2, 0.4),
        RecognizedToken("不", 0.4, 0.6),
        RecognizedToken("同", 0.6, 0.8),
    ]

    with pytest.raises(MediaSegmentationError, match="差异过大"):
        plan_script_aligned_segments(script, 1.0, tokens)


def test_funasr_response_parser_accepts_token_timestamps():
    tokens = _parse_tokens(
        {
            "tokens": [
                {
                    "text": "麻",
                    "startSeconds": 62.1,
                    "endSeconds": 62.3,
                },
                {
                    "text": "烦",
                    "startSeconds": 62.3,
                    "endSeconds": 62.55,
                    "confidence": 0.98,
                },
                {
                    "text": "那",
                    "startSeconds": 62.8,
                    "endSeconds": 63.0,
                },
            ]
        }
    )

    assert [token.text for token in tokens] == ["麻", "烦", "那"]
    assert tokens[1].confidence == 0.98


def test_funasr_http_provider_posts_audio_and_builds_plans(
    tmp_path: Path,
    monkeypatch,
):
    audio = tmp_path / "speech.mp3"
    audio.write_bytes(b"ID3audio")
    script = f"{'甲' * 140}。{'乙' * 140}。"
    recognized, duration = _timestamped_script(script)

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "tokens": [
                    {
                        "text": token.text,
                        "startSeconds": token.start_seconds,
                        "endSeconds": token.end_seconds,
                    }
                    for token in recognized
                ]
            }

    posted: dict[str, object] = {}

    def fake_post(url, **kwargs):
        posted["url"] = url
        posted["headers"] = kwargs["headers"]
        posted["timeout"] = kwargs["timeout"]
        return FakeResponse()

    monkeypatch.setattr(
        "app.services.alignment.funasr_http.requests.post",
        fake_post,
    )
    monkeypatch.setattr(
        "app.services.alignment.funasr_http.inspect_audio_duration",
        lambda _path: duration,
    )
    provider = FunASRHTTPProvider(
        base_url="http://127.0.0.1:18084",
        shared_token="secret",
        timeout_seconds=321,
    )

    result = provider.align(audio, script)

    assert posted["url"] == "http://127.0.0.1:18084/v1/transcribe"
    assert posted["headers"] == {"Authorization": "Bearer secret"}
    assert posted["timeout"] == (10, 321)
    assert "".join(plan.script_text for plan in result.plans) == script

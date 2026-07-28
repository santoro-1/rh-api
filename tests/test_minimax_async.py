from __future__ import annotations

import io
import json
import tarfile
import zipfile

import pytest

from app.services.media_segmentation import plan_timestamped_segments
from app.services.speech.async_outputs import (
    SubtitleCue,
    decode_async_speech_output,
)
from app.services.speech.minimax import MiniMaxClient, parse_pronunciation_tones
from tests.async_speech_fakes import make_async_speech_bundle


class _Response:
    def __init__(self, payload=None, *, content: bytes = b""):
        self.payload = payload
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)


def test_minimax_async_client_submits_queries_and_downloads():
    session = _Session(
        [
            _Response(
                {
                    "task_id": 123,
                    "file_id": 456,
                    "base_resp": {"status_code": 0},
                }
            ),
            _Response(
                {
                    "task_id": 123,
                    "status": "Success",
                    "file_id": 456,
                    "base_resp": {"status_code": 0},
                }
            ),
            _Response(content=b"result-bundle"),
        ]
    )
    client = MiniMaxClient(
        "test-key",
        base_url="https://api.minimaxi.com",
        session=session,
    )

    task_id, file_id, _ = client.create_async_speech_task(
        text="这是一段需要生成的口播脚本。",
        voice_id="audiobook_male_1",
        model="speech-2.8-hd",
        speed=1.0,
        volume=1.0,
        pitch=0,
        language_boost="auto",
        output_format="mp3",
        pronunciation_tones=[
            "燕少飞/(yan4)(shao3)(fei1)",
            "omg/oh my god",
        ],
    )
    status, queried_file_id, _ = client.query_async_speech_task(task_id)
    content = client.download_file_content(queried_file_id or "")

    assert (task_id, file_id) == ("123", "456")
    assert (status, queried_file_id) == ("success", "456")
    assert content == b"result-bundle"
    submit_body = session.calls[0][2]["json"]
    assert submit_body["audio_setting"]["audio_sample_rate"] == 32000
    assert submit_body["voice_setting"]["voice_id"] == "audiobook_male_1"
    assert submit_body["pronunciation_dict"] == {
        "tone": [
            "燕少飞/(yan4)(shao3)(fei1)",
            "omg/oh my god",
        ]
    }
    assert session.calls[1][2]["params"] == {"task_id": "123"}
    assert session.calls[2][2]["params"] == {"file_id": "456"}


def test_async_output_parser_supports_srt_and_json_milliseconds():
    srt_bundle = make_async_speech_bundle(
        b"ID3audio",
        [(0.0, 28.5, "第一句。"), (28.8, 56.2, "第二句。")],
    )
    decoded = decode_async_speech_output(
        srt_bundle,
        expected_format="mp3",
    )
    assert decoded.audio_bytes == b"ID3audio"
    assert decoded.cues[1].start_seconds == 28.8

    json_timeline = {
        "sentences": [
            {"text": "第一句。", "start_time": 0, "end_time": 28500},
            {
                "text": "第二句。",
                "start_time": 28800,
                "end_time": 56200,
            },
        ]
    }
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("audio.mp3", b"ID3audio")
        archive.writestr(
            "subtitle.json",
            json.dumps(json_timeline, ensure_ascii=False),
        )
    decoded_json = decode_async_speech_output(
        target.getvalue(),
        expected_format="mp3",
    )
    assert decoded_json.cues[0].end_seconds == 28.5
    assert decoded_json.cues[1].start_seconds == 28.8


def test_async_output_parser_supports_real_minimax_titles_tar():
    titles = [
        {
            "text": "第一句话。",
            "pronounce_text": "第一句话。",
            "time_begin": 0,
            "time_end": 8816.462585034013,
        },
        {
            "text": "第二句话。",
            "pronounce_text": "第二句话。",
            "time_begin": 9016.462585034013,
            "time_end": 21629.0,
        },
    ]
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w") as archive:
        for name, content in (
            ("result/content.mp3", b"ID3audio"),
            (
                "result/content.titles",
                json.dumps(titles, ensure_ascii=False).encode("utf-8"),
            ),
            ("result/content.extra", b'{"audio_length":21800}'),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

    decoded = decode_async_speech_output(
        target.getvalue(),
        expected_format="mp3",
    )

    assert decoded.audio_bytes == b"ID3audio"
    assert decoded.cues[0].end_seconds == pytest.approx(8.816462585034013)
    assert decoded.cues[1].start_seconds == pytest.approx(9.016462585034013)


def test_pronunciation_tones_use_official_json_array_format():
    raw = '["燕少飞/(yan4)(shao3)(fei1)", "omg/oh my god"]'

    assert parse_pronunciation_tones(raw) == [
        "燕少飞/(yan4)(shao3)(fei1)",
        "omg/oh my god",
    ]


def test_timestamp_planner_uses_sentence_boundaries_and_45_second_limit():
    cues = [
        SubtitleCue("第一段。", 0.0, 27.0),
        SubtitleCue("第二段。", 27.4, 35.0),
        SubtitleCue("第三段。", 35.3, 62.0),
        SubtitleCue("第四段。", 62.4, 87.0),
    ]

    plans = plan_timestamped_segments(cues, 87.5)

    assert [plan.script_text for plan in plans] == [
        "第一段。",
        "第二段。第三段。",
        "第四段。",
    ]
    assert all(plan.duration_seconds <= 45.0 for plan in plans)
    assert plans[0].end_seconds == 27.2
    assert plans[-1].end_seconds == 87.5
    assert all(
        plan.alignment_method == "minimax_sentence_timestamp"
        for plan in plans
    )

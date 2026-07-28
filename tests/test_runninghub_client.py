from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings
from app.services.runninghub import RunningHubClient, RunningHubError
from app.services.workflow import build_payload


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.pop(0)


def _test_media_path(name: str) -> Path:
    directory = get_settings().data_dir / "runninghub-client-tests"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def test_upload_and_submit_are_server_side_and_use_filename():
    media = _test_media_path("source.png")
    media.write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    session = FakeSession(
        [
            FakeResponse(200, {"code": 0, "data": {"fileName": "openapi/image.png"}}),
            FakeResponse(200, {"taskId": "remote-123", "status": "RUNNING"}),
        ]
    )
    client = RunningHubClient("secret", "https://rh.example", "app-1", session)
    filename = client.upload_file(media)
    payload = build_payload(filename, "openapi/audio.mp3", "0:00", "0:10", "提示", "plus")
    assert client.submit_task(payload) == "remote-123"
    assert payload["instanceType"] == "default"
    assert payload["usePersonalQueue"] is False
    assert "retainSeconds" not in payload
    assert any(node["fieldValue"] == "openapi/image.png" for node in payload["nodeInfoList"])


def test_upload_rejects_missing_filename():
    media = _test_media_path("missing-filename.png")
    media.write_bytes(b"x")
    client = RunningHubClient(
        "secret",
        "https://rh.example",
        "app-1",
        FakeSession([FakeResponse(200, {"code": 0, "data": {}})]),
    )
    with pytest.raises(RunningHubError, match="fileName"):
        client.upload_file(media)


def test_query_preserves_usage_null():
    client = RunningHubClient(
        "secret",
        "https://rh.example",
        "app-1",
        FakeSession([FakeResponse(200, {"taskId": "x", "status": "SUCCESS", "usage": None})]),
    )
    assert client.query_task("x")["usage"] is None


def test_workflow_submission_uses_workflow_v2_endpoint():
    session = FakeSession(
        [FakeResponse(200, {"taskId": "workflow-task", "status": "RUNNING"})]
    )
    client = RunningHubClient(
        "secret",
        "https://rh.example",
        "2080551073030434817",
        session,
        submission_type="workflow",
    )
    assert client.submit_task({"nodeInfoList": []}) == "workflow-task"
    assert (
        session.calls[0][0][0]
        == "https://rh.example/openapi/v2/run/workflow/2080551073030434817"
    )

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from app.config import get_settings
from app.services.runninghub import (
    RunningHubClient,
    RunningHubError,
    runninghub_upload_diagnostics,
)
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
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


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
    assert payload["instanceType"] == "plus"
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
    with pytest.raises(RunningHubError, match="fileName") as error:
        client.upload_file(media)
    assert error.value.retry_safe is True


def test_upload_surfaces_business_error_message():
    media = _test_media_path("invalid-key.png")
    media.write_bytes(b"x")
    client = RunningHubClient(
        "secret",
        "https://rh.example",
        "app-1",
        FakeSession(
            [
                FakeResponse(
                    200,
                    {"code": 1, "msg": "API Key不存在", "data": None},
                )
            ]
        ),
    )
    with pytest.raises(RunningHubError, match="API Key不存在") as error:
        client.upload_file(media)
    assert error.value.retry_safe is True


def test_upload_network_error_keeps_safe_operator_diagnostics():
    media = _test_media_path("network-timeout.png")
    media.write_bytes(b"network-test")
    session = FakeSession(
        [
            requests.ReadTimeout(
                "proxy https://proxy-user:proxy-password@proxy.example/"
                "?apiKey=secret-value timed out"
            )
        ]
    )
    client = RunningHubClient(
        "secret",
        "https://rh.example",
        "app-1",
        session,
    )

    with pytest.raises(RunningHubError) as error:
        client.upload_file(media)

    details = error.value.log_details()
    assert error.value.retry_safe is True
    assert details["runninghub_operation"] == "asset_upload"
    assert details["endpoint_host"] == "rh.example"
    assert details["network_error_type"] == "ReadTimeout"
    assert details["asset_size_bytes"] == len(b"network-test")
    assert details["elapsed_ms"] >= 0
    assert "proxy-password" not in details["network_error"]
    assert "secret-value" not in details["network_error"]
    assert "***:***@" in details["network_error"]


def test_large_upload_diagnostics_warn_without_rejecting_input():
    details = runninghub_upload_diagnostics(240 * 1024 * 1024)

    assert details["asset_size_mb"] == 240.0
    assert "超过 RunningHub 官方建议的 30MB" in details["upload_size_warning"]
    assert "upload_size_warning" not in runninghub_upload_diagnostics(
        15 * 1024 * 1024
    )


def test_account_status_returns_current_task_count_without_exposing_key():
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "code": 0,
                    "msg": "success",
                    "data": {"currentTaskCounts": "1"},
                },
            )
        ]
    )
    client = RunningHubClient("secret", "https://rh.example", "app-1", session)
    assert client.get_account_current_task_count() == 1
    _, kwargs = session.calls[0]
    assert kwargs["json"] == {"apikey": "secret"}
    assert session.calls[0][0][0] == "https://rh.example/uc/openapi/accountStatus"


def test_submit_identifies_remote_capacity_limit():
    client = RunningHubClient(
        "secret",
        "https://rh.example",
        "app-1",
        FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "errorCode": "421",
                        "errorMessage": (
                            "api queue limit reached, please retry later | "
                            "API 并发数已达上线，请降低并发或稍后重试"
                        ),
                    },
                )
            ]
        ),
    )
    with pytest.raises(RunningHubError) as error:
        client.submit_task({"nodeInfoList": []})
    assert error.value.is_capacity_limited is True


def test_query_identifies_remote_task_not_found():
    client = RunningHubClient(
        "secret",
        "https://rh.example",
        "app-1",
        FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "taskId": "missing-task",
                        "status": "",
                        "errorCode": "1004",
                        "errorMessage": (
                            "Task not found, please check the task ID | "
                            "任务不存在或已过期，请检查任务ID"
                        ),
                    },
                )
            ]
        ),
    )
    with pytest.raises(RunningHubError) as error:
        client.query_task("missing-task")
    assert error.value.is_task_not_found is True


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


def test_cancel_task_uses_official_endpoint_and_requires_success():
    session = FakeSession(
        [FakeResponse(200, {"code": 0, "msg": "success", "data": None})]
    )
    client = RunningHubClient("secret", "https://rh.example", "app-1", session)
    client.cancel_task("remote-123")
    args, kwargs = session.calls[0]
    assert args[0] == "https://rh.example/task/openapi/cancel"
    assert kwargs["json"] == {"apiKey": "secret", "taskId": "remote-123"}


def test_cancel_task_surfaces_business_failure():
    client = RunningHubClient(
        "secret",
        "https://rh.example",
        "app-1",
        FakeSession(
            [FakeResponse(200, {"code": 1, "msg": "task cannot cancel"})]
        ),
    )
    with pytest.raises(RunningHubError, match="task cannot cancel"):
        client.cancel_task("remote-123")

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.workflows import get_workflow, list_workflows
from app.workflows.base import WorkflowAsset


def test_digital_human_adapter_is_registered_and_owns_payload_mapping():
    workflow = get_workflow("digital_human")
    assert workflow in list_workflows()
    parameters = workflow.validate_parameters(
        {"prompt": "自定义提示词", "start_time": "0:00", "end_time": "0:10"},
        {"audio_duration_seconds": 10.5},
    )
    payload_input = workflow.serialize_input(
        [
            WorkflowAsset("image", "image", "uploads/1/a/image.png", "image.png"),
            WorkflowAsset("audio", "audio", "uploads/1/a/audio.mp3", "audio.mp3"),
        ],
        parameters,
        {"audio_duration_seconds": 10.5},
    )
    task = SimpleNamespace(
        input_payload=json.dumps(payload_input, ensure_ascii=False),
        image_path="legacy/image.png",
        audio_path="legacy/audio.mp3",
        image_original_name="image.png",
        audio_original_name="audio.mp3",
        audio_duration_seconds=10.5,
        start_seconds=0,
        end_seconds=10,
        prompt="自定义提示词",
    )
    payload = workflow.build_payload(
        task,
        {"image": "remote-image", "audio": "remote-audio"},
        ai_app_id="app-id-for-this-workflow",
        instance_type="plus",
        settings={},
    )

    assert payload["instanceType"] == "plus"
    assert payload["usePersonalQueue"] is False
    assert "retainSeconds" not in payload
    assert any(
        node["nodeId"] == "240" and node["fieldValue"] == "remote-image"
        for node in payload["nodeInfoList"]
    )
    assert any(
        node["nodeId"] == "339" and node["fieldValue"] == "remote-audio"
        for node in payload["nodeInfoList"]
    )
    expected_nodes = {
        node["nodeId"]: node["fieldValue"] for node in payload["nodeInfoList"]
    }
    assert expected_nodes["503"] == "1024"
    assert expected_nodes["752"] == "2"
    assert expected_nodes["753"] == "1"
    assert expected_nodes["739"] == "None"
    assert expected_nodes["738"] == "None"


def test_digital_human_adapter_maps_dual_person_audio_and_selected_modes():
    workflow = get_workflow("digital_human")
    parameters = workflow.validate_parameters(
        {
            "prompt": "双人对话",
            "start_time": "0:00",
            "end_time": "0:10",
            "resolution": "2048",
            "overall_mode": "0",
            "person_mode": "0",
        },
        {"audio_duration_seconds": 10.5},
    )
    assets = [
        WorkflowAsset("image", "image", "uploads/image.png", "image.png"),
        WorkflowAsset("audio", "audio", "uploads/audio.mp3", "audio.mp3"),
        WorkflowAsset("left_audio", "audio", "uploads/left.mp3", "left.mp3"),
        WorkflowAsset("right_audio", "audio", "uploads/right.mp3", "right.mp3"),
    ]
    task = SimpleNamespace(
        input_payload=json.dumps(
            workflow.serialize_input(assets, parameters, {"audio_duration_seconds": 10.5}),
            ensure_ascii=False,
        ),
        image_path="legacy/image.png",
        audio_path="legacy/audio.mp3",
        image_original_name="image.png",
        audio_original_name="audio.mp3",
        audio_duration_seconds=10.5,
        start_seconds=0,
        end_seconds=10,
        prompt="双人对话",
    )

    assert [asset.name for asset in workflow.assets_for_task(task)] == [
        "image",
        "audio",
        "left_audio",
        "right_audio",
    ]
    payload = workflow.build_payload(
        task,
        {
            "image": "remote-image",
            "audio": "remote-audio",
            "left_audio": "remote-left-audio",
            "right_audio": "remote-right-audio",
        },
        ai_app_id="app-id",
        instance_type="plus",
        settings={},
    )
    nodes = {node["nodeId"]: node["fieldValue"] for node in payload["nodeInfoList"]}
    assert nodes["503"] == "2048"
    assert nodes["752"] == "0"
    assert nodes["753"] == "0"
    assert nodes["739"] == "remote-left-audio"
    assert nodes["738"] == "remote-right-audio"


def test_digital_human_adapter_uses_video_output_and_rejects_bad_parameters():
    workflow = get_workflow("digital_human")
    with pytest.raises(ValueError):
        workflow.validate_parameters(
            {"prompt": "", "start_time": "0:00", "end_time": "0:01"},
            {"audio_duration_seconds": 2},
        )
    with pytest.raises(ValueError, match="总体模式"):
        workflow.validate_parameters(
            {
                "prompt": "提示词",
                "start_time": "0:00",
                "end_time": "0:01",
                "overall_mode": "9",
            },
            {"audio_duration_seconds": 2},
        )
    with pytest.raises(ValueError, match="正整数"):
        workflow.validate_parameters(
            {
                "prompt": "提示词",
                "start_time": "0:00",
                "end_time": "0:01",
                "resolution": "0",
            },
            {"audio_duration_seconds": 2},
        )
    output = workflow.select_output(
        SimpleNamespace(),
        {
            "results": [
                {"outputType": "png", "url": "https://example.test/preview.png"},
                {"outputType": "mp4", "url": "https://example.test/result.mp4"},
            ]
        },
    )
    assert output is not None
    assert output.url.endswith("result.mp4")
    assert output.extension == "mp4"

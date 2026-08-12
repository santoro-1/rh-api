from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services.security import encrypt_secret
from app.services.workflow_configs import get_user_workflow_config
from app.workflows import get_workflow, list_workflows
from app.workflows.base import WorkflowAsset
from app.workflows.seedvr2_upscale import (
    SEEDVR2_AI_APP_ID,
    seedvr2_upscale_workflow,
)
from app.workflows.digital_human import generation_tail_padding_seconds


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
    assert expected_nodes["753"] == "1"
    assert "739" not in expected_nodes
    assert "738" not in expected_nodes
    assert all(node["fieldValue"] != "None" for node in payload["nodeInfoList"])
    assert "752" not in expected_nodes


def test_digital_human_adapter_preserves_frozen_24g_recipe():
    workflow = get_workflow("digital_human")
    parameters = workflow.validate_parameters(
        {"prompt": "历史任务", "start_time": "0:00", "end_time": "0:10"},
        {"audio_duration_seconds": 10.0},
    )
    payload_input = workflow.serialize_input(
        [
            WorkflowAsset("image", "image", "uploads/image.png", "image.png"),
            WorkflowAsset("audio", "audio", "uploads/audio.mp3", "audio.mp3"),
        ],
        parameters,
        {"audio_duration_seconds": 10.0},
    )
    payload_input["parameters"]["instance_type"] = "default"
    task = SimpleNamespace(
        input_payload=json.dumps(payload_input, ensure_ascii=False),
        image_path="legacy/image.png",
        audio_path="legacy/audio.mp3",
        image_original_name="image.png",
        audio_original_name="audio.mp3",
        audio_duration_seconds=10.0,
        start_seconds=0,
        end_seconds=10,
        prompt="历史任务",
    )

    payload = workflow.build_payload(
        task,
        {"image": "remote-image", "audio": "remote-audio"},
        ai_app_id="app-id",
        instance_type="default",
        settings={},
    )

    assert payload["instanceType"] == "default"


def test_digital_human_adapter_normalizes_seedvr2_switch():
    workflow = get_workflow("digital_human")

    parameters = workflow.validate_parameters(
        {
            "prompt": "不开启放大",
            "start_time": "0:00",
            "end_time": "0:10",
            "instance_type": "default",
            "seedvr2_enabled": "false",
        },
        {"audio_duration_seconds": 10.0},
    )

    assert parameters["instance_type"] == "default"
    assert parameters["seedvr2_enabled"] is False


def test_seedvr2_adapter_is_fixed_to_48g_and_maps_video_nodes():
    payload = seedvr2_upscale_workflow.build_payload("openapi/source.mp4")
    assert SEEDVR2_AI_APP_ID == "2064116518987845634"
    assert payload["instanceType"] == "plus"
    assert payload["usePersonalQueue"] is False
    nodes = {
        (node["nodeId"], node["fieldName"]): node["fieldValue"]
        for node in payload["nodeInfoList"]
    }
    assert nodes[("46", "video")] == "openapi/source.mp4"
    assert nodes[("108", "select")] == "1"
    assert nodes[("112", "value")] == "1920"


def test_seedvr2_adapter_requires_one_unambiguous_video_output():
    output = seedvr2_upscale_workflow.select_output(
        {
            "results": [
                {"outputType": "png", "url": "https://x/preview.png"},
                {
                    "nodeId": "200",
                    "outputType": "mp4",
                    "url": "https://x/final.mp4",
                },
            ]
        }
    )
    assert output is not None
    assert output.url == "https://x/final.mp4"
    assert seedvr2_upscale_workflow.select_output(
        {"results": [{"outputType": "png", "url": "https://x/preview.png"}]}
    ) is None
    assert seedvr2_upscale_workflow.select_output(
        {
            "results": [
                {"outputType": "mp4", "url": "https://x/a.mp4"},
                {"outputType": "mov", "url": "https://x/b.mov"},
            ]
        }
    ) is None


def test_digital_human_adapter_maps_dual_person_audio_and_selected_modes():
    workflow = get_workflow("digital_human")
    parameters = workflow.validate_parameters(
        {
            "prompt": "双人对话",
            "start_time": "0:00",
            "end_time": "0:10",
            "resolution": "2048",
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
    assert nodes["753"] == "0"
    assert nodes["739"] == "remote-left-audio"
    assert nodes["738"] == "remote-right-audio"
    assert "752" not in nodes


def test_digital_human_adapter_uses_video_output_and_rejects_bad_parameters():
    workflow = get_workflow("digital_human")
    with pytest.raises(ValueError):
        workflow.validate_parameters(
            {"prompt": "", "start_time": "0:00", "end_time": "0:01"},
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


def test_digital_human_batch_payload_adds_safe_silent_tail_window():
    workflow = get_workflow("digital_human")
    parameters = workflow.validate_parameters(
        {"prompt": "完整口播", "start_time": "0:00", "end_time": "0:31"},
        {"audio_duration_seconds": 30.9},
    )
    task = SimpleNamespace(
        input_payload=json.dumps(
            workflow.serialize_input(
                [
                    WorkflowAsset("image", "image", "image.png", "image.png"),
                    WorkflowAsset("audio", "audio", "audio.mp3", "audio.mp3"),
                ],
                parameters,
                {"audio_duration_seconds": 30.9},
            )
        ),
        audio_duration_seconds=30.9,
        start_seconds=0,
        end_seconds=31,
        prompt="完整口播",
        segment_id="segment-1",
        batch_item_id=None,
    )
    payload = workflow.build_payload(
        task,
        {"image": "remote-image", "audio": "remote-audio-with-tail"},
        ai_app_id="app-id",
        instance_type="default",
        settings={},
    )
    nodes = {
        (node["nodeId"], node["fieldName"]): node["fieldValue"]
        for node in payload["nodeInfoList"]
    }
    assert nodes[("341", "end_time")] == "0:32"


def test_timestamped_workbench_payload_uses_ceiling_without_silent_tail():
    workflow = get_workflow("digital_human")
    parameters = workflow.validate_parameters(
        {
            "prompt": "完整口播",
            "start_time": "0:00",
            "end_time": "0:25",
            "timing_mode": "exact_timestamps",
        },
        {"audio_duration_seconds": 24.4},
    )
    task = SimpleNamespace(
        input_payload=json.dumps(
            workflow.serialize_input(
                [
                    WorkflowAsset("image", "image", "image.png", "image.png"),
                    WorkflowAsset("audio", "audio", "audio.mp3", "audio.mp3"),
                ],
                parameters,
                {"audio_duration_seconds": 24.4},
            )
        ),
        audio_duration_seconds=24.4,
        start_seconds=0,
        end_seconds=25,
        prompt="完整口播",
        segment_id="segment-1",
        batch_item_id=None,
    )
    assert generation_tail_padding_seconds(task) == 0.0
    payload = workflow.build_payload(
        task,
        {"image": "remote-image", "audio": "remote-audio"},
        ai_app_id="app-id",
        instance_type="default",
        settings={},
    )
    nodes = {
        (node["nodeId"], node["fieldName"]): node["fieldValue"]
        for node in payload["nodeInfoList"]
    }
    assert nodes[("341", "end_time")] == "0:25"


def test_ltx_lip_sync_adapter_maps_custom_audio_and_output_node():
    workflow = get_workflow("ltx_lip_sync")
    assert workflow.submission_type == "workflow"
    assert workflow.default_prompt.startswith("一名人物用中文说")
    assert "动作" not in workflow.default_prompt
    with pytest.raises(ValueError, match="必须上传自定义音频"):
        workflow.validate_parameters(
            {"prompt": "测试"},
            {"has_custom_audio": False},
        )
    parameters = workflow.validate_parameters(
        {"prompt": "测试", "instance_type": "plus"},
        {"has_custom_audio": True},
    )
    serialized = workflow.serialize_input(
        [
            WorkflowAsset("video", "video", "uploads/source.mp4", "source.mp4"),
            WorkflowAsset("audio", "audio", "uploads/voice.mp3", "voice.mp3"),
        ],
        parameters,
        {"has_custom_audio": True},
    )
    task = SimpleNamespace(
        input_payload=json.dumps(serialized, ensure_ascii=False),
        prompt="测试",
    )
    assert [asset.name for asset in workflow.assets_for_task(task)] == [
        "video",
        "audio",
    ]
    payload = workflow.build_payload(
        task,
        {
            "video": "openapi/source.mp4",
            "audio": "openapi/voice.mp3",
        },
        ai_app_id="2080551073030434817",
        instance_type="plus",
        settings={},
    )
    assert any(
        node["nodeId"] == "237"
        and node["fieldName"] == "video"
        and node["fieldValue"] == "openapi/source.mp4"
        for node in payload["nodeInfoList"]
    )
    assert any(
        node["nodeId"] == "246"
        and node["fieldName"] == "audio"
        and node["fieldValue"] == "openapi/voice.mp3"
        for node in payload["nodeInfoList"]
    )
    assert any(
        node["nodeId"] == "222"
        and node["fieldName"] == "text"
        and node["fieldValue"] == "测试"
        for node in payload["nodeInfoList"]
    )
    assert len(payload["nodeInfoList"]) == 3
    assert payload["addMetadata"] is True
    assert payload["instanceType"] == "plus"
    assert payload["usePersonalQueue"] is False
    assert "retainSeconds" not in payload
    assert "accessPassword" not in payload
    encrypted_payload = workflow.build_payload(
        task,
        {
            "video": "openapi/source.mp4",
            "audio": "openapi/voice.mp3",
        },
        ai_app_id="2080551073030434817",
        instance_type="plus",
        settings={
            "access_password_encrypted": encrypt_secret(
                "private-workflow-password"
            )
        },
    )
    assert encrypted_payload["accessPassword"] == "private-workflow-password"
    assert "access_password_encrypted" not in encrypted_payload
    output = workflow.select_output(
        task,
        {
            "results": [
                {"nodeId": "999", "outputType": "mp4", "url": "https://x/fallback.mp4"},
                {"nodeId": "260", "outputType": "mp4", "url": "https://x/final.mp4"},
            ]
        },
    )
    assert output is not None
    assert output.url.endswith("final.mp4")

    default_parameters = workflow.validate_parameters(
        {"prompt": "普通版测试", "instance_type": "default"},
        {"has_custom_audio": True},
    )
    assert default_parameters["instance_type"] == "default"


def test_ltx_obsolete_builtin_prompt_is_replaced_but_custom_prompt_is_kept():
    legacy_prompt = (
        "人物自然地说话，口型与语音一致，保持原视频动作、构图和镜头稳定。"
    )
    config = SimpleNamespace(
        workflow_key="ltx_lip_sync",
        ai_app_id="workflow-id",
        instance_type="default",
        default_prompt=legacy_prompt,
        is_enabled=True,
        settings_json="{}",
    )
    user = SimpleNamespace(workflow_configs=[config], runninghub_config=None)
    resolved = get_user_workflow_config(user, "ltx_lip_sync")
    assert resolved.default_prompt == get_workflow("ltx_lip_sync").default_prompt

    config.default_prompt = "一名男性用英语说：“Hello.”"
    resolved = get_user_workflow_config(user, "ltx_lip_sync")
    assert resolved.default_prompt == config.default_prompt

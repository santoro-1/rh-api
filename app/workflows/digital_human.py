from __future__ import annotations

import json
from typing import Any

from app.models import GenerationTask
from app.services.audio import format_timecode, validate_time_range
from app.workflows.base import WorkflowAsset, WorkflowOutput


class DigitalHumanWorkflow:
    key = "digital_human"
    display_name = "数字人视频"
    default_ai_app_id = "2062251097452007426"
    default_prompt = "人物自然地说话，表情自然，动作自然，镜头保持稳定。"
    submission_type = "ai-app"

    # This is intentionally private to this adapter.  Other workflows define
    # their own node map rather than extending generic task or worker code.
    _NODES = {
        "resolution": ("503", "value", "1024", "最长分辨率"),
        "person_mode": ("753", "select", "1", "单双人模式选择"),
        "image": ("240", "image", None, "参考图像"),
        "audio": ("339", "audio", None, "总参考音频"),
        "start_time": ("341", "start_time", None, "音频开始时间"),
        "end_time": ("341", "end_time", None, "音频结束时间"),
        "prompt": ("422", "text", None, "提示词"),
    }
    _DUAL_PERSON_AUDIO_NODES = {
        "left_audio": ("739", "audio", "左边人物音频"),
        "right_audio": ("738", "audio", "右边人物音频"),
    }

    def validate_parameters(
        self, parameters: dict[str, Any], asset_metadata: dict[str, Any]
    ) -> dict[str, Any]:
        prompt = str(parameters.get("prompt") or "").strip()
        if not 1 <= len(prompt) <= 5000:
            raise ValueError("提示词长度必须在 1 到 5000 个字符之间")
        duration = float(asset_metadata.get("audio_duration_seconds") or 0)
        start_seconds, end_seconds = validate_time_range(
            str(parameters.get("start_time") or ""),
            str(parameters.get("end_time") or ""),
            duration,
        )
        person_mode = str(parameters.get("person_mode", "1")).strip()
        if person_mode not in {"0", "1"}:
            raise ValueError("单双人模式不合法")
        resolution = str(parameters.get("resolution", "1024")).strip()
        try:
            if int(resolution) <= 0:
                raise ValueError
        except ValueError as exc:
            raise ValueError("最长分辨率必须是正整数") from exc
        requested_instance_type = str(
            parameters.get("instance_type") or "default"
        ).strip()
        if requested_instance_type != "default":
            raise ValueError("数字人工作流当前固定使用 Stand 运行（24G）")
        return {
            "prompt": prompt,
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "start_time": format_timecode(start_seconds),
            "end_time": format_timecode(end_seconds),
            "person_mode": person_mode,
            "resolution": resolution,
            "instance_type": "default",
        }

    def serialize_input(
        self,
        assets: list[WorkflowAsset],
        parameters: dict[str, Any],
        asset_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "assets": {
                asset.name: {
                    "kind": asset.kind,
                    "path": asset.relative_path,
                    "original_name": asset.original_name,
                }
                for asset in assets
            },
            "parameters": parameters,
            "metadata": asset_metadata,
        }

    def _input(self, task: GenerationTask) -> dict[str, Any]:
        if task.input_payload:
            try:
                value = json.loads(task.input_payload)
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                pass
        # Compatibility for tasks created before workflow adapters existed.
        return {
            "assets": {
                "image": {
                    "kind": "image",
                    "path": task.image_path,
                    "original_name": task.image_original_name,
                },
                "audio": {
                    "kind": "audio",
                    "path": task.audio_path,
                    "original_name": task.audio_original_name,
                },
            },
            "parameters": {
                "prompt": task.prompt,
                "start_seconds": task.start_seconds,
                "end_seconds": task.end_seconds,
                "start_time": format_timecode(task.start_seconds),
                "end_time": format_timecode(task.end_seconds),
            },
            "metadata": {"audio_duration_seconds": task.audio_duration_seconds},
        }

    def assets_for_task(self, task: GenerationTask) -> list[WorkflowAsset]:
        assets = self._input(task).get("assets")
        if not isinstance(assets, dict):
            raise ValueError("数字人任务缺少素材定义")
        parameters = self._input(task).get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError("数字人任务参数不合法")
        names = ["image", "audio"]
        if str(parameters.get("person_mode", "1")) == "0":
            names.extend(["left_audio", "right_audio"])
        result: list[WorkflowAsset] = []
        for name in names:
            value = assets.get(name)
            if not isinstance(value, dict) or not value.get("path"):
                raise ValueError(f"数字人任务缺少 {name} 素材")
            result.append(
                WorkflowAsset(
                    name=name,
                    kind=str(value.get("kind") or name),
                    relative_path=str(value["path"]),
                    original_name=str(value.get("original_name") or name),
                )
            )
        return result

    def build_payload(
        self,
        task: GenerationTask,
        uploaded_files: dict[str, str],
        *,
        ai_app_id: str,
        instance_type: str,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        del ai_app_id, settings  # The client owns the URL; this app has no extra settings yet.
        if instance_type not in {"default", "plus"}:
            raise ValueError("实例类型不合法")
        values = self._input(task).get("parameters")
        if not isinstance(values, dict):
            raise ValueError("数字人任务参数不合法")
        selected_instance_type = str(values.get("instance_type") or "default").strip()
        if selected_instance_type != "default":
            raise ValueError("数字人工作流当前固定使用 Stand 运行（24G）")
        values = {
            **values,
            "image": uploaded_files["image"],
            "audio": uploaded_files["audio"],
            "start_time": str(values.get("start_time") or format_timecode(task.start_seconds)),
            "end_time": str(values.get("end_time") or format_timecode(task.end_seconds)),
            "prompt": str(values.get("prompt") or task.prompt),
        }
        nodes = [
            {
                "nodeId": node_id,
                "fieldName": field_name,
                "fieldValue": values.get(name, default_value),
                "description": description,
            }
            for name, (node_id, field_name, default_value, description) in self._NODES.items()
        ]
        if str(values.get("person_mode", "1")) == "0":
            nodes.extend(
                {
                    "nodeId": node_id,
                    "fieldName": field_name,
                    "fieldValue": uploaded_files[name],
                    "description": description,
                }
                for name, (
                    node_id,
                    field_name,
                    description,
                ) in self._DUAL_PERSON_AUDIO_NODES.items()
            )
        return {
            "nodeInfoList": nodes,
            "instanceType": "default",
            "usePersonalQueue": False,
        }

    def select_output(self, task: GenerationTask, result: dict[str, Any]) -> WorkflowOutput | None:
        del task
        results = result.get("results")
        if not isinstance(results, list):
            return None
        # Prefer a video result if the workflow emits intermediate images too.
        candidates = [item for item in results if isinstance(item, dict) and item.get("url")]
        if not candidates:
            return None
        item = next(
            (
                candidate
                for candidate in candidates
                if str(candidate.get("outputType") or "").lower() in {"mp4", "mov", "webm"}
            ),
            candidates[0],
        )
        extension = str(item.get("outputType") or "mp4").lower().lstrip(".")
        return WorkflowOutput(url=str(item["url"]), extension=extension, metadata=item)

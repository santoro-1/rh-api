from __future__ import annotations

import json
from typing import Any

from app.models import GenerationTask
from app.workflows.base import WorkflowAsset, WorkflowOutput


class LtxLipSyncWorkflow:
    """Adapter for the private Painter LTX 2.3 video lip-sync workflow."""

    key = "ltx_lip_sync"
    display_name = "视频对口型"
    default_ai_app_id = "2080551073030434817"
    default_prompt = "人物自然地说话，口型与语音一致，保持原视频动作、构图和镜头稳定。"
    submission_type = "workflow"
    output_node_id = "260"

    _NODES = {
        "video": ("237", "video", "源视频"),
        "audio": ("246", "audio", "自定义音频"),
        "prompt": ("222", "text", "画面及对白提示词"),
    }

    def validate_parameters(
        self, parameters: dict[str, Any], asset_metadata: dict[str, Any]
    ) -> dict[str, Any]:
        prompt = str(parameters.get("prompt") or "").strip()
        if not 1 <= len(prompt) <= 5000:
            raise ValueError("提示词长度必须在 1 到 5000 个字符之间")
        if not asset_metadata.get("has_custom_audio"):
            raise ValueError("必须上传自定义音频")
        requested_instance_type = str(
            parameters.get("instance_type") or "plus"
        ).strip()
        if requested_instance_type not in {"default", "plus"}:
            raise ValueError("实例类型只能为普通版 default 或 Plus")
        return {
            "prompt": prompt,
            "instance_type": requested_instance_type,
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
        if not task.input_payload:
            raise ValueError("LTX 对口型任务缺少输入定义")
        try:
            value = json.loads(task.input_payload)
        except json.JSONDecodeError as exc:
            raise ValueError("LTX 对口型任务输入格式错误") from exc
        if not isinstance(value, dict):
            raise ValueError("LTX 对口型任务输入格式错误")
        return value

    def assets_for_task(self, task: GenerationTask) -> list[WorkflowAsset]:
        workflow_input = self._input(task)
        assets = workflow_input.get("assets")
        parameters = workflow_input.get("parameters")
        if not isinstance(assets, dict) or not isinstance(parameters, dict):
            raise ValueError("LTX 对口型任务素材定义错误")
        names = ["video", "audio"]
        result = []
        for name in names:
            value = assets.get(name)
            if not isinstance(value, dict) or not value.get("path"):
                raise ValueError(f"LTX 对口型任务缺少 {name} 素材")
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
        del ai_app_id, settings
        if instance_type not in {"default", "plus"}:
            raise ValueError("实例类型不合法")
        values = self._input(task).get("parameters")
        if not isinstance(values, dict):
            raise ValueError("LTX 对口型任务参数不合法")
        selected_instance_type = str(
            values.get("instance_type") or instance_type
        ).strip()
        if selected_instance_type not in {"default", "plus"}:
            raise ValueError("实例类型不合法")
        nodes = [
            {
                "nodeId": self._NODES["video"][0],
                "fieldName": self._NODES["video"][1],
                "fieldValue": uploaded_files["video"],
                "description": self._NODES["video"][2],
            },
            {
                "nodeId": self._NODES["prompt"][0],
                "fieldName": self._NODES["prompt"][1],
                "fieldValue": str(values.get("prompt") or task.prompt),
                "description": self._NODES["prompt"][2],
            },
        ]
        nodes.append(
            {
                "nodeId": self._NODES["audio"][0],
                "fieldName": self._NODES["audio"][1],
                "fieldValue": uploaded_files["audio"],
                "description": self._NODES["audio"][2],
            }
        )
        return {
            "addMetadata": True,
            "nodeInfoList": nodes,
            "instanceType": selected_instance_type,
            "usePersonalQueue": False,
        }

    def select_output(
        self, task: GenerationTask, result: dict[str, Any]
    ) -> WorkflowOutput | None:
        del task
        results = result.get("results")
        if not isinstance(results, list):
            return None
        candidates = [
            item for item in results if isinstance(item, dict) and item.get("url")
        ]
        if not candidates:
            return None
        item = next(
            (
                candidate
                for candidate in candidates
                if str(candidate.get("nodeId") or "") == self.output_node_id
                and str(candidate.get("outputType") or "").lower()
                in {"mp4", "mov", "webm"}
            ),
            next(
                (
                    candidate
                    for candidate in candidates
                    if str(candidate.get("outputType") or "").lower()
                    in {"mp4", "mov", "webm"}
                ),
                candidates[0],
            ),
        )
        extension = str(item.get("outputType") or "mp4").lower().lstrip(".")
        return WorkflowOutput(
            url=str(item["url"]), extension=extension, metadata=item
        )

from __future__ import annotations

from typing import Any

from app.workflows.base import WorkflowOutput


SEEDVR2_AI_APP_ID = "2064116518987845634"
SEEDVR2_INSTANCE_TYPE = "plus"
SEEDVR2_MAX_RESOLUTION = "1920"


class SeedVR2UpscaleWorkflow:
    """Internal one-input video enhancement adapter; it is not user-selectable."""

    key = "seedvr2_upscale"
    display_name = "SeedVR2 视频清晰化"
    ai_app_id = SEEDVR2_AI_APP_ID
    submission_type = "ai-app"

    def build_payload(self, uploaded_video: str) -> dict[str, Any]:
        if not str(uploaded_video).strip():
            raise ValueError("SeedVR2 缺少已上传的视频文件名")
        return {
            "nodeInfoList": [
                {
                    "nodeId": "46",
                    "fieldName": "video",
                    "fieldValue": str(uploaded_video),
                    "description": "上传视频",
                },
                {
                    "nodeId": "108",
                    "fieldName": "select",
                    "fieldValue": "1",
                    "description": "模型切换",
                },
                {
                    "nodeId": "112",
                    "fieldName": "value",
                    "fieldValue": SEEDVR2_MAX_RESOLUTION,
                    "description": "最大分辨率（不建议超过1920）",
                },
            ],
            # Product policy: SeedVR2 always uses the 48G instance.
            "instanceType": SEEDVR2_INSTANCE_TYPE,
            "usePersonalQueue": False,
        }

    def select_output(self, result: dict[str, Any]) -> WorkflowOutput | None:
        results = result.get("results")
        if not isinstance(results, list):
            return None
        videos = [
            item
            for item in results
            if isinstance(item, dict)
            and item.get("url")
            and str(item.get("outputType") or "").lower().lstrip(".")
            in {"mp4", "mov", "webm"}
        ]
        # Never fall back to a PNG or silently choose between ambiguous videos.
        if len(videos) != 1:
            return None
        item = videos[0]
        extension = str(item["outputType"]).lower().lstrip(".")
        return WorkflowOutput(
            url=str(item["url"]),
            extension=extension,
            metadata=item,
        )


seedvr2_upscale_workflow = SeedVR2UpscaleWorkflow()

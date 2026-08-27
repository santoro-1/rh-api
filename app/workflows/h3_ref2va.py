from __future__ import annotations

import hashlib
import json
from typing import Any

from app.models import GenerationTask
from app.services.h3.duration import plan_h3_duration
from app.services.h3.graph import (
    H3_ADAPTER_VERSION,
    H3_OUTPUT_NODE_ID,
    H3_WORKFLOW_TEMPLATE_CANONICAL_SHA256,
    H3_WORKFLOW_TEMPLATE_ID,
    H3_WORKFLOW_TEMPLATE_VERSION,
    H3GraphBuildRequest,
    load_default_h3_graph_builder,
)
from app.services.h3.prompt import (
    H3_LOOP_ANCHOR_PROMPT_TEMPLATE_VERSION,
    H3_MANUAL_PROMPT_OVERRIDE_VERSION,
    H3_PROMPT_TEMPLATE_VERSION,
    H3PromptRequest,
    compile_loop_anchor_ref2va_prompt,
    compile_ref2va_prompt,
    normalize_h3_prompt_override,
    validate_h3_prompt_request,
)
from app.workflows.base import WorkflowAsset, WorkflowOutput


H3_REF2VA_WORKFLOW_KEY = "minimax_h3_ref2va"
H3_DEFAULT_INSTANCE_TYPE = "plus"
H3_DEFAULT_ASPECT_RATIO = "9:16 (Portrait Widescreen)"
H3_DEFAULT_MEGAPIXELS = 1.0
H3_DEFAULT_MULTIPLE = 32
H3_DEFAULT_GENERATION_TAIL_SECONDS = 0.1
H3_DEFAULT_CONTINUITY_MODE = "loop_anchor"
H3_CONTINUITY_MODES = {"loop_anchor", "fast", "soft_chain"}
H3_OUTPUT_TYPE = "mp4"


def _input_payload(task: GenerationTask) -> dict[str, Any]:
    try:
        value = json.loads(str(task.input_payload or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("H3 任务输入快照不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("H3 任务输入快照不合法")
    return value


def _integer(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field}不合法")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}不合法") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{field}不合法")
    if isinstance(value, float) and value != result:
        raise ValueError(f"{field}不合法")
    if isinstance(value, str) and str(result) != value.strip():
        raise ValueError(f"{field}不合法")
    return result


def _boolean(value: object, field: str, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if value in {0, 1}:
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"true", "yes", "on"}:
        return True
    if normalized in {"false", "no", "off"}:
        return False
    raise ValueError(f"{field}不合法")


class H3Ref2VAWorkflow:
    key = H3_REF2VA_WORKFLOW_KEY
    display_name = "MiniMax H3 多参考视频"
    # H3 workflow IDs are account capabilities and must be configured explicitly.
    default_ai_app_id = ""
    default_prompt = "由 H3 PromptProfile 根据每段台词自动编译"
    submission_type = "raw-workflow"

    def validate_parameters(
        self,
        parameters: dict[str, Any],
        asset_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        segment_text = str(parameters.get("segment_text") or "").strip()
        user_direction = str(parameters.get("user_direction") or "").strip()
        prompt_override = normalize_h3_prompt_override(
            parameters.get("prompt_override")
        )
        try:
            audio_duration_seconds = float(
                asset_metadata.get("audio_duration_seconds") or 0
            )
            generation_tail_seconds = float(
                parameters.get("generation_tail_seconds", H3_DEFAULT_GENERATION_TAIL_SECONDS)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("H3 分段音频时长或生成余量不合法") from exc
        duration = plan_h3_duration(audio_duration_seconds, generation_tail_seconds)

        segment_index = _integer(
            parameters.get("segment_index", 0),
            "H3 分段序号",
            minimum=0,
            maximum=9999,
        )
        segment_count = _integer(
            parameters.get("segment_count", 1),
            "H3 分段总数",
            minimum=1,
            maximum=10000,
        )
        if segment_index >= segment_count:
            raise ValueError("H3 分段序号不能大于等于分段总数")
        identity_image_count = _integer(
            asset_metadata.get("identity_image_count", 0),
            "H3 人物参考图数量",
            minimum=0,
            maximum=5,
        )
        has_continuity_anchor = _boolean(
            asset_metadata.get("has_continuity_anchor"),
            "H3 连续性尾帧标记",
        )
        continuity_mode = str(
            parameters.get("continuity_mode") or H3_DEFAULT_CONTINUITY_MODE
        ).strip()
        if continuity_mode not in H3_CONTINUITY_MODES:
            raise ValueError("H3 连续性模式不合法")
        if has_continuity_anchor and continuity_mode != "soft_chain":
            raise ValueError("H3 尾帧参考只允许用于 soft_chain 模式")
        if has_continuity_anchor and segment_index == 0:
            raise ValueError("H3 第一段不能使用上一段尾帧参考")
        if continuity_mode == "loop_anchor" and identity_image_count < 1:
            raise ValueError("H3 首尾同图模式至少需要 1 张参考图")

        instance_type = str(
            parameters.get("instance_type") or H3_DEFAULT_INSTANCE_TYPE
        ).strip()
        if instance_type not in {"default", "plus"}:
            raise ValueError("H3 实例类型不合法")
        aspect_ratio = str(
            parameters.get("aspect_ratio") or H3_DEFAULT_ASPECT_RATIO
        ).strip()
        try:
            megapixels = float(
                parameters.get("megapixels", H3_DEFAULT_MEGAPIXELS)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("H3 megapixels 参数不合法") from exc
        multiple = _integer(
            parameters.get("multiple", H3_DEFAULT_MULTIPLE),
            "H3 分辨率 multiple",
            minimum=1,
            maximum=4096,
        )
        seed = _integer(
            parameters.get("seed", 0),
            "H3 Seed",
            minimum=0,
            maximum=2**64 - 1,
        )

        prompt_request = H3PromptRequest(
            segment_text=segment_text,
            segment_duration_seconds=audio_duration_seconds,
            segment_index=segment_index,
            segment_count=segment_count,
            identity_image_count=identity_image_count,
            has_continuity_anchor=has_continuity_anchor,
            user_direction="" if prompt_override else user_direction,
        )
        effective_user_direction = prompt_request.user_direction
        if prompt_override:
            validate_h3_prompt_request(prompt_request)
            prompt = prompt_override
            prompt_template_version = H3_MANUAL_PROMPT_OVERRIDE_VERSION
        elif continuity_mode == "loop_anchor":
            prompt = compile_loop_anchor_ref2va_prompt(prompt_request)
            prompt_template_version = H3_LOOP_ANCHOR_PROMPT_TEMPLATE_VERSION
        else:
            prompt = compile_ref2va_prompt(prompt_request)
            prompt_template_version = H3_PROMPT_TEMPLATE_VERSION
        # Reuse graph request validation before a task can enter the paid queue.
        load_default_h3_graph_builder().build(
            H3GraphBuildRequest(
                prompt=prompt,
                reference_video="preflight/reference.mp4",
                reference_audio="preflight/segment.wav",
                reference_images=tuple(
                    f"preflight/image-{index + 1}.png"
                    for index in range(identity_image_count)
                ),
                continuity_anchor=(
                    "preflight/continuity-anchor.png"
                    if has_continuity_anchor
                    else None
                ),
                audio_duration_seconds=audio_duration_seconds,
                generation_tail_seconds=generation_tail_seconds,
                aspect_ratio=aspect_ratio,
                megapixels=megapixels,
                multiple=multiple,
                seed=seed,
            )
        )
        return {
            "segment_text": segment_text,
            "segment_index": segment_index,
            "segment_count": segment_count,
            "user_direction": effective_user_direction,
            "continuity_mode": continuity_mode,
            "identity_image_count": identity_image_count,
            "has_continuity_anchor": has_continuity_anchor,
            "audio_duration_seconds": audio_duration_seconds,
            "generation_tail_seconds": generation_tail_seconds,
            "requested_generation_duration_seconds": (
                duration.requested_generation_duration_seconds
            ),
            "quantized_frame_count": duration.quantized_frame_count,
            "effective_generation_duration_seconds": (
                duration.effective_generation_duration_seconds
            ),
            "aspect_ratio": aspect_ratio,
            "megapixels": megapixels,
            "multiple": multiple,
            "seed": seed,
            "instance_type": instance_type,
            "seedvr2_enabled": False,
            "start_seconds": 0.0,
            "end_seconds": audio_duration_seconds,
            "prompt": prompt,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt_template_version": prompt_template_version,
            "workflow_template_id": H3_WORKFLOW_TEMPLATE_ID,
            "workflow_template_version": H3_WORKFLOW_TEMPLATE_VERSION,
            "workflow_template_sha256": H3_WORKFLOW_TEMPLATE_CANONICAL_SHA256,
            "adapter_version": H3_ADAPTER_VERSION,
        }

    def serialize_input(
        self,
        assets: list[WorkflowAsset],
        parameters: dict[str, Any],
        asset_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        assets_by_name = {asset.name: asset for asset in assets}
        if len(assets_by_name) != len(assets):
            raise ValueError("H3 素材槽位名称不能重复")
        required = {"video", "audio"}
        identity_count = int(parameters["identity_image_count"])
        required.update(
            f"identity_image_{index + 1}" for index in range(identity_count)
        )
        if parameters["has_continuity_anchor"]:
            required.add("continuity_anchor")
        if set(assets_by_name) != required:
            missing = sorted(required - set(assets_by_name))
            unexpected = sorted(set(assets_by_name) - required)
            details = []
            if missing:
                details.append("缺少 " + ", ".join(missing))
            if unexpected:
                details.append("多出 " + ", ".join(unexpected))
            raise ValueError("H3 素材槽位与冻结参数不一致：" + "；".join(details))
        return {
            "assets": {
                name: {
                    "kind": asset.kind,
                    "path": asset.relative_path,
                    "original_name": asset.original_name,
                }
                for name, asset in assets_by_name.items()
            },
            "parameters": parameters,
            "metadata": dict(asset_metadata),
        }

    def assets_for_task(self, task: GenerationTask) -> list[WorkflowAsset]:
        task_input = _input_payload(task)
        assets = task_input.get("assets")
        parameters = task_input.get("parameters")
        if not isinstance(assets, dict) or not isinstance(parameters, dict):
            raise ValueError("H3 任务缺少素材或参数快照")
        identity_count = int(parameters.get("identity_image_count") or 0)
        names = ["video", "audio"]
        names.extend(f"identity_image_{index + 1}" for index in range(identity_count))
        if parameters.get("has_continuity_anchor"):
            names.append("continuity_anchor")
        result = []
        for name in names:
            value = assets.get(name)
            if not isinstance(value, dict) or not value.get("path"):
                raise ValueError(f"H3 任务缺少 {name} 素材")
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
        task_input = _input_payload(task)
        parameters = task_input.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError("H3 任务参数快照不合法")
        selected_instance_type = str(
            parameters.get("instance_type") or instance_type
        ).strip()
        if selected_instance_type not in {"default", "plus"}:
            raise ValueError("H3 实例类型不合法")
        identity_count = int(parameters.get("identity_image_count") or 0)
        images = [
            uploaded_files[f"identity_image_{index + 1}"]
            for index in range(identity_count)
        ]
        result = load_default_h3_graph_builder().build(
            H3GraphBuildRequest(
                prompt=str(parameters.get("prompt") or task.prompt),
                reference_video=uploaded_files["video"],
                reference_audio=uploaded_files["audio"],
                reference_images=tuple(images),
                continuity_anchor=(
                    uploaded_files["continuity_anchor"]
                    if parameters.get("has_continuity_anchor")
                    else None
                ),
                audio_duration_seconds=float(parameters["audio_duration_seconds"]),
                generation_tail_seconds=float(parameters["generation_tail_seconds"]),
                aspect_ratio=str(parameters["aspect_ratio"]),
                megapixels=float(parameters["megapixels"]),
                multiple=int(parameters["multiple"]),
                seed=int(parameters["seed"]),
            )
        )
        return {
            "workflow": result.workflow_json,
            "addMetadata": True,
            "instanceType": selected_instance_type,
            "usePersonalQueue": False,
        }

    def select_output(
        self,
        task: GenerationTask,
        result: dict[str, Any],
    ) -> WorkflowOutput | None:
        del task
        results = result.get("results")
        if not isinstance(results, list):
            return None
        candidates = [
            item
            for item in results
            if isinstance(item, dict)
            and item.get("url")
            and str(item.get("nodeId") or "") == H3_OUTPUT_NODE_ID
            and str(item.get("outputType") or "").lower().lstrip(".")
            == H3_OUTPUT_TYPE
        ]
        if not candidates:
            return None
        selected = candidates[0]
        extension = H3_OUTPUT_TYPE
        return WorkflowOutput(
            url=str(selected["url"]),
            extension=extension,
            metadata=selected,
        )

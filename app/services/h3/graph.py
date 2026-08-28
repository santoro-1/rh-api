from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from app.services.h3.duration import H3DurationPlan, plan_h3_duration


H3_WORKFLOW_TEMPLATE_ID = "minimax_h3_ref2va_6image_4step_20260827"
H3_WORKFLOW_TEMPLATE_VERSION = "h3.workflow.ref2va.20260827.v2"
H3_WORKFLOW_TEMPLATE_CANONICAL_SHA256 = (
    "13146c4aabd9da9b0ec5d05e8a392b720a3921fe5020a8d6fab1b3b828b3830b"
)
H3_ADAPTER_VERSION = "h3.runninghub.raw.v4"
H3_OUTPUT_NODE_ID = "387"
H3_OUTPUT_TYPE = "mp4"
H3_SAMPLING_STEPS = 4
H3_DEFAULT_TEMPLATE_PATH = (
    Path(__file__).with_name("templates")
    / "minimax_h3_ref2va_6image_20260822.json"
)
H3_DURATION_EXPRESSION = (
    "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17"
)


_IMAGE_BRANCHES = (
    ("97", "99", "100"),
    ("101", "102", "103"),
    ("132", "129", "130"),
    ("170", "168", "169"),
    ("174", "172", "173"),
    ("178", "176", "177"),
)

_EXPECTED_NODE_TYPES = {
    "4": "CLIPLoader",
    "5": "VAELoader",
    "6": "VAELoader",
    "8": "UNETLoader",
    "9": "UNETLoader",
    "83": "Text",
    "84": "PrimitiveFloat",
    "92": "ComfyMathExpression",
    "97": "LoadImage",
    "99": "LayerUtility: ImageScaleByAspectRatio V2",
    "100": "PreviewImage",
    "101": "LoadImage",
    "102": "LayerUtility: ImageScaleByAspectRatio V2",
    "103": "PreviewImage",
    "105": "ResolutionSelector",
    "108": "MiniMaxH3ReferenceToVideo",
    "129": "LayerUtility: ImageScaleByAspectRatio V2",
    "130": "PreviewImage",
    "132": "LoadImage",
    "135": "VHS_LoadVideo",
    "138": "LoadAudio",
    "168": "LayerUtility: ImageScaleByAspectRatio V2",
    "169": "PreviewImage",
    "170": "LoadImage",
    "172": "LayerUtility: ImageScaleByAspectRatio V2",
    "173": "PreviewImage",
    "174": "LoadImage",
    "176": "LayerUtility: ImageScaleByAspectRatio V2",
    "177": "PreviewImage",
    "178": "LoadImage",
    "186": "LoraLoaderModelOnly",
    "187": "LoraLoaderModelOnly",
    "243": "RandomNoise",
    "246": "BasicGuider",
    "247": "SamplerCustomAdvanced",
    "248": "BasicScheduler",
    "249": "KSamplerSelect",
    "364": "VAEDecodeAudio",
    "365": "VAEDecode",
    "387": "VHS_VideoCombine",
    "391": "MiniMaxH3MemoryEfficientSageAttentionPatch",
    "392": "MiniMaxH3MemoryEfficientSageAttentionPatch",
    "395": "ModelAttentionBackend",
    "396": "ModelAttentionBackend",
}

_REMOTE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._/\-\u0080-\uffff]+$")
_ASPECT_RATIO_RE = re.compile(r"^\d{1,3}:\d{1,3}(?: \([^\r\n]{1,80}\))?$")


@dataclass(frozen=True)
class H3GraphBuildRequest:
    prompt: str
    reference_video: str
    reference_audio: str
    reference_images: tuple[str, ...] = ()
    continuity_anchor: str | None = None
    audio_duration_seconds: float = 0
    generation_tail_seconds: float = 0.1
    aspect_ratio: str = "9:16 (Portrait Widescreen)"
    megapixels: float = 1.0
    multiple: int = 32
    seed: int = 0


@dataclass(frozen=True)
class H3GraphBuildResult:
    workflow: dict[str, Any]
    workflow_json: str
    template_id: str
    template_version: str
    template_sha256: str
    dynamic_graph_sha256: str
    adapter_version: str
    duration: H3DurationPlan
    effective_image_count: int


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _workflow_references(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for nested in value.values():
            yield from _workflow_references(nested)
        return
    if isinstance(value, list):
        if (
            len(value) == 2
            and isinstance(value[0], str)
            and value[0].isdigit()
            and isinstance(value[1], int)
            and not isinstance(value[1], bool)
        ):
            yield value[0]
            return
        for nested in value:
            yield from _workflow_references(nested)


def _safe_remote_filename(value: object, field: str) -> str:
    filename = str(value or "").strip()
    if (
        not filename
        or len(filename) > 1000
        or ".." in filename.split("/")
        or "\\" in filename
        or filename.startswith("/")
        or "://" in filename
        or not _REMOTE_FILENAME_RE.fullmatch(filename)
    ):
        raise ValueError(f"{field}不是有效的 RunningHub 上传文件名")
    return filename


class H3DynamicGraphBuilder:
    """Build an audited H3 API-format graph without mutating its source template."""

    def __init__(self, template: dict[str, Any]) -> None:
        self._template = copy.deepcopy(template)
        self.template_sha256 = _canonical_sha256(self._template)
        self._validate_template(self._template, require_all_image_branches=True)
        if self.template_sha256 != H3_WORKFLOW_TEMPLATE_CANONICAL_SHA256:
            raise ValueError("H3 工作流模板摘要不匹配，必须先审核并升级适配器版本")

    @classmethod
    def from_path(cls, path: Path) -> "H3DynamicGraphBuilder":
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("H3 工作流模板无法读取或不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("H3 工作流模板顶层必须是节点对象")
        return cls(value)

    @staticmethod
    def _validate_node_types(workflow: dict[str, Any]) -> None:
        unknown_node_ids = set(workflow) - set(_EXPECTED_NODE_TYPES)
        if unknown_node_ids:
            raise ValueError(
                "H3 工作流包含未授权节点：" + ", ".join(sorted(unknown_node_ids))
            )
        for node_id, node in workflow.items():
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                raise ValueError(f"H3 工作流节点 {node_id} 结构不合法")
            expected_type = _EXPECTED_NODE_TYPES.get(node_id)
            if node.get("class_type") != expected_type:
                raise ValueError(f"H3 工作流节点 {node_id} 类型与审核模板不一致")

    @classmethod
    def _validate_template(
        cls,
        workflow: dict[str, Any],
        *,
        require_all_image_branches: bool,
    ) -> None:
        cls._validate_node_types(workflow)
        required_nodes = set(_EXPECTED_NODE_TYPES)
        if not require_all_image_branches:
            removable = {node_id for branch in _IMAGE_BRANCHES for node_id in branch}
            required_nodes -= removable
        missing_nodes = required_nodes - set(workflow)
        if missing_nodes:
            raise ValueError(
                "H3 工作流缺少审核节点：" + ", ".join(sorted(missing_nodes))
            )
        if workflow.get("92", {}).get("inputs", {}).get("expression") != H3_DURATION_EXPRESSION:
            raise ValueError("H3 节点 92 帧数公式与审核模板不一致")
        if workflow.get("248", {}).get("inputs", {}).get("steps") != H3_SAMPLING_STEPS:
            raise ValueError("H3 节点 248 采样步数与审核模板不一致")
        if workflow.get(H3_OUTPUT_NODE_ID, {}).get("class_type") != "VHS_VideoCombine":
            raise ValueError("H3 最终输出节点 387 不合法")
        output_inputs = workflow[H3_OUTPUT_NODE_ID]["inputs"]
        if output_inputs.get("images") != ["365", 0]:
            raise ValueError("H3 节点 387 必须读取节点 365 的模型解码视频")
        if output_inputs.get("audio") != ["364", 0]:
            raise ValueError("H3 节点 387 必须读取节点 364 的模型解码音频")
        if output_inputs.get("trim_to_audio") is not False:
            raise ValueError("H3 节点 387 不得按输入音频时长裁切模型成片")
        cls._validate_references(workflow)
        cls._validate_output_reachable(workflow)

    @staticmethod
    def _validate_references(workflow: dict[str, Any]) -> None:
        node_ids = set(workflow)
        for node_id, node in workflow.items():
            for referenced_id in _workflow_references(node.get("inputs")):
                if referenced_id not in node_ids:
                    raise ValueError(
                        f"H3 工作流节点 {node_id} 引用了不存在的节点 {referenced_id}"
                    )

    @staticmethod
    def _validate_output_reachable(workflow: dict[str, Any]) -> None:
        if H3_OUTPUT_NODE_ID not in workflow:
            raise ValueError("H3 工作流缺少最终输出节点 387")
        visited: set[str] = set()
        pending = [H3_OUTPUT_NODE_ID]
        while pending:
            node_id = pending.pop()
            if node_id in visited:
                continue
            visited.add(node_id)
            node = workflow.get(node_id)
            if not isinstance(node, dict):
                raise ValueError(f"H3 输出链引用了不存在的节点 {node_id}")
            pending.extend(_workflow_references(node.get("inputs")))
        for required_id in {
            "83", "84", "92", "105", "108", "135", "138", "243", "247", "364", "365"
        }:
            if required_id not in visited:
                raise ValueError(f"H3 最终输出链无法到达必要节点 {required_id}")

    @staticmethod
    def _validated_request(request: H3GraphBuildRequest) -> tuple[
        str,
        str,
        str,
        tuple[str, ...],
        str | None,
        str,
        float,
        int,
        int,
        H3DurationPlan,
    ]:
        prompt = str(request.prompt or "").strip()
        if not prompt or len(prompt) > 30000:
            raise ValueError("H3 Prompt 长度不合法")
        video = _safe_remote_filename(request.reference_video, "H3 参考视频")
        audio = _safe_remote_filename(request.reference_audio, "H3 参考音频")
        images = tuple(
            _safe_remote_filename(value, f"H3 第 {index + 1} 张参考图")
            for index, value in enumerate(request.reference_images)
        )
        if len(images) > 5:
            raise ValueError("H3 人物参考图最多为 5 张")
        continuity_anchor = (
            _safe_remote_filename(request.continuity_anchor, "H3 连续性尾帧")
            if request.continuity_anchor
            else None
        )
        effective_images = images + ((continuity_anchor,) if continuity_anchor else ())
        if len(set(effective_images)) != len(effective_images):
            raise ValueError("H3 参考图不能使用重复文件凑数")
        aspect_ratio = str(request.aspect_ratio or "").strip()
        if not _ASPECT_RATIO_RE.fullmatch(aspect_ratio):
            raise ValueError("H3 画幅参数不合法")
        try:
            megapixels = float(request.megapixels)
        except (TypeError, ValueError) as exc:
            raise ValueError("H3 megapixels 参数不合法") from exc
        if not math.isfinite(megapixels) or not 0.2 <= megapixels <= 2.0:
            raise ValueError("H3 megapixels 必须在 0.2 到 2.0 之间")
        if isinstance(request.multiple, bool) or int(request.multiple) != 32:
            raise ValueError("H3 第一版分辨率 multiple 必须固定为 32")
        if isinstance(request.seed, bool):
            raise ValueError("H3 Seed 不合法")
        try:
            seed = int(request.seed)
        except (TypeError, ValueError) as exc:
            raise ValueError("H3 Seed 不合法") from exc
        if not 0 <= seed < 2**64:
            raise ValueError("H3 Seed 不合法")
        duration = plan_h3_duration(
            request.audio_duration_seconds,
            request.generation_tail_seconds,
        )
        return (
            prompt,
            video,
            audio,
            images,
            continuity_anchor,
            aspect_ratio,
            megapixels,
            32,
            seed,
            duration,
        )

    def build(self, request: H3GraphBuildRequest) -> H3GraphBuildResult:
        (
            prompt,
            video,
            audio,
            images,
            continuity_anchor,
            aspect_ratio,
            megapixels,
            multiple,
            seed,
            duration,
        ) = self._validated_request(request)

        workflow = copy.deepcopy(self._template)
        workflow["83"]["inputs"]["text"] = prompt
        workflow["84"]["inputs"]["value"] = duration.requested_generation_duration_seconds
        workflow["105"]["inputs"].update(
            {
                "aspect_ratio": aspect_ratio,
                "megapixels": megapixels,
                "multiple": multiple,
            }
        )
        workflow["135"]["inputs"]["video"] = video
        workflow["138"]["inputs"]["audio"] = audio
        workflow["138"]["inputs"]["audioUI"] = ""
        workflow["243"]["inputs"]["noise_seed"] = seed
        # The downloaded official API graph previews the video but does not save
        # it. RunningHub's advanced API only exposes files from saved outputs.
        workflow[H3_OUTPUT_NODE_ID]["inputs"]["save_output"] = True

        slot_images = {index: value for index, value in enumerate(images)}
        if continuity_anchor:
            slot_images[5] = continuity_anchor

        ref_inputs = workflow["108"]["inputs"]
        for index, (load_id, scale_id, preview_id) in enumerate(_IMAGE_BRANCHES):
            ref_key = f"ref_images.ref_image_{index}"
            if index in slot_images:
                workflow[load_id]["inputs"]["image"] = slot_images[index]
                ref_inputs[ref_key] = [scale_id, 0]
            else:
                ref_inputs.pop(ref_key, None)
                workflow.pop(load_id, None)
                workflow.pop(scale_id, None)
                workflow.pop(preview_id, None)

        self._validate_template(workflow, require_all_image_branches=False)
        expected_ref_keys = sorted(
            f"ref_images.ref_image_{index}" for index in slot_images
        )
        actual_ref_keys = sorted(
            key for key in workflow["108"]["inputs"] if key.startswith("ref_images.")
        )
        if actual_ref_keys != expected_ref_keys:
            raise ValueError("H3 参考图连接与人物图及固定尾帧槽不一致")

        workflow_json = _canonical_json(workflow)
        dynamic_sha256 = hashlib.sha256(workflow_json.encode("utf-8")).hexdigest()
        return H3GraphBuildResult(
            workflow=workflow,
            workflow_json=workflow_json,
            template_id=H3_WORKFLOW_TEMPLATE_ID,
            template_version=H3_WORKFLOW_TEMPLATE_VERSION,
            template_sha256=self.template_sha256,
            dynamic_graph_sha256=dynamic_sha256,
            adapter_version=H3_ADAPTER_VERSION,
            duration=duration,
            effective_image_count=len(slot_images),
        )


def load_default_h3_graph_builder() -> H3DynamicGraphBuilder:
    return H3DynamicGraphBuilder.from_path(H3_DEFAULT_TEMPLATE_PATH)

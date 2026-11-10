from __future__ import annotations

from copy import deepcopy
import hashlib
from itertools import product
from pathlib import Path
from typing import Any, Iterable
import uuid

from .bgm_loudness import BGM_FALLBACK_VOLUME, automatic_bgm_mix
from .project_results import ProjectResultLibrary
from .project_store import ProjectStore
from .project_video_source import build_project_speech_audio, build_project_video_source
from .project_postprocess import (
    BGM_CROSSFADE_US,
    BGM_FADE_IN_US,
    CAPTION_REFERENCE_FONT_SIZE,
    CAPTION_STROKE_COLOR,
    CAPTION_STROKE_WIDTH,
    CAPTION_TRANSFORM_Y,
    VIDEO_FADE_OUT_US,
    build_project_cover,
    build_top_title_texts,
)
from .project_export_naming import (
    available_draft_name,
    variant_draft_name,
    variant_export_filename,
)
from .semantic_visuals import fixed_nameplate_overlay, frozen_visual_overlays
from .layout_profiles import (
    DEFAULT_LAYOUT_PROFILE,
    apply_layout_to_visual_overlays,
    layout_font,
    layout_profile,
    nameplate_texts,
    normalize_layout_profile,
)


VARIANT_OPERATION_TYPES = {
    "VARIANT_GENERATE",
    "VARIANT_SUPPLEMENT",
    "VARIANT_RETRY",
}
MAX_VARIANTS_PER_SUBMISSION = 500
DEFAULT_VARIANT_SETTINGS = {
    "mode": "recommended",
    "use_effects": True,
    "use_fullscreen_stickers": True,
    "use_visual": True,
    "mirror_interval_seconds": 10.0,
    "crop_ratios": ["1:1", "3:4"],
    "background_colors": ["#000000", "#FFFFFF", "#DBE7F5", "#F2DDDD"],
    "face_centered": True,
    "use_corner_stickers": True,
}
_DISTANCE_WEIGHTS = (5, 2, 4, 4, 3)


def _stable_rank(seed: str, signature: tuple[str, ...]) -> str:
    return hashlib.sha256(f"{seed}|{'|'.join(signature)}".encode("utf-8")).hexdigest()


def signature_distance(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    """Weighted Hamming distance; layout and major decoration changes dominate."""

    return sum(
        weight
        for weight, left_value, right_value in zip(_DISTANCE_WEIGHTS, left, right)
        if left_value != right_value
    )


def select_maximum_difference(
    candidates: Iterable[tuple[str, ...]],
    count: int,
    *,
    seed: str,
    existing: Iterable[tuple[str, ...]] = (),
) -> list[tuple[str, ...]]:
    """Deterministic maximin selection used for initial and supplemental variants."""

    unique = sorted(set(candidates))
    existing_set = set(existing)
    pool = [candidate for candidate in unique if candidate not in existing_set]
    if count < 1:
        raise ValueError("变体数量必须大于 0")
    if count > len(pool):
        raise ValueError(f"当前设置最多还能生成 {len(pool)} 个不重复变体")

    selected: list[tuple[str, ...]] = []
    reference = list(existing_set)
    usage: list[dict[str, int]] = [dict() for _ in range(5)]
    for row in reference:
        for axis, value in enumerate(row):
            usage[axis][value] = usage[axis].get(value, 0) + 1

    while len(selected) < count:

        def score(candidate: tuple[str, ...]) -> tuple[int, int, str]:
            distances = [signature_distance(candidate, other) for other in reference]
            minimum = min(distances) if distances else sum(_DISTANCE_WEIGHTS)
            reuse = sum(
                usage[axis].get(value, 0) for axis, value in enumerate(candidate)
            )
            return minimum, -reuse, _stable_rank(seed, candidate)

        chosen = max(pool, key=score)
        pool.remove(chosen)
        selected.append(chosen)
        reference.append(chosen)
        for axis, value in enumerate(chosen):
            usage[axis][value] = usage[axis].get(value, 0) + 1
    return selected


def _enabled_assets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("path")
        and not item.get("error")
        and item.get("enabled", True)
        and not item.get("deleted", False)
    ]


def _identity(item: dict[str, Any]) -> str:
    return str(
        item.get("identity") or item.get("resource_id") or item.get("path") or ""
    )


def _clean_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    result = deepcopy(DEFAULT_VARIANT_SETTINGS)
    aliases = {
        "useEffects": "use_effects",
        "useStickers": "use_fullscreen_stickers",
        "useVisual": "use_visual",
        "mirrorInterval": "mirror_interval_seconds",
        "ratios": "crop_ratios",
        "colors": "background_colors",
        "faceCentered": "face_centered",
        "cornerStickers": "use_corner_stickers",
    }
    for key, value in source.items():
        result[aliases.get(key, key)] = value
    result["mode"] = str(result.get("mode") or "recommended").lower()
    if result["mode"] == "default":
        result["mode"] = "recommended"
    result["mirror_interval_seconds"] = float(result["mirror_interval_seconds"])
    if not 0 < result["mirror_interval_seconds"] <= 3600:
        raise ValueError("镜像间隔必须大于 0 秒且不超过 3600 秒")
    ratios = [
        str(value).strip()
        for value in result.get("crop_ratios", [])
        if str(value).strip()
    ]
    colors = [
        str(value).strip().upper()
        for value in result.get("background_colors", [])
        if str(value).strip()
    ]
    if result.get("use_visual") and (not ratios or not colors):
        raise ValueError("画面变化至少需要一个裁剪比例和一个背景颜色")
    result["crop_ratios"] = list(dict.fromkeys(ratios))
    result["background_colors"] = list(dict.fromkeys(colors))
    return result


class ProjectVariantCoordinator:
    """Freeze the module-4B recipe and submit diverse Jianying variants once."""

    def __init__(
        self,
        store: ProjectStore,
        render_queue: Any,
        *,
        storage_root: Path,
        draft_root: Path,
        fonts: list[dict[str, Any]],
        bgm_assets: list[dict[str, Any]],
        effects: list[dict[str, Any]],
        fullscreen_stickers: list[dict[str, Any]],
        corner_stickers: list[dict[str, Any]],
        result_library_root: Path | None = None,
        semantic_visual_library_root: Path | None = None,
    ) -> None:
        self.store = store
        self.render_queue = render_queue
        self.storage_root = Path(storage_root).resolve()
        self.draft_root = Path(draft_root).resolve()
        self.fonts = {_identity(item): item for item in fonts if _identity(item)}
        self.bgm_assets = {
            _identity(item): item for item in bgm_assets if _identity(item)
        }
        self.effects = _enabled_assets(effects)
        self.fullscreen_stickers = _enabled_assets(fullscreen_stickers)
        self.corner_stickers = _enabled_assets(corner_stickers)
        self.semantic_visual_library_root = Path(
            semantic_visual_library_root
            or (
                Path(__file__).resolve().parents[2]
                / "data"
                / "libraries"
                / "semantic_visual_library"
            )
        ).resolve()
        self.result_library = ProjectResultLibrary(
            store, result_library_root or (self.storage_root / "result_library")
        )

    def options(self) -> dict[str, Any]:
        return {
            "schema": "jyd.project-variant-options.v1",
            "defaults": deepcopy(DEFAULT_VARIANT_SETTINGS),
            "frozen_dimensions": ["subtitle_font", "background_music"],
            "cover": {
                "mode": "project_postprocess",
                "frame_count": 3,
                "source": "input_image",
            },
            "result_library": {
                "root": str(self.result_library.root),
                "layout": "月.日/当日批次号",
                "example": str(self.result_library.root / "8.5" / "1"),
            },
            "effects": [
                {
                    "identity": _identity(item),
                    "name": item.get("name") or item.get("label"),
                }
                for item in self.effects
            ],
            "fullscreen_stickers": [
                {"identity": _identity(item), "name": item.get("name")}
                for item in self.fullscreen_stickers
            ],
            "corner_stickers": [
                {"identity": _identity(item), "name": item.get("name")}
                for item in self.corner_stickers
            ],
        }

    def start(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        idempotency_key: str,
        settings: dict[str, Any] | None,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        clean_key = str(idempotency_key or "").strip()
        if not clean_key:
            raise ValueError("变体生成请求缺少幂等键")
        project = self.store.get_project(owner_user_id, project_id)
        repeated = [
            operation
            for operation in project.get("operations", [])
            if operation.get("operation_type") == "VARIANT_GENERATE"
            and operation.get("idempotency_key") == clean_key
        ]
        if repeated:
            return self.sync(owner_user_id, project_id)
        supplied = {
            str(item.get("item_id") or ""): item
            for item in items
            if isinstance(item, dict) and str(item.get("item_id") or "").strip()
        }
        if not supplied or len(supplied) != len(items):
            raise ValueError("变体生成必须指定非空且不重复的脚本行")
        project_items = {str(item["item_id"]): item for item in project["items"]}
        if not set(supplied).issubset(project_items):
            raise KeyError("项目脚本行不存在")
        requests = [
            {
                "item_id": item["item_id"],
                "count": int(supplied[str(item["item_id"])].get("count") or 0),
            }
            for item in project["items"]
            if str(item["item_id"]) in supplied
        ]
        project = self.store.configure_variant_settings(
            owner_user_id,
            project_id,
            settings=_clean_settings(settings),
            items=requests,
        )
        return self._submit(
            owner_user_id,
            project_id,
            project,
            requests,
            settings=_clean_settings(settings),
            operation_type="VARIANT_GENERATE",
            idempotency_key=clean_key,
        )

    def supplement(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        idempotency_key: str,
        count: int,
        settings: dict[str, Any] | None = None,
        cover: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        item = self._item(project, item_id)
        previous = self._latest_variant_payload(project, item_id)
        resolved_settings = _clean_settings(settings or previous.get("settings"))
        del cover
        return self._submit(
            owner_user_id,
            project_id,
            project,
            [
                {
                    "item_id": item["item_id"],
                    "count": int(count),
                }
            ],
            settings=resolved_settings,
            operation_type="VARIANT_SUPPLEMENT",
            idempotency_key=str(idempotency_key or "").strip(),
        )

    def retry(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        self._item(project, item_id)
        failed_operation = next(
            (
                operation
                for operation in reversed(project.get("operations", []))
                if operation.get("item_id") == item_id
                and operation.get("operation_type") in VARIANT_OPERATION_TYPES
                and any(
                    job.get("status") == "failed"
                    for job in operation.get("result", {}).get("jobs", [])
                )
            ),
            None,
        )
        if failed_operation is None:
            raise ValueError("当前脚本行没有失败的变体可重试")
        payload = failed_operation.get("payload", {})
        jobs = payload.get("jobs", [])
        failed_indices = {
            int(job["index"])
            for job in failed_operation.get("result", {}).get("jobs", [])
            if job.get("status") == "failed"
        }
        retry_jobs = [
            deepcopy(job) for job in jobs if int(job.get("index", -1)) in failed_indices
        ]
        if not retry_jobs:
            raise ValueError("失败变体缺少冻结任务数据")
        return self._submit_prebuilt(
            owner_user_id,
            project_id,
            item_id,
            retry_jobs,
            operation_type="VARIANT_RETRY",
            idempotency_key=str(idempotency_key or "").strip(),
            settings=payload.get("settings", {}),
        )

    def _submit(
        self,
        owner_user_id: str,
        project_id: str,
        project: dict[str, Any],
        requests: list[dict[str, Any]],
        *,
        settings: dict[str, Any],
        operation_type: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise ValueError("变体任务缺少幂等键")
        if any(
            operation.get("operation_type") == operation_type
            and operation.get("idempotency_key") == idempotency_key
            for operation in project.get("operations", [])
        ):
            return self.sync(owner_user_id, project_id)
        if any(
            operation.get("operation_type") in VARIANT_OPERATION_TYPES
            and operation.get("status") in {"PENDING", "RUNNING"}
            for operation in project.get("operations", [])
        ):
            raise ValueError("当前已有变体任务正在处理")
        total = sum(int(request["count"]) for request in requests)
        if total < 1 or total > MAX_VARIANTS_PER_SUBMISSION:
            raise ValueError(
                f"单次提交的变体总数必须在 1 到 {MAX_VARIANTS_PER_SUBMISSION} 之间"
            )

        prepared: list[tuple[str, list[dict[str, Any]]]] = []
        for request in requests:
            item = self._item(project, str(request["item_id"]))
            count = int(request["count"])
            if not 1 <= count <= MAX_VARIANTS_PER_SUBMISSION:
                raise ValueError("每行变体数量必须在 1 到 500 之间")
            existing = [
                tuple(
                    str(value)
                    for value in asset.get("metadata", {}).get("signature", [])
                )
                for asset in item.get("outputs", {}).get("variants", [])
                if len(asset.get("metadata", {}).get("signature", [])) == 5
            ]
            rows = self._build_jobs(
                owner_user_id, project_id, item, count, settings, existing
            )
            prepared.append((str(item["item_id"]), rows))

        archive = self.result_library.prepare_batch(
            owner_user_id,
            project_id,
            operation_type=operation_type,
        )
        output_directory = Path(str(archive["export_path"]))
        serial = 0
        for item_id, rows in prepared:
            item = self._item(project, item_id)
            for row in rows:
                serial += 1
                variant_output = self._variant_output(
                    output_directory,
                    item=item,
                    index=serial,
                )
                row["job"]["output"]["mp4_path"] = str(variant_output)
                row["job"]["output"]["draft_name"] = available_draft_name(
                    self.draft_root,
                    variant_draft_name(item, index=serial),
                )

        operations: dict[str, dict[str, Any]] = {}
        flat_jobs: list[dict[str, Any]] = []
        flat_variants: list[dict[str, Any]] = []
        for item_id, rows in prepared:
            operation = self.store.create_operation(
                owner_user_id=owner_user_id,
                project_id=project_id,
                item_id=item_id,
                operation_type=operation_type,
                idempotency_key=idempotency_key,
                payload={
                    "settings": settings,
                    "jobs": rows,
                    "archive": archive,
                },
            )
            operations[item_id] = operation
            for row in rows:
                row["job"]["observability"] = {
                    "project_id": project_id,
                    "item_id": item_id,
                    "operation_id": operation["operation_id"],
                    "correlation_id": operation["correlation_id"],
                }
                flat_jobs.append(row["job"])
                flat_variants.append(
                    {
                        "project_id": project_id,
                        "item_id": item_id,
                        "kind": "module_6_variant",
                        "signature": row["signature"],
                        "dimensions": row["dimensions"],
                    }
                )
        try:
            submitted = self.render_queue.submit_batch(flat_jobs, flat_variants)
            batch_id = str(submitted.get("batch_id") or "")
            job_ids = [str(value) for value in submitted.get("job_ids", [])]
            if not batch_id or len(job_ids) != len(flat_jobs):
                raise ValueError("剪映任务队列返回了无效的变体批次")
            self.store.update_result_batch(
                owner_user_id,
                str(archive["result_batch_id"]),
                status="RUNNING",
                jianying_batch_id=batch_id,
            )
            cursor = 0
            for item_id, rows in prepared:
                result_jobs = []
                for index, row in enumerate(rows):
                    job_id = job_ids[cursor]
                    cursor += 1
                    result_jobs.append(
                        {
                            "index": index,
                            "job_id": job_id,
                            "status": "pending",
                            "signature": row["signature"],
                            "dimensions": row["dimensions"],
                            "output_path": row["job"]["output"]["mp4_path"],
                        }
                    )
                    self.store.add_link(
                        owner_user_id=owner_user_id,
                        project_id=project_id,
                        item_id=item_id,
                        system="jianying",
                        relation="variant_render_job",
                        external_id=job_id,
                        metadata={
                            "batch_id": batch_id,
                            "operation_id": operations[item_id]["operation_id"],
                        },
                    )
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item_id,
                    operation_type=operation_type,
                    status="RUNNING",
                    item_status="VARIANT_RUNNING",
                    result={
                        "batch_id": batch_id,
                        "result_batch_id": archive["result_batch_id"],
                        "archive": archive,
                        "jobs": result_jobs,
                    },
                )
        except Exception as exc:
            self.store.update_result_batch(
                owner_user_id,
                str(archive["result_batch_id"]),
                status="FAILED",
                error_message=str(exc),
            )
            for item_id in operations:
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item_id,
                    operation_type=operation_type,
                    status="FAILED",
                    item_status="VARIANT_FAILED",
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )
            raise
        return self.sync(owner_user_id, project_id)

    def _submit_prebuilt(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        rows: list[dict[str, Any]],
        *,
        operation_type: str,
        idempotency_key: str,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise ValueError("变体重试请求缺少幂等键")
        project = self.store.get_project(owner_user_id, project_id)
        if any(
            operation.get("operation_type") == operation_type
            and operation.get("item_id") == item_id
            and operation.get("idempotency_key") == idempotency_key
            for operation in project.get("operations", [])
        ):
            return self.sync(owner_user_id, project_id)
        archive = self.result_library.prepare_batch(
            owner_user_id,
            project_id,
            operation_type=operation_type,
        )
        output_directory = Path(str(archive["export_path"]))
        item = self._item(project, item_id)
        for output_index, row in enumerate(rows, start=1):
            variant_output = self._variant_output(
                output_directory,
                item=item,
                index=output_index,
            )
            row["job"]["output"]["mp4_path"] = str(variant_output)
            row["job"]["output"]["draft_name"] = available_draft_name(
                self.draft_root,
                variant_draft_name(item, index=output_index),
            )
        operation = self.store.create_operation(
            owner_user_id=owner_user_id,
            project_id=project_id,
            item_id=item_id,
            operation_type=operation_type,
            idempotency_key=idempotency_key,
            payload={
                "settings": settings,
                "jobs": rows,
                "archive": archive,
            },
        )
        for row in rows:
            row["job"]["observability"] = {
                "project_id": project_id,
                "item_id": item_id,
                "operation_id": operation["operation_id"],
                "correlation_id": operation["correlation_id"],
            }
        try:
            submitted = self.render_queue.submit_batch(
                [row["job"] for row in rows],
                [
                    {
                        "project_id": project_id,
                        "item_id": item_id,
                        "kind": "module_6_variant_retry",
                        "signature": row["signature"],
                    }
                    for row in rows
                ],
            )
            job_ids = [str(value) for value in submitted.get("job_ids", [])]
            batch_id = str(submitted.get("batch_id") or "")
            if not batch_id or len(job_ids) != len(rows):
                raise ValueError("剪映任务队列返回了无效的重试批次")
            self.store.update_result_batch(
                owner_user_id,
                str(archive["result_batch_id"]),
                status="RUNNING",
                jianying_batch_id=batch_id,
            )
            result_jobs = []
            for row, job_id in zip(rows, job_ids):
                result_jobs.append(
                    {
                        "index": int(row.get("index", 0)),
                        "job_id": job_id,
                        "status": "pending",
                        "signature": row["signature"],
                        "dimensions": row["dimensions"],
                        "output_path": row["job"]["output"]["mp4_path"],
                    }
                )
                self.store.add_link(
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    item_id=item_id,
                    system="jianying",
                    relation="variant_render_job",
                    external_id=job_id,
                    metadata={
                        "batch_id": batch_id,
                        "operation_id": operation["operation_id"],
                    },
                )
            self.store.transition_operation(
                owner_user_id,
                project_id,
                item_id,
                operation_type=operation_type,
                status="RUNNING",
                item_status="VARIANT_RUNNING",
                result={
                    "batch_id": batch_id,
                    "result_batch_id": archive["result_batch_id"],
                    "archive": archive,
                    "jobs": result_jobs,
                },
            )
        except Exception as exc:
            self.store.update_result_batch(
                owner_user_id,
                str(archive["result_batch_id"]),
                status="FAILED",
                error_message=str(exc),
            )
            self.store.transition_operation(
                owner_user_id,
                project_id,
                item_id,
                operation_type=operation_type,
                status="FAILED",
                item_status="VARIANT_FAILED",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            raise
        return self.sync(owner_user_id, project_id)

    def _build_jobs(
        self,
        owner_user_id: str,
        project_id: str,
        item: dict[str, Any],
        count: int,
        settings: dict[str, Any],
        existing: list[tuple[str, ...]],
    ) -> list[dict[str, Any]]:
        effects = self.effects if settings.get("use_effects") else [None]
        fullscreen = (
            self.fullscreen_stickers
            if settings.get("use_fullscreen_stickers")
            else [None]
        )
        if settings.get("use_fullscreen_stickers") and not fullscreen:
            raise ValueError("全屏贴纸已开启，但素材库中没有可用贴纸")
        layouts = (
            list(product(settings["crop_ratios"], settings["background_colors"]))
            if settings.get("use_visual")
            else [("none", "none")]
        )
        corner_sets: list[int | None] = (
            list(range(len(self.corner_stickers)))
            if settings.get("use_visual")
            and settings.get("use_corner_stickers")
            and self.corner_stickers
            else [None]
        )
        changing_axes = sum(
            len(values) > 1 for values in (effects, fullscreen, layouts, corner_sets)
        )
        if settings.get("mode") == "custom" and changing_axes < 2:
            raise ValueError("自定义变体至少需要两个真正可变化的核心维度")

        candidates: list[tuple[str, ...]] = []
        lookup: dict[tuple[str, ...], tuple[Any, Any, tuple[str, str], int | None]] = {}
        for effect, sticker, layout, corner_index in product(
            effects, fullscreen, layouts, corner_sets
        ):
            signature = (
                str(layout[0]),
                str(layout[1]),
                _identity(effect) if effect else "none",
                _identity(sticker) if sticker else "none",
                str(corner_index) if corner_index is not None else "none",
            )
            candidates.append(signature)
            lookup[signature] = (effect, sticker, layout, corner_index)
        selected = select_maximum_difference(
            candidates, count, seed=f"{project_id}:{item['item_id']}", existing=existing
        )
        frozen = self._frozen_recipe(item)
        rows: list[dict[str, Any]] = []
        for index, signature in enumerate(selected):
            effect, sticker, layout, corner_index = lookup[signature]
            job = deepcopy(frozen["job"])
            job["output"] = {
                "draft_root": str(self.draft_root),
                "mp4_path": str(self.storage_root / "pending" / f"{uuid.uuid4().hex}.mp4"),
                "skip_export": False,
            }
            if effect:
                job["effects"] = [
                    {
                        "effect_json_path": str(effect["path"]),
                        "start_us": 0,
                        "duration_us": 0,
                    }
                ]
            stickers: list[dict[str, Any]] = []
            if sticker:
                stickers.append(
                    {
                        "sticker_json_path": str(sticker["path"]),
                        "start_us": 0,
                        "duration_us": 0,
                    }
                )
            corner_names: list[str] = []
            if corner_index is not None:
                corners = ["top_left", "top_right", "bottom_left", "bottom_right"]
                for offset, corner in enumerate(corners):
                    asset = self.corner_stickers[
                        (corner_index + offset) % len(self.corner_stickers)
                    ]
                    corner_names.append(str(asset.get("name") or _identity(asset)))
                    stickers.append(
                        {
                            "sticker_json_path": str(asset["path"]),
                            "corner": corner,
                            "visible_ratio": 0.05,
                            "start_us": 0,
                            "duration_us": 0,
                        }
                    )
            if stickers:
                job["stickers"] = stickers
            if settings.get("use_visual"):
                job["visual_variant"] = {
                    "enabled": True,
                    "mirror_interval_seconds": settings["mirror_interval_seconds"],
                    "crop_ratio": layout[0],
                    "background_color": layout[1],
                    "face_centered": bool(settings.get("face_centered", True)),
                    "face_sample_count": 3,
                }
            dimensions = {
                "crop_ratio": layout[0],
                "background_color": layout[1],
                "effect": (effect or {}).get("name")
                or (effect or {}).get("label")
                or "无",
                "fullscreen_sticker": (sticker or {}).get("name") or "无",
                "corner_stickers": corner_names,
                "subtitle_font": frozen["font_identity"],
                "background_music": frozen["bgm_identity"],
            }
            rows.append(
                {
                    "index": index,
                    "signature": list(signature),
                    "dimensions": dimensions,
                    "source_video_asset_id": frozen["source_video_asset_id"],
                    "job": job,
                }
            )
        return rows

    def _frozen_recipe(self, item: dict[str, Any]) -> dict[str, Any]:
        outputs = item.get("outputs", {})
        current = outputs.get("composition_video")
        if isinstance(current, dict) and current.get("source_type") == "user_upload":
            source = current
            return {
                "job": {
                    "schema": "jyd.render_job.v1",
                    "source": {
                        "type": "video",
                        "media_path": str(Path(str(source["managed_path"])).resolve()),
                    },
                    "export": {"resolution": "1080P", "framerate": "30fps"},
                },
                "font_identity": None,
                "bgm_identity": None,
                "source_video_asset_id": source.get("asset_id"),
            }
        source = outputs.get("base_video")
        if not isinstance(source, dict) or not source.get("managed_path"):
            raise ValueError(f"任务 {item['row_key']} 缺少可复用的基础视频")
        subtitles = item.get("subtitles", {})
        cues = subtitles.get("render_cues", [])
        style = subtitles.get("style", {})
        postprocess = item.get("settings", {}).get("postprocess", {})
        profile_id = normalize_layout_profile(
            postprocess.get("layout_profile") or DEFAULT_LAYOUT_PROFILE
        )
        profile = layout_profile(profile_id)
        caption_profile = profile["caption"]
        font_identity = str(
            style.get("font_id") or postprocess.get("font_identity") or ""
        )
        font = layout_font(self.fonts, font_identity)
        font_identity = str(font.get("identity") or font_identity)
        if cues and font is None:
            raise ValueError(f"任务 {item['row_key']} 冻结的字幕字体不可用")
        bgm_identity = str(postprocess.get("bgm_identity") or "")
        if bgm_identity and bgm_identity not in self.bgm_assets:
            raise ValueError(f"任务 {item['row_key']} 冻结的 BGM 不可用")
        source = build_project_video_source(item)
        source["fade_out_us"] = VIDEO_FADE_OUT_US
        job: dict[str, Any] = {
            "schema": "jyd.render_job.v1",
            "source": source,
            "original_video_volume": 0.0,
            "export": {"resolution": "1080P", "framerate": "30fps"},
            "audios": [build_project_speech_audio(item)],
        }
        if cues:
            job["captions"] = {
                "cues": cues,
                "track_name": "MiniMax 单行字幕",
                "size": float(caption_profile["font_size"]),
                "clip_scale": float(caption_profile["clip_scale"]),
                "color": "#FFFFFF",
                "stroke_color": "",
                "stroke_width": 0.0,
                "shadow_color": "#000000",
                "shadow_alpha": float(caption_profile["shadow_alpha"]),
                "shadow_distance": 5.0,
                "shadow_angle": -45.0,
                "shadow_smoothing": 0.45000001788139343,
                "transform_x": 0.0,
                "transform_y": float(caption_profile["transform_y"]),
                "line_max_width": float(caption_profile["max_width_ratio"]),
                "max_lines": 1,
                "single_line": True,
                "font_id": str(font.get("resource_id") or ""),
                "font_path": str(font["path"]),
                "font_title": str(font.get("name") or ""),
            }
        if bgm_identity:
            saved_bgm_volume = postprocess.get("bgm_volume")
            if saved_bgm_volume is None:
                audio = outputs.get("audio")
                bgm = self.bgm_assets.get(bgm_identity) or {}
                voice_path = str(audio.get("managed_path") or "") if isinstance(audio, dict) else ""
                bgm_path = str(bgm.get("absolute_path") or "")
                bgm_mix = (
                    automatic_bgm_mix(voice_path, bgm_path)
                    if voice_path and bgm_path
                    else {"volume": BGM_FALLBACK_VOLUME}
                )
                saved_bgm_volume = bgm_mix["volume"]
            job["audios"].append(
                {
                    "type": "bgm",
                    "library_identity": bgm_identity,
                    "target_start_us": 0,
                    "target_duration_us": 0,
                    "fit_to_video": True,
                    "align_to_end": True,
                    "crossfade_us": BGM_CROSSFADE_US,
                    "fade_in_us": BGM_FADE_IN_US,
                    "volume": float(saved_bgm_volume or BGM_FALLBACK_VOLUME),
                }
            )
        job["visual_overlays"] = apply_layout_to_visual_overlays(
            frozen_visual_overlays(item, library_root=self.semantic_visual_library_root),
            profile_id,
        )
        job["fixed_overlays"] = [
            fixed_nameplate_overlay(self.semantic_visual_library_root, profile_id)
        ]
        title_texts = [
            *build_top_title_texts(
                postprocess.get("top_title"), font=font, layout_profile_id=profile_id
            ),
            *nameplate_texts(profile_id, font=font),
        ]
        if title_texts:
            job["texts"] = title_texts
        cover = build_project_cover(item, fonts=self.fonts)
        if cover is not None:
            job["cover"] = cover
        return {
            "job": job,
            "font_identity": font_identity or None,
            "bgm_identity": bgm_identity or None,
            "source_video_asset_id": source.get("asset_id"),
        }

    def sync(self, owner_user_id: str, project_id: str) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        active = [
            operation
            for operation in project.get("operations", [])
            if operation.get("operation_type") in VARIANT_OPERATION_TYPES
            and operation.get("status") in {"PENDING", "RUNNING"}
        ]
        for operation in active:
            item_id = str(operation.get("item_id") or "")
            jobs = deepcopy(operation.get("result", {}).get("jobs", []))
            any_running = False
            any_failed = False
            for job in jobs:
                if job.get("status") == "completed":
                    continue
                try:
                    remote = self.render_queue.get_status(str(job.get("job_id") or ""))
                    status = str(remote.get("status") or "")
                except Exception as exc:
                    status = "failed"
                    remote = {"error": str(exc)}
                if status in {"pending", "running"}:
                    job["status"] = status
                    any_running = True
                    continue
                if status != "completed":
                    job["status"] = "failed"
                    job["error"] = str(remote.get("error") or "剪映变体任务失败")
                    any_failed = True
                    continue
                render_result = (
                    remote.get("result")
                    if isinstance(remote.get("result"), dict)
                    else {}
                )
                output = Path(
                    str(render_result.get("output_mp4") or job.get("output_path") or "")
                ).resolve()
                if not output.is_file():
                    job["status"] = "failed"
                    job["error"] = "剪映任务完成但变体文件不存在"
                    any_failed = True
                    continue
                current_project = self.store.get_project(owner_user_id, project_id)
                item = self._item(current_project, item_id)
                exists = any(
                    asset.get("external_ref", {}).get("render_job_id")
                    == job.get("job_id")
                    for asset in item.get("outputs", {}).get("variants", [])
                )
                if not exists:
                    archive = operation.get("payload", {}).get("archive", {})
                    self.store.add_asset(
                        owner_user_id=owner_user_id,
                        project_id=project_id,
                        item_id=item_id,
                        asset_type="variant_video",
                        source_type="jianying_variant",
                        status="READY",
                        filename=output.name,
                        managed_path=str(output),
                        external_ref={
                            "render_job_id": job.get("job_id"),
                            "batch_id": operation.get("result", {}).get("batch_id"),
                        },
                        metadata={
                            "signature": job.get("signature", []),
                            "dimensions": job.get("dimensions", {}),
                            "operation_id": operation.get("operation_id"),
                            "source_video_asset_id": next(
                                (
                                    row.get("source_video_asset_id")
                                    for row in operation.get("payload", {}).get("jobs", [])
                                    if int(row.get("index", -1)) == int(job.get("index", -2))
                                ),
                                None,
                            ),
                            "result_batch_id": archive.get("result_batch_id"),
                            "result_date": archive.get("date_label"),
                            "result_batch_no": archive.get("batch_no"),
                            "export_path": archive.get("export_path"),
                        },
                        make_current=True,
                    )
                job["status"] = "completed"
            if any_running:
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item_id,
                    operation_type=str(operation["operation_type"]),
                    status="RUNNING",
                    item_status="VARIANT_RUNNING",
                    result={**operation.get("result", {}), "jobs": jobs},
                )
            elif any_failed or any(job.get("status") == "failed" for job in jobs):
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item_id,
                    operation_type=str(operation["operation_type"]),
                    status="FAILED",
                    item_status="VARIANT_FAILED",
                    result={**operation.get("result", {}), "jobs": jobs},
                    error_code="PARTIAL_VARIANT_FAILURE",
                    error_message="部分变体生成失败，可仅重试失败项",
                )
            else:
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item_id,
                    operation_type=str(operation["operation_type"]),
                    status="SUCCEEDED",
                    item_status="VARIANT_READY",
                    result={**operation.get("result", {}), "jobs": jobs},
                )
        return self._sync_result_batches(owner_user_id, project_id)

    def _sync_result_batches(
        self, owner_user_id: str, project_id: str
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        operations = project.get("operations", [])
        for batch in project.get("result_batches", []):
            result_batch_id = str(batch.get("result_batch_id") or "")
            related = [
                operation
                for operation in operations
                if str(operation.get("payload", {}).get("archive", {}).get("result_batch_id") or "")
                == result_batch_id
            ]
            jobs = [
                job
                for operation in related
                for job in operation.get("result", {}).get("jobs", [])
            ]
            if not related:
                continue
            if any(operation.get("status") in {"PENDING", "RUNNING"} for operation in related):
                status = "RUNNING"
            else:
                completed = sum(job.get("status") == "completed" for job in jobs)
                failed = sum(job.get("status") == "failed" for job in jobs)
                status = (
                    "PARTIAL_FAILED"
                    if completed and failed
                    else ("FAILED" if failed else "SUCCEEDED")
                )
            self.store.update_result_batch(
                owner_user_id,
                result_batch_id,
                status=status,
                jianying_batch_id=next(
                    (
                        str(operation.get("result", {}).get("batch_id") or "")
                        for operation in related
                        if operation.get("result", {}).get("batch_id")
                    ),
                    None,
                ),
                error_message=next(
                    (
                        str(operation.get("error_message") or "")
                        for operation in related
                        if operation.get("error_message")
                    ),
                    "",
                ),
            )
        return self.store.get_project(owner_user_id, project_id)

    @staticmethod
    def _item(project: dict[str, Any], item_id: str) -> dict[str, Any]:
        item = next(
            (
                value
                for value in project.get("items", [])
                if str(value.get("item_id")) == str(item_id)
            ),
            None,
        )
        if item is None:
            raise KeyError("脚本行不存在")
        return item

    @staticmethod
    def _latest_variant_payload(
        project: dict[str, Any], item_id: str
    ) -> dict[str, Any]:
        operation = next(
            (
                value
                for value in reversed(project.get("operations", []))
                if value.get("item_id") == item_id
                and value.get("operation_type") in VARIANT_OPERATION_TYPES
            ),
            None,
        )
        return operation.get("payload", {}) if operation else {}

    def _variant_output(
        self, output_directory: Path, *, item: dict[str, Any], index: int
    ) -> Path:
        return output_directory / variant_export_filename(item, index=index)

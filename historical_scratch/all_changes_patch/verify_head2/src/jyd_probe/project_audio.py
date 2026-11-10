from __future__ import annotations

from collections import defaultdict
import hashlib
import logging
import math
from pathlib import Path
from typing import Any, Mapping
import uuid

from .auth_center import AuthCenterClient
from .project_export_naming import audio_export_filename
from .project_store import ProjectStore
from .logging_config import log_event
from .semantic_visuals import SemanticVisualCatalog
from .unified_visual_plan import refresh_saved_visual_item


REMOTE_AUDIO_ACTIVE = {
    "PENDING",
    "CLONING",
    "SYNTHESIZING",
    "REMOTE_PENDING",
    "ALIGNING",
    "SEGMENTING",
    "HANDOFF",
}
logger = logging.getLogger("jyd_probe.workbench")


def _audio_speed(project: dict[str, Any], item_id: str) -> float:
    """Return the speed snapshot of the latest paid audio command for a row."""

    for operation in reversed(project.get("operations", [])):
        if (
            operation.get("item_id") != item_id
            or operation.get("operation_type") != "AUDIO_GENERATE"
        ):
            continue
        payload = operation.get("payload")
        speech = payload.get("speech_settings") if isinstance(payload, dict) else None
        try:
            speed = float(speech.get("speed", 1.0))
        except (AttributeError, TypeError, ValueError):
            continue
        if math.isfinite(speed) and 0.5 <= speed <= 2.0:
            return speed
    return 1.0


def _current_audio_links(
    links: list[dict[str, Any]],
    *,
    active_item_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Select newest remote links, optionally limited to locally active items."""

    newest_by_item: dict[str, dict[str, Any]] = {}
    for link in links:
        if (
            link.get("system") == "runninghub"
            and link.get("relation") == "digital_human_audio_item"
            and link.get("item_id")
            and (
                active_item_ids is None
                or str(link["item_id"]) in active_item_ids
            )
        ):
            newest_by_item[str(link["item_id"])] = link
    current_items = {
        str(link["external_id"]): link for link in newest_by_item.values()
    }
    current_batch_ids = {
        str(link.get("metadata", {}).get("batch_id") or "")
        for link in newest_by_item.values()
    }
    batches = [
        link
        for link in links
        if link.get("system") == "runninghub"
        and link.get("relation") == "digital_human_audio_batch"
        and str(link.get("external_id") or "") in current_batch_ids
    ]
    return batches, current_items


def _speech_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    model = str(value.get("model") or "speech-2.8-hd").strip()
    speed = float(value.get("speed", 1.0))
    volume = float(value.get("volume", 1.0))
    pitch = int(value.get("pitch", 0))
    if model not in {"speech-2.8-hd", "speech-2.8-turbo"}:
        raise ValueError("语音模型只能使用 speech-2.8-hd 或 speech-2.8-turbo")
    if not 0.5 <= speed <= 2.0:
        raise ValueError("语速必须在 0.5–2.0 之间")
    if not 0 < volume <= 10:
        raise ValueError("音量必须大于 0 且不超过 10")
    if not -12 <= pitch <= 12:
        raise ValueError("音调必须在 -12–12 之间")
    return {
        "model": model,
        "speed": speed,
        "volume": volume,
        "pitch": pitch,
        "languageBoost": str(value.get("language_boost") or "Chinese"),
        "outputFormat": "mp3",
        "costConfirmed": True,
    }


def _digital_human_resolution(value: Any) -> str:
    resolution = str(value or "1024").strip()
    try:
        if int(resolution) <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("数字人最长边分辨率必须是正整数") from exc
    return resolution


class ProjectAudioCoordinator:
    """Orchestrate real MiniMax audio while keeping project state local."""

    def __init__(
        self,
        store: ProjectStore,
        client: AuthCenterClient,
        *,
        storage_root: Path,
        max_audio_bytes: int,
        visual_catalog: SemanticVisualCatalog | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.storage_root = Path(storage_root).resolve()
        self.max_audio_bytes = int(max_audio_bytes)
        self.visual_catalog = visual_catalog

    def start(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        *,
        default_voice_asset_id: str,
        voice_assignments: dict[str, str] | None,
        settings: dict[str, Any] | None,
        resolution: str = "1024",
        idempotency_key: str,
        item_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        if not idempotency_key.strip():
            raise ValueError("声音生成请求缺少幂等键")

        requested_ids: list[str] | None = None
        if item_ids is not None:
            requested_ids = [str(value or "").strip() for value in item_ids]
            if (
                not requested_ids
                or any(not value for value in requested_ids)
                or len(set(requested_ids)) != len(requested_ids)
            ):
                raise ValueError("单条声音生成必须指定非空且不重复的脚本行 ID")
            by_id = {str(item["item_id"]): item for item in project["items"]}
            missing = [value for value in requested_ids if value not in by_id]
            if missing:
                raise KeyError("项目脚本行不存在")
            selected_items = [by_id[value] for value in requested_ids]
            blocked = [
                item for item in selected_items
                if not item.get("allowed_actions", {}).get("generate_audio")
            ]
            if blocked:
                raise ValueError(f"任务 {blocked[0]['row_key']} 正在生成，暂时不能生成声音")
            # An explicit row request is a smart action: a still-current audio
            # asset is reused instead of creating another paid TTS version.
            selected_items = [
                item for item in selected_items
                if item.get("outputs", {}).get("audio") is None
            ]
            if not selected_items:
                return project
        else:
            if not project["allowed_actions"]["generate_audio"]:
                raise ValueError("当前项目状态不能开始声音生成")
            selected_items = project["items"]

        library = self.client.list_workbench_voices(token)
        available_ids = {
            str(voice.get("voice_asset_id") or "")
            for voice in library.get("voices", [])
            if isinstance(voice, dict)
        }
        default_voice = str(default_voice_asset_id or "").strip()
        if default_voice not in available_ids:
            raise ValueError("默认声音不属于当前数字人账号")
        assignments = voice_assignments if isinstance(voice_assignments, dict) else {}
        speech = _speech_settings(settings)
        digital_human_resolution = _digital_human_resolution(resolution)
        self.store.set_voice_preferences(
            owner_user_id,
            default_voice_asset_id=default_voice,
            voice_settings={
                "model": speech["model"],
                "speed": speech["speed"],
                "volume": speech["volume"],
                "pitch": speech["pitch"],
                "language_boost": speech["languageBoost"],
                "output_format": speech["outputFormat"],
            },
        )

        pending_items = [
            item
            for item in selected_items
            if item.get("status") in {"DRAFT", "AUDIO_FAILED"}
        ]
        target_items = (
            selected_items
            if requested_ids is not None
            else pending_items or [
                item
                for item in selected_items
                if item.get("status") not in {
                    "AUDIO_QUEUED",
                    "AUDIO_RUNNING",
                    "COMPOSITION_QUEUED",
                    "DIGITAL_HUMAN_RUNNING",
                    "VIDEO_ENHANCING",
                    "VIDEO_MERGING",
                    "POSTPROCESS_RUNNING",
                    "VARIANT_QUEUED",
                    "VARIANT_RUNNING",
                }
            ]
        )
        if not target_items:
            raise ValueError("当前项目没有可创建新声音版本的脚本行")

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        correlation_by_voice: dict[str, str] = {}
        resolved_items: list[tuple[dict[str, Any], str]] = []
        for item in target_items:
            voice_id = str(assignments.get(item["item_id"]) or default_voice).strip()
            if voice_id not in available_ids:
                raise ValueError(f"任务 {item['row_key']} 选择了不可用声音")
            resolved_items.append((item, voice_id))

        for item, voice_id in resolved_items:
            correlation_id = correlation_by_voice.setdefault(
                voice_id, uuid.uuid4().hex
            )
            script_hash = hashlib.sha256(
                str(item["script_text"]).encode("utf-8")
            ).hexdigest()
            self.store.prepare_item_audio_generation(
                owner_user_id, project_id, item["item_id"]
            )
            self.store.configure_item_voice(
                owner_user_id,
                project_id,
                item["item_id"],
                voice_asset_id=voice_id,
            )
            operation = self.store.create_operation(
                owner_user_id=owner_user_id,
                project_id=project_id,
                item_id=item["item_id"],
                operation_type="AUDIO_GENERATE",
                idempotency_key=idempotency_key,
                payload={
                    "voice_asset_id": voice_id,
                    "speech_settings": speech,
                    "resolution": digital_human_resolution,
                    "script_sha256": script_hash,
                    "script_length": len(str(item["script_text"])),
                },
                correlation_id=correlation_id,
            )
            grouped[voice_id].append({**item, "operation": operation})

        project = self.store.get_project(owner_user_id, project_id)
        for group_index, (voice_id, items) in enumerate(grouped.items(), start=1):
            correlation_id = str(items[0]["operation"]["correlation_id"])
            try:
                rows = [
                    {
                        "row_id": item["row_key"],
                        "speech_script": item["script_text"],
                    }
                    for item in items
                ]
                request_digest = hashlib.sha256(
                    f"{idempotency_key}\0{voice_id}".encode("utf-8")
                ).hexdigest()[:48]
                remote = self.client.create_workbench_audio_batch(
                    token,
                    {
                        "name": f"{project['project_no']} 声音批次 {group_index}",
                        # The digital backend stores this in a 64-character
                        # internal idempotency column.  It is not a RunningHub
                        # task id and does not couple TTS to video generation.
                        "request_key": f"workbench-audio-{request_digest}",
                        "correlation_id": correlation_id,
                        "resolution": digital_human_resolution,
                        "rows": rows,
                        "speech_options": {
                            **speech,
                            "voiceAssetId": voice_id,
                        },
                    },
                )
                batch_id = str(remote.get("batch_id") or "")
                if not batch_id:
                    raise ValueError("数字人后端没有返回声音批次编号")
                remote_correlation_id = str(
                    remote.get("correlation_id") or correlation_id
                )
                if remote_correlation_id != correlation_id:
                    raise ValueError("数字人后端返回了不一致的日志关联标识")
                log_event(
                    logger,
                    "workbench.cloud_audio_batch_accepted",
                    "云端已接收声音批次",
                    component="workbench",
                    user_id=owner_user_id,
                    project_id=project_id,
                    batch_id=batch_id,
                    correlation_id=correlation_id,
                    item_count=len(items),
                )
                self.store.add_link(
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    system="runninghub",
                    relation="digital_human_audio_batch",
                    external_id=batch_id,
                    metadata={
                        "voice_asset_id": voice_id,
                        "correlation_id": correlation_id,
                    },
                )
                remote_by_row = {
                    str(row.get("row_key") or ""): row
                    for row in remote.get("items", [])
                    if isinstance(row, dict)
                }
                for item in items:
                    remote_item = remote_by_row.get(str(item["row_key"]))
                    if not remote_item or not remote_item.get("item_id"):
                        raise ValueError(f"数字人后端缺少任务 {item['row_key']} 的声音记录")
                    remote_item_id = str(remote_item["item_id"])
                    self.store.add_link(
                        owner_user_id=owner_user_id,
                        project_id=project_id,
                        item_id=item["item_id"],
                        system="runninghub",
                        relation="digital_human_audio_item",
                        external_id=remote_item_id,
                        metadata={
                            "batch_id": batch_id,
                            "correlation_id": correlation_id,
                            "script_sha256": hashlib.sha256(
                                str(item["script_text"]).encode("utf-8")
                            ).hexdigest(),
                            "script_length": len(str(item["script_text"])),
                        },
                    )
                    self.store.transition_audio_operation(
                        owner_user_id,
                        project_id,
                        item["item_id"],
                        status="RUNNING",
                        item_status="AUDIO_QUEUED",
                        result={
                            "batch_id": batch_id,
                            "item_id": remote_item_id,
                            "provider_status": remote_item.get("status"),
                        },
                    )
            except Exception as exc:
                log_event(
                    logger,
                    "workbench.cloud_audio_batch_failed",
                    "云端声音批次创建失败",
                    level=logging.ERROR,
                    component="workbench",
                    user_id=owner_user_id,
                    project_id=project_id,
                    correlation_id=correlation_id,
                    error_type=type(exc).__name__,
                )
                for item in items:
                    self.store.transition_audio_operation(
                        owner_user_id,
                        project_id,
                        item["item_id"],
                        status="FAILED",
                        item_status="AUDIO_FAILED",
                        error_code=type(exc).__name__,
                        error_message=str(exc),
                    )
                raise
        return self.sync(owner_user_id, project_id, token)

    def sync(
        self, owner_user_id: str, project_id: str, token: str
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        active_item_ids = {
            str(item["item_id"])
            for item in project["items"]
            if item.get("status") in {"AUDIO_QUEUED", "AUDIO_RUNNING"}
        }
        caption_recovery_item_ids = {
            str(item["item_id"])
            for item in project["items"]
            if isinstance(item.get("outputs", {}).get("audio"), dict)
            and (
                item.get("subtitles", {}).get("bound_audio_asset_id")
                != item["outputs"]["audio"].get("asset_id")
                or not item.get("subtitles", {}).get("raw_cues")
            )
        }
        batch_links, item_links = _current_audio_links(
            project["links"],
            active_item_ids=active_item_ids | caption_recovery_item_ids,
        )
        local_by_item = {item["item_id"]: item for item in project["items"]}
        for batch_link in batch_links:
            remote = self.client.get_workbench_audio_batch(
                token, batch_link["external_id"]
            )
            batch_id = str(remote.get("batch_id") or batch_link["external_id"])
            for remote_item in remote.get("items", []):
                if not isinstance(remote_item, dict):
                    continue
                remote_item_id = str(remote_item.get("item_id") or "")
                link = item_links.get(remote_item_id)
                if link is None or not link.get("item_id"):
                    continue
                local_item_id = str(link["item_id"])
                provider_status = str(remote_item.get("status") or "")
                if provider_status == "FAILED":
                    if local_item_id not in active_item_ids:
                        continue
                    self.store.transition_audio_operation(
                        owner_user_id,
                        project_id,
                        local_item_id,
                        status="FAILED",
                        item_status="AUDIO_FAILED",
                        result={"batch_id": batch_id, "item_id": remote_item_id},
                        error_code=str(remote_item.get("error_code") or "REMOTE_FAILED"),
                        error_message=str(remote_item.get("error_message") or "声音生成失败"),
                    )
                    continue
                if provider_status == "AWAITING_REVIEW" and remote_item.get("audio_ready"):
                    local_item = local_by_item[local_item_id]
                    speed = _audio_speed(project, local_item_id)
                    generation_version = int(remote_item.get("generation_version") or 1)
                    captions = remote_item.get("captions")
                    subtitle_cues = (
                        captions.get("cues")
                        if isinstance(captions, dict)
                        and isinstance(captions.get("cues"), list)
                        else []
                    )
                    current = local_item.get("outputs", {}).get("audio")
                    current_ref = current.get("external_ref", {}) if isinstance(current, dict) else {}
                    already_downloaded = (
                        current_ref.get("remote_item_id") == remote_item_id
                        and int(current_ref.get("generation_version") or 0) == generation_version
                    )
                    if local_item_id in caption_recovery_item_ids and not already_downloaded:
                        # Caption recovery must never replace the restored current audio with
                        # another remote generation merely because a newer link also exists.
                        continue
                    asset = current
                    if not already_downloaded:
                        directory = (
                            self.storage_root
                            / "projects"
                            / str(owner_user_id)
                            / project_id
                            / local_item_id
                            / "audio"
                        )
                        target = directory / f"v{generation_version}.mp3"
                        temporary = target.with_suffix(".mp3.tmp")
                        try:
                            self.client.download_workbench_audio(
                                token,
                                batch_id,
                                remote_item_id,
                                temporary,
                                max_bytes=self.max_audio_bytes,
                            )
                            target.parent.mkdir(parents=True, exist_ok=True)
                            temporary.replace(target)
                        except BaseException:
                            temporary.unlink(missing_ok=True)
                            raise
                        asset = self.store.add_asset(
                            owner_user_id=owner_user_id,
                            project_id=project_id,
                            item_id=local_item_id,
                            asset_type="audio",
                            source_type="minimax",
                            status="READY",
                            filename=audio_export_filename(
                                local_item, {"metadata": {"speed": speed}}
                            ),
                            managed_path=str(target),
                            external_ref={
                                "batch_id": batch_id,
                                "remote_item_id": remote_item_id,
                                "generation_version": generation_version,
                            },
                            metadata={
                                "provider_status": provider_status,
                                "script_sha256": link.get("metadata", {}).get(
                                    "script_sha256"
                                ),
                                "script_length": link.get("metadata", {}).get(
                                    "script_length"
                                ),
                                "speed": speed,
                                "subtitle_cues": subtitle_cues,
                            },
                            make_current=True,
                        )
                    if isinstance(captions, dict) and isinstance(asset, dict):
                        cues = subtitle_cues
                        if isinstance(cues, list) and (
                            cues or local_item_id not in caption_recovery_item_ids
                        ):
                            updated = self.store.set_item_subtitles(
                                owner_user_id,
                                project_id,
                                local_item_id,
                                {
                                    "source": "minimax_timestamps",
                                    "raw_cues": cues,
                                    "render_cues": cues,
                                    "bound_audio_asset_id": asset["asset_id"],
                                    "bound_video_asset_id": None,
                                    "style": local_item.get("subtitles", {}).get("style", {}),
                                    "status": "READY",
                                    "overflow_risk": False,
                                },
                            )
                            if self.visual_catalog is not None:
                                updated_item = next(
                                    (
                                        value
                                        for value in updated.get("items", [])
                                        if value.get("item_id") == local_item_id
                                    ),
                                    None,
                                )
                                if isinstance(updated_item, Mapping):
                                    try:
                                        refresh_saved_visual_item(
                                            self.store,
                                            owner_user_id=owner_user_id,
                                            project_id=project_id,
                                            item=updated_item,
                                            catalog=self.visual_catalog,
                                        )
                                    except Exception as exc:
                                        log_event(
                                            logger,
                                            "workbench.visual_local_remap_failed",
                                            "声音已完成，但视觉计划本地重映射失败",
                                            level=logging.WARNING,
                                            component="workbench",
                                            user_id=owner_user_id,
                                            project_id=project_id,
                                            item_id=local_item_id,
                                            error_type=type(exc).__name__,
                                        )
                    if not already_downloaded or local_item_id in caption_recovery_item_ids:
                        project = self.store.get_project(owner_user_id, project_id)
                        local_by_item = {item["item_id"]: item for item in project["items"]}
                    if local_item_id in active_item_ids:
                        self.store.transition_audio_operation(
                            owner_user_id,
                            project_id,
                            local_item_id,
                            status="SUCCEEDED",
                            item_status="AUDIO_READY",
                            result={
                                "batch_id": batch_id,
                                "item_id": remote_item_id,
                                "generation_version": generation_version,
                            },
                        )
                elif (
                    provider_status in REMOTE_AUDIO_ACTIVE
                    and local_item_id in active_item_ids
                ):
                    self.store.transition_audio_operation(
                        owner_user_id,
                        project_id,
                        local_item_id,
                        status="RUNNING",
                        item_status=(
                            "AUDIO_QUEUED"
                            if provider_status == "PENDING"
                            else "AUDIO_RUNNING"
                        ),
                        result={
                            "batch_id": batch_id,
                            "item_id": remote_item_id,
                            "provider_status": provider_status,
                        },
                    )
        return self.store.get_project(owner_user_id, project_id)

    def retry(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        token: str,
        *,
        idempotency_key: str,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        speech = _speech_settings(settings)
        matching_links = [
            link
            for link in project["links"]
            if link.get("item_id") == item_id
            and link["system"] == "runninghub"
            and link["relation"] == "digital_human_audio_item"
        ]
        link = matching_links[-1] if matching_links else None
        if link is None:
            raise ValueError("当前脚本行没有数字人声音任务")
        batch_id = str(link.get("metadata", {}).get("batch_id") or "")
        if not batch_id:
            raise ValueError("声音任务缺少数字人批次编号")
        self.store.prepare_item_audio_generation(
            owner_user_id, project_id, item_id
        )
        self.store.create_operation(
            owner_user_id=owner_user_id,
            project_id=project_id,
            item_id=item_id,
            operation_type="AUDIO_GENERATE",
            idempotency_key=idempotency_key,
            payload={
                "retry": True,
                "remote_item_id": link["external_id"],
                "speech_settings": speech,
            },
        )
        self.client.retry_workbench_audio(
            token,
            batch_id,
            str(link["external_id"]),
            speed=speech["speed"],
        )
        self.store.transition_audio_operation(
            owner_user_id,
            project_id,
            item_id,
            status="RUNNING",
            item_status="AUDIO_QUEUED",
            result={"batch_id": batch_id, "item_id": link["external_id"]},
        )
        return self.sync(owner_user_id, project_id, token)

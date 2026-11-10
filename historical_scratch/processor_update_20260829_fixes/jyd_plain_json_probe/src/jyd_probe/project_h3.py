from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import threading
from typing import Any, Callable
import uuid

from .auth_center import AuthCenterClient
from .caption_alignment import CaptionAlignmentError, alignment_matches
from .project_h3_media import H3MediaAssets, prepare_h3_media
from .project_store import ProjectStore


H3_MODE = "minimax_h3_ref2va"
H3_SAFE_CUT_ALIGNMENT_SCHEMA = "jyd.h3-safe-cut-alignment.v1"
H3_OPEN_BATCH_STATUSES = {
    "AWAITING_COST_CONFIRMATION",
    "ACTIVE",
    "QUEUED",
    "RUNNING",
}
H3_ACTIVE_ITEM_STATUSES = {
    "ACTIVE",
    "QUEUED",
    "RUNNING",
    "PENDING",
    "UPLOADING",
    "SUBMITTED",
    "WAITING_DEPENDENCY",
    "WAITING_REGENERATION_DEPENDENCY",
}


def _h3_segment_cache_key(
    *,
    remote_batch_id: str,
    remote_item_id: str,
    segment_id: str,
) -> str:
    payload = {
        "remote_batch_id": str(remote_batch_id or ""),
        "remote_item_id": str(remote_item_id or ""),
        "segment_id": str(segment_id or ""),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _h3_segment_cache_files(
    storage_root: Path,
    *,
    owner_user_id: str,
    project_id: str,
    item_id: str,
    remote_batch_id: str,
    remote_item_id: str,
    segment_id: str,
) -> tuple[Path, Path]:
    key = _h3_segment_cache_key(
        remote_batch_id=remote_batch_id,
        remote_item_id=remote_item_id,
        segment_id=segment_id,
    )
    directory = (
        Path(storage_root).resolve()
        / "projects"
        / str(owner_user_id)
        / str(project_id)
        / str(item_id)
        / "h3"
        / "segment-cache"
        / key
    )
    return directory / "current.mp4", directory / "current.json"


def current_h3_segment_preview_path(
    project: dict[str, Any],
    *,
    item_id: str,
    segment_number: int,
    storage_root: Path,
) -> Path:
    """Resolve one current, already-materialized H3 source segment for review."""

    if segment_number < 1:
        raise ValueError("H3 分段序号必须从 1 开始")
    item = next(
        (
            value
            for value in project.get("items", [])
            if isinstance(value, dict) and str(value.get("item_id") or "") == item_id
        ),
        None,
    )
    if item is None:
        raise KeyError("脚本行不存在")
    root = Path(storage_root).resolve()
    owner = project.get("owner") if isinstance(project.get("owner"), dict) else {}
    owner_user_id = str(owner.get("user_id") or "").strip()
    settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
    h3 = settings.get("h3") if isinstance(settings.get("h3"), dict) else {}
    raw_segments = h3.get("segments")
    segments = raw_segments if isinstance(raw_segments, list) else []
    current_segment = None
    for value in segments:
        if not isinstance(value, dict):
            continue
        try:
            index = int(value.get("index"))
        except (TypeError, ValueError):
            continue
        if index == segment_number - 1:
            current_segment = value
            break
    remote_batch_id = str(h3.get("remote_batch_id") or "").strip()
    remote_item_id = str(h3.get("remote_item_id") or "").strip()
    segment_id = (
        str(current_segment.get("segment_id") or "").strip()
        if isinstance(current_segment, dict)
        else ""
    )
    if owner_user_id and remote_batch_id and remote_item_id and segment_id:
        cache_path, _ = _h3_segment_cache_files(
            root,
            owner_user_id=owner_user_id,
            project_id=str(project.get("project_id") or ""),
            item_id=item_id,
            remote_batch_id=remote_batch_id,
            remote_item_id=remote_item_id,
            segment_id=segment_id,
        )
        cache_path = cache_path.resolve()
        try:
            cache_path.relative_to(root)
        except ValueError as exc:
            raise FileNotFoundError("H3 分段文件不存在") from exc
        if cache_path.is_file() and cache_path.stat().st_size > 0:
            return cache_path

    # Compatibility for H3 assets produced before incremental segment caching.
    outputs = item.get("outputs") if isinstance(item.get("outputs"), dict) else {}
    base_video = (
        outputs.get("base_video")
        if isinstance(outputs.get("base_video"), dict)
        else None
    )
    if base_video is None or str(base_video.get("source_type") or "").lower() != "h3":
        raise FileNotFoundError("当前 H3 片段尚未准备完成")
    metadata = (
        base_video.get("metadata")
        if isinstance(base_video.get("metadata"), dict)
        else {}
    )
    source_segment_ids = metadata.get("source_segment_ids")
    if not isinstance(source_segment_ids, list) or not source_segment_ids:
        h3 = item.get("settings", {}).get("h3", {})
        source_segment_ids = (
            h3.get("segments") if isinstance(h3.get("segments"), list) else []
        )
    if segment_number > len(source_segment_ids):
        raise FileNotFoundError("H3 分段不存在")
    if current_segment is not None and metadata.get("source_segment_ids"):
        bound_segment_id = str(source_segment_ids[segment_number - 1] or "").strip()
        if bound_segment_id != segment_id:
            raise FileNotFoundError("当前 H3 分段尚未准备完成")

    base_path = Path(str(base_video.get("managed_path") or "")).resolve()
    try:
        base_path.relative_to(root)
    except ValueError as exc:
        raise FileNotFoundError("H3 分段文件不存在") from exc
    path = (
        base_path.parent
        / "segments"
        / f"segment-{segment_number:03d}.mp4"
    ).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise FileNotFoundError("H3 分段文件不存在") from exc
    if not path.is_file() or path.stat().st_size <= 0:
        raise FileNotFoundError("H3 分段文件不存在")
    return path


class ProjectH3Coordinator:
    """Bind the cloud H3 quote/confirm lifecycle to an existing JYD project."""

    def __init__(
        self,
        store: ProjectStore,
        client: AuthCenterClient,
        *,
        storage_root: Path | None = None,
        max_video_bytes: int = 2 * 1024 * 1024 * 1024,
        max_segment_download_workers: int = 3,
        caption_aligner: Any = None,
        require_precise_alignment: bool = False,
        media_preparer: Callable[..., H3MediaAssets] = prepare_h3_media,
    ):
        self.store = store
        self.client = client
        self.storage_root = (
            Path(storage_root).resolve() if storage_root is not None else None
        )
        self.max_video_bytes = int(max_video_bytes)
        self.max_segment_download_workers = max(
            1, min(8, int(max_segment_download_workers))
        )
        self.caption_aligner = caption_aligner
        self.require_precise_alignment = bool(require_precise_alignment)
        self.media_preparer = media_preparer
        self._segment_download_locks: dict[str, threading.Lock] = {}
        self._segment_download_locks_guard = threading.Lock()

    def accounts(self, token: str) -> dict[str, Any]:
        return self.client.list_h3_execution_accounts(token)

    @staticmethod
    def _h3_settings(project: dict[str, Any]) -> dict[str, Any]:
        settings = (
            project.get("settings")
            if isinstance(project.get("settings"), dict)
            else {}
        )
        h3 = settings.get("h3") if isinstance(settings.get("h3"), dict) else {}
        if settings.get("generation_mode") != H3_MODE:
            raise ValueError("请先把项目生成方式切换为 MiniMax H3")
        return h3

    @staticmethod
    def _batch_records(h3: dict[str, Any]) -> list[dict[str, Any]]:
        """Return stored batches plus the legacy latest-batch fields."""

        records = [
            dict(value)
            for value in h3.get("batches", [])
            if isinstance(value, dict) and str(value.get("batch_id") or "").strip()
        ]
        legacy_batch_id = str(h3.get("remote_batch_id") or "").strip()
        if legacy_batch_id and not any(
            str(value.get("batch_id") or "") == legacy_batch_id for value in records
        ):
            records.append(
                {
                    "batch_id": legacy_batch_id,
                    "prepare_key": str(h3.get("prepare_key") or ""),
                    "status": str(h3.get("remote_status") or "").upper(),
                    "fee_snapshot": h3.get("fee_snapshot"),
                    "last_synced_at": h3.get("last_synced_at"),
                }
            )
        return records

    @classmethod
    def _batch_record(
        cls,
        h3: dict[str, Any],
        *,
        batch_id: str = "",
        prepare_key: str = "",
    ) -> dict[str, Any] | None:
        clean_batch_id = str(batch_id or "").strip()
        clean_prepare_key = str(prepare_key or "").strip()
        for record in reversed(cls._batch_records(h3)):
            if clean_batch_id and str(record.get("batch_id") or "") == clean_batch_id:
                return record
            if clean_prepare_key and str(record.get("prepare_key") or "") == clean_prepare_key:
                return record
        return None

    @classmethod
    def _prepare_key_for_snapshot(
        cls, h3: dict[str, Any], snapshot: dict[str, Any]
    ) -> str:
        record = cls._batch_record(
            h3, batch_id=str(snapshot.get("batch_id") or "")
        )
        return str(
            (record or {}).get("prepare_key") or h3.get("prepare_key") or ""
        )

    @staticmethod
    def _local_h3_files_ready(item: dict[str, Any]) -> bool:
        outputs = item.get("outputs") if isinstance(item.get("outputs"), dict) else {}
        for asset_type in ("audio", "base_video"):
            asset = outputs.get(asset_type)
            if not isinstance(asset, dict):
                return False
            path = Path(str(asset.get("managed_path") or ""))
            try:
                if not path.is_file() or path.stat().st_size <= 0:
                    return False
            except OSError:
                return False
        return True

    @classmethod
    def _h3_item_needs_materialization(cls, item: dict[str, Any]) -> bool:
        h3 = (
            item.get("settings", {}).get("h3", {})
            if isinstance(item.get("settings"), dict)
            else {}
        )
        return (
            bool(str(h3.get("remote_batch_id") or "").strip())
            and str(h3.get("remote_status") or "").upper() == "SUCCESS"
            and not cls._local_h3_files_ready(item)
        )

    @staticmethod
    def _target_items(
        project: dict[str, Any], item_ids: list[str] | None
    ) -> list[dict[str, Any]]:
        items = [value for value in project.get("items", []) if isinstance(value, dict)]
        if item_ids is None:
            return items
        clean_ids = [str(value or "").strip() for value in item_ids]
        if not clean_ids or any(not value for value in clean_ids):
            raise ValueError("H3 至少需要选择一条脚本")
        if len(set(clean_ids)) != len(clean_ids):
            raise ValueError("H3 脚本选择不能重复")
        by_id = {str(item.get("item_id") or ""): item for item in items}
        missing = [value for value in clean_ids if value not in by_id]
        if missing:
            raise ValueError("H3 选择中包含不属于当前项目的脚本")
        return [by_id[value] for value in clean_ids]

    @staticmethod
    def _latest_minimax_audio(item: dict[str, Any]) -> dict[str, Any] | None:
        history = item.get("asset_history", {}).get("audio", [])
        candidates = [
            value
            for value in history
            if isinstance(value, dict)
            and value.get("source_type") == "minimax"
            and value.get("status") == "READY"
        ]
        return candidates[-1] if candidates else None

    @staticmethod
    def _audio_bound_script(
        item: dict[str, Any], audio: dict[str, Any]
    ) -> str:
        """Return the immutable script actually bound to the selected audio."""

        table_script = str(item.get("script_text") or "").strip()
        subtitles = (
            item.get("subtitles") if isinstance(item.get("subtitles"), dict) else {}
        )
        cues = subtitles.get("raw_cues")
        cue_script = ""
        if isinstance(cues, list):
            cue_script = "".join(
                str(cue.get("text") or "")
                for cue in cues
                if isinstance(cue, dict)
            ).strip()
        metadata = (
            audio.get("metadata") if isinstance(audio.get("metadata"), dict) else {}
        )
        expected_sha = str(metadata.get("script_sha256") or "").strip().lower()
        candidates = [value for value in (cue_script, table_script) if value]
        if expected_sha:
            for candidate in candidates:
                if hashlib.sha256(candidate.encode("utf-8")).hexdigest() == expected_sha:
                    return candidate
            raise ValueError(
                f"第 {item.get('row_key')} 行当前脚本与已生成声音不一致，请重新生成声音"
            )
        if candidates:
            return candidates[0]
        raise ValueError(f"第 {item.get('row_key')} 行声音缺少原稿")

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _safe_cut_alignment(
        self,
        owner_user_id: str,
        project_id: str,
        item: dict[str, Any],
        audio: dict[str, Any],
        script_text: str,
    ) -> dict[str, Any] | None:
        """Build the small, audio-bound alignment contract consumed by H3."""

        audio_path = Path(str(audio.get("managed_path") or ""))
        if not audio_path.is_file():
            raise ValueError(
                f"第 {item.get('row_key')} 行缺少可供本地 ASR 对齐的声音文件"
            )
        subtitles = (
            dict(item.get("subtitles"))
            if isinstance(item.get("subtitles"), dict)
            else {}
        )
        alignment = subtitles.get("asr_alignment")
        alignment_is_current = alignment_matches(
            alignment,
            script=script_text,
            audio_asset_id=str(audio.get("asset_id") or ""),
            audio_version=audio.get("version"),
        )
        if not alignment_is_current:
            if self.caption_aligner is None:
                if self.require_precise_alignment:
                    raise ValueError(
                        f"第 {item.get('row_key')} 行无法建立 H3 安全切点："
                        "本机 ASR 服务未配置"
                    )
                return None
            try:
                alignment = self.caption_aligner.align(
                    audio_path,
                    script=script_text,
                    raw_cues=subtitles.get("raw_cues", []),
                    audio_asset_id=str(audio.get("asset_id") or ""),
                    audio_version=audio.get("version"),
                )
            except CaptionAlignmentError as exc:
                raise ValueError(
                    f"第 {item.get('row_key')} 行无法建立 H3 安全切点：{exc}"
                ) from exc
            subtitles["asr_alignment"] = alignment
            self.store.set_item_subtitles(
                owner_user_id,
                project_id,
                str(item["item_id"]),
                subtitles,
            )
        if not isinstance(alignment, dict):
            raise ValueError(f"第 {item.get('row_key')} 行本地 ASR 对齐结果无效")
        raw_ranges = alignment.get("ranges")
        if not isinstance(raw_ranges, list) or not raw_ranges:
            raise ValueError(f"第 {item.get('row_key')} 行本地 ASR 没有返回安全切点")
        ranges: list[dict[str, int]] = []
        for position, value in enumerate(raw_ranges, start=1):
            if not isinstance(value, dict):
                raise ValueError(
                    f"第 {item.get('row_key')} 行本地 ASR 第 {position} 个切点无效"
                )
            try:
                ranges.append(
                    {
                        "script_start": int(value["start"]),
                        "script_end": int(value["end"]),
                        "start_us": int(value["start_us"]),
                        "end_us": int(value["end_us"]),
                    }
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"第 {item.get('row_key')} 行本地 ASR 第 {position} 个切点无效"
                ) from exc
        audio_ref = audio.get("external_ref", {})
        return {
            "schema": H3_SAFE_CUT_ALIGNMENT_SCHEMA,
            "source": "jyd_local_funasr",
            "script_sha256": hashlib.sha256(script_text.encode("utf-8")).hexdigest(),
            "audio_sha256": self._sha256_file(audio_path),
            "audio_batch_id": str(audio_ref.get("batch_id") or ""),
            "audio_item_id": str(audio_ref.get("remote_item_id") or ""),
            "audio_generation_version": int(
                audio_ref.get("generation_version") or 1
            ),
            "ranges": ranges,
        }

    def approve_audio(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        *,
        item_ids: list[str] | None,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        self._h3_settings(project)
        targets = self._target_items(project, item_ids)
        reviewed: list[str] = []
        for item in targets:
            audio = self._latest_minimax_audio(item)
            if audio is None:
                raise ValueError(
                    f"第 {item.get('row_key')} 行还没有可审核的 MiniMax 完整音频"
                )
            audio_ref = audio.get("external_ref", {})
            if not audio_ref.get("batch_id") or not audio_ref.get("remote_item_id"):
                raise ValueError(
                    f"第 {item.get('row_key')} 行 MiniMax 声音缺少云端版本信息"
                )
            result = self.client.approve_h3_audio_source(
                token,
                audio_batch_id=str(audio_ref["batch_id"]),
                audio_item_id=str(audio_ref["remote_item_id"]),
                audio_generation_version=int(audio_ref.get("generation_version") or 1),
            )
            if str(result.get("status") or "").upper() != "SUCCESS":
                raise ValueError(f"第 {item.get('row_key')} 行声音审核未成功")
            self.store.mark_h3_audio_reviewed(
                owner_user_id,
                project_id,
                str(item["item_id"]),
                asset_id=str(audio["asset_id"]),
                reviewed_at=str(result.get("reviewed_at") or ""),
            )
            reviewed.append(str(item["item_id"]))
        return {
            "project": self.store.get_project(owner_user_id, project_id),
            "reviewed_item_ids": reviewed,
        }

    def prepare(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        *,
        idempotency_key: str,
        selected_account_ids: list[int],
        item_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        clean_key = str(idempotency_key or "").strip()
        if not clean_key:
            raise ValueError("H3 费用预览幂等键不能为空")
        if (
            not selected_account_ids
            or any(
                type(value) is not int or value <= 0
                for value in selected_account_ids
            )
            or len(set(selected_account_ids)) != len(selected_account_ids)
        ):
            raise ValueError("请选择有效且不重复的 H3 执行账号")
        project = self.store.get_project(owner_user_id, project_id)
        h3 = self._h3_settings(project)
        target_items = self._target_items(project, item_ids)
        existing_batch = self._batch_record(h3, prepare_key=clean_key)
        if existing_batch is not None:
            snapshot = self.client.get_h3_batch(
                token, str(existing_batch["batch_id"])
            )
            project = self.store.set_h3_batch_snapshot(
                owner_user_id,
                project_id,
                prepare_key=clean_key,
                snapshot=snapshot,
            )
            return {"project": project, "h3_batch": snapshot}

        blocked_rows: list[str] = []
        for item in target_items:
            item_h3 = (
                item.get("settings", {}).get("h3", {})
                if isinstance(item.get("settings"), dict)
                else {}
            )
            item_remote_status = str(item_h3.get("remote_status") or "").upper()
            item_status = str(item.get("status") or "").upper()
            segments = item_h3.get("segments")
            segment_active = isinstance(segments, list) and any(
                isinstance(segment, dict)
                and str(segment.get("status") or "").upper()
                in H3_ACTIVE_ITEM_STATUSES
                for segment in segments
            )
            if (
                item_remote_status in H3_OPEN_BATCH_STATUSES
                or item_status in {"H3_COST_PENDING", "H3_QUEUED", "H3_RUNNING"}
                or segment_active
            ):
                blocked_rows.append(str(item.get("row_key") or item.get("item_id")))
        if blocked_rows:
            raise ValueError(
                f"第 {', '.join(blocked_rows)} 行已有未结束的 H3 任务或费用预览"
            )

        pending_audio_item_ids: list[str] = []
        for item in target_items:
            audio = self._latest_minimax_audio(item) or {}
            metadata = (
                audio.get("metadata")
                if isinstance(audio.get("metadata"), dict)
                else {}
            )
            if str(metadata.get("provider_status") or "").upper() != "SUCCESS":
                pending_audio_item_ids.append(str(item["item_id"]))
        if pending_audio_item_ids:
            reviewed = self.approve_audio(
                owner_user_id,
                project_id,
                token,
                item_ids=pending_audio_item_ids,
            )
            project = reviewed["project"]
            h3 = self._h3_settings(project)
            target_items = self._target_items(project, item_ids)

        prepared_audio: dict[str, tuple[dict[str, Any], str, dict[str, Any] | None]] = {}
        for item in target_items:
            audio = self._latest_minimax_audio(item)
            audio_ref = (
                audio.get("external_ref", {}) if isinstance(audio, dict) else {}
            )
            if not audio_ref.get("batch_id") or not audio_ref.get("remote_item_id"):
                raise ValueError(
                    f"第 {item.get('row_key')} 行还没有可复用的 MiniMax 完整音频"
                )
            provider_status = str(audio.get("metadata", {}).get("provider_status") or "")
            if provider_status != "SUCCESS":
                raise ValueError(f"第 {item.get('row_key')} 行 MiniMax 声音锁定失败")
            script_text = self._audio_bound_script(item, audio)
            prepared_audio[str(item["item_id"])] = (
                audio,
                script_text,
                self._safe_cut_alignment(
                    owner_user_id,
                    project_id,
                    item,
                    audio,
                    script_text,
                ),
            )

        cloud_image_by_source: dict[str, str] = {}
        cloud_images: list[str] = []
        row_cloud_images: dict[str, str] = {}
        for item in target_items:
            image = item.get("inputs", {}).get("image")
            if not isinstance(image, dict):
                raise ValueError(
                    f"第 {item.get('row_key')} 行尚未分配人物图，请先应用上方图片分配规则"
                )
            path = Path(str(image.get("managed_path") or ""))
            if not path.is_file():
                raise ValueError(
                    f"第 {item.get('row_key')} 行人物图文件不存在: {image.get('filename') or image.get('asset_id')}"
                )
            source_key = str(image.get("metadata", {}).get("sha256") or path.resolve())
            asset_id = cloud_image_by_source.get(source_key)
            if asset_id is None:
                uploaded = self.client.upload_workbench_batch_asset(
                    token,
                    path,
                    kind="image",
                    filename=str(image.get("filename") or path.name),
                )
                asset_id = str(uploaded.get("asset_id") or "").strip()
                if not asset_id:
                    raise ValueError("数字人网站没有返回 H3 人物图素材编号")
                cloud_image_by_source[source_key] = asset_id
                cloud_images.append(asset_id)
            row_cloud_images[str(item["item_id"])] = asset_id

        rows: list[dict[str, Any]] = []
        for item in target_items:
            audio, script_text, safe_cut_alignment = prepared_audio[
                str(item["item_id"])
            ]
            audio_ref = audio.get("external_ref", {})
            reference = item.get("inputs", {}).get("h3_reference_video")
            if not isinstance(reference, dict):
                raise ValueError(
                    f"第 {item.get('row_key')} 行尚未绑定 H3 参考视频"
                )
            path = Path(str(reference.get("managed_path") or ""))
            if not path.is_file():
                raise ValueError(
                    f"第 {item.get('row_key')} 行 H3 参考视频文件不存在"
                )
            uploaded = self.client.upload_workbench_batch_asset(
                token,
                path,
                kind="video",
                filename=str(reference.get("filename") or path.name),
            )
            video_asset_id = str(uploaded.get("asset_id") or "").strip()
            if not video_asset_id:
                raise ValueError("数字人网站没有返回 H3 参考视频素材编号")
            item_settings = (
                item.get("settings")
                if isinstance(item.get("settings"), dict)
                else {}
            )
            item_h3 = (
                item_settings.get("h3")
                if isinstance(item_settings.get("h3"), dict)
                else {}
            )
            row: dict[str, Any] = {
                "row_id": str(item.get("row_key") or item.get("item_id")),
                "script_text": script_text,
                "video_asset_id": video_asset_id,
                "reference_image_asset_ids": [
                    row_cloud_images[str(item["item_id"])]
                ],
                "audio_batch_id": str(audio_ref["batch_id"]),
                "audio_item_id": str(audio_ref["remote_item_id"]),
                "audio_generation_version": int(
                    audio_ref.get("generation_version") or 1
                ),
            }
            if safe_cut_alignment is not None:
                row["audio_alignment"] = safe_cut_alignment
            overrides = (
                item_h3.get("overrides")
                if isinstance(item_h3.get("overrides"), dict)
                else {}
            )
            if overrides:
                row["overrides"] = overrides
            rows.append(row)

        defaults = (
            h3.get("defaults") if isinstance(h3.get("defaults"), dict) else {}
        )
        snapshot = self.client.prepare_h3_batch(
            token,
            {
                "name": str(project.get("name") or "JYD H3 批次"),
                "request_key": clean_key,
                "reference_image_asset_ids": cloud_images,
                "selected_account_ids": selected_account_ids,
                "defaults": {
                    "continuity_mode": defaults.get(
                        "continuity_mode", "loop_anchor"
                    ),
                    "generation_tail_seconds": defaults.get(
                        "generation_tail_seconds", 0.1
                    ),
                    "resolution": {
                        "aspect_ratio": defaults.get(
                            "aspect_ratio", "9:16 (Portrait Widescreen)"
                        ),
                        "megapixels": defaults.get("megapixels", 1.0),
                        "multiple": 32,
                    },
                },
                "rows": rows,
            },
        )
        batch_id = str(snapshot.get("batch_id") or "").strip()
        if not batch_id:
            raise ValueError("数字人网站没有返回 H3 批次编号")
        self.store.add_link(
            owner_user_id=owner_user_id,
            project_id=project_id,
            system="runninghub_h3",
            relation="generation_batch",
            external_id=batch_id,
            metadata={"prepare_key": clean_key},
        )
        project = self.store.set_h3_batch_snapshot(
            owner_user_id,
            project_id,
            prepare_key=clean_key,
            snapshot=snapshot,
        )
        return {"project": project, "h3_batch": snapshot}

    def confirm(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        *,
        batch_id: str = "",
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        h3 = self._h3_settings(project)
        clean_batch_id = str(batch_id or h3.get("remote_batch_id") or "").strip()
        if not clean_batch_id:
            raise ValueError("请先计算 H3 分段与费用")
        record = self._batch_record(h3, batch_id=clean_batch_id)
        if record is None:
            raise ValueError("H3 费用预览不属于当前项目或已失效")
        snapshot = self.client.confirm_h3_batch(token, clean_batch_id)
        project = self.store.set_h3_batch_snapshot(
            owner_user_id,
            project_id,
            prepare_key=str(record.get("prepare_key") or ""),
            snapshot=snapshot,
        )
        return {"project": project, "h3_batch": snapshot}

    @staticmethod
    def _require_project_segment(
        project: dict[str, Any], segment_id: str
    ) -> dict[str, Any]:
        clean_id = str(segment_id or "").strip()
        for item in project.get("items", []):
            if not isinstance(item, dict):
                continue
            h3 = item.get("settings", {}).get("h3", {})
            for segment in h3.get("segments", []) if isinstance(h3, dict) else []:
                if (
                    isinstance(segment, dict)
                    and str(segment.get("segment_id") or "") == clean_id
                ):
                    return segment
        raise ValueError("当前 H3 批次已更新，请刷新状态后重试当前失败分段")

    def prepare_regeneration(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        segment_id: str,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        self._h3_settings(project)
        self._require_project_segment(project, segment_id)
        return self.client.prepare_h3_segment_regeneration(token, segment_id)

    def confirm_regeneration(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        segment_id: str,
        *,
        request_key: str,
        quote_token: str,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        h3 = self._h3_settings(project)
        self._require_project_segment(project, segment_id)
        snapshot = self.client.confirm_h3_segment_regeneration(
            token,
            segment_id,
            request_key=str(request_key or "").strip(),
            quote_token=str(quote_token or "").strip(),
        )
        project = self.store.set_h3_batch_snapshot(
            owner_user_id,
            project_id,
            prepare_key=self._prepare_key_for_snapshot(h3, snapshot),
            snapshot=snapshot,
        )
        return {"project": project, "h3_batch": snapshot}

    def prepare_retry(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        segment_id: str,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        self._h3_settings(project)
        self._require_project_segment(project, segment_id)
        return self.client.prepare_h3_segment_retry(token, segment_id)

    def confirm_retry(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        segment_id: str,
        *,
        request_key: str,
        quote_token: str,
        cost_confirmed: bool,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        h3 = self._h3_settings(project)
        self._require_project_segment(project, segment_id)
        snapshot = self.client.confirm_h3_segment_retry(
            token,
            segment_id,
            request_key=str(request_key or "").strip(),
            quote_token=str(quote_token or "").strip(),
            cost_confirmed=cost_confirmed,
        )
        project = self.store.set_h3_batch_snapshot(
            owner_user_id,
            project_id,
            prepare_key=self._prepare_key_for_snapshot(h3, snapshot),
            snapshot=snapshot,
        )
        return {"project": project, "h3_batch": snapshot}

    def cancel_segment(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        segment_id: str,
        *,
        request_key: str,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        h3 = self._h3_settings(project)
        self._require_project_segment(project, segment_id)
        snapshot = self.client.cancel_h3_segment(
            token, segment_id, request_key=str(request_key or "").strip()
        )
        project = self.store.set_h3_batch_snapshot(
            owner_user_id,
            project_id,
            prepare_key=self._prepare_key_for_snapshot(h3, snapshot),
            snapshot=snapshot,
        )
        return {"project": project, "h3_batch": snapshot}

    def sync(
        self, owner_user_id: str, project_id: str, token: str
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        h3 = self._h3_settings(project)
        records = self._batch_records(h3)
        recorded_batch_ids = {
            str(record.get("batch_id") or "").strip() for record in records
        }
        for item in project.get("items", []):
            if not isinstance(item, dict) or not self._h3_item_needs_materialization(item):
                continue
            item_h3 = (
                item.get("settings", {}).get("h3", {})
                if isinstance(item.get("settings"), dict)
                else {}
            )
            item_batch_id = str(item_h3.get("remote_batch_id") or "").strip()
            if not item_batch_id or item_batch_id in recorded_batch_ids:
                continue
            # Older single-batch workbench versions only kept the latest batch
            # at project level.  A completed non-latest batch can therefore
            # survive solely on its item after the installation directory has
            # moved.  Include that item-owned batch so its cloud result can be
            # downloaded again without submitting or charging for a new job.
            records.append(
                {
                    "batch_id": item_batch_id,
                    "prepare_key": "",
                    "status": str(item_h3.get("remote_status") or "").upper(),
                    "last_synced_at": item_h3.get("last_synced_at"),
                }
            )
            recorded_batch_ids.add(item_batch_id)
        if not records:
            return {"project": project, "h3_batch": None, "h3_batches": []}

        latest_batch_id = str(h3.get("remote_batch_id") or "")
        materialization_batch_ids = {
            str(item.get("settings", {}).get("h3", {}).get("remote_batch_id") or "")
            for item in project.get("items", [])
            if isinstance(item, dict)
            and self._h3_item_needs_materialization(item)
        }
        records_to_sync = [
            record
            for record in records
            if str(record.get("status") or "").upper() in H3_OPEN_BATCH_STATUSES
            or str(record.get("batch_id") or "") == latest_batch_id
            or str(record.get("batch_id") or "") in materialization_batch_ids
        ]
        snapshots: list[dict[str, Any]] = []
        for record in records_to_sync:
            remote_batch_id = str(record.get("batch_id") or "")
            snapshot = self.client.get_h3_batch(token, remote_batch_id)
            if self.storage_root is not None:
                snapshot = self._cache_snapshot_segments(
                    owner_user_id,
                    project_id,
                    token,
                    project=project,
                    snapshot=snapshot,
                )
            snapshots.append(snapshot)
            project = self.store.set_h3_batch_snapshot(
                owner_user_id,
                project_id,
                prepare_key=str(record.get("prepare_key") or ""),
                snapshot=snapshot,
            )
            if self.storage_root is not None:
                project = self._materialize_ready_items(
                    owner_user_id,
                    project_id,
                    token,
                    project=project,
                    snapshot=snapshot,
                )
        latest_snapshot = next(
            (
                value
                for value in reversed(snapshots)
                if str(value.get("batch_id") or "") == latest_batch_id
            ),
            snapshots[-1] if snapshots else None,
        )
        return {
            "project": project,
            "h3_batch": latest_snapshot,
            "h3_batches": snapshots,
        }

    @staticmethod
    def _segment_result_signature(segment: dict[str, Any]) -> str:
        delivery = (
            segment.get("video_delivery")
            if isinstance(segment.get("video_delivery"), dict)
            else {}
        )
        provider_signature = str(delivery.get("result_signature") or "").strip().lower()
        if provider_signature:
            return provider_signature
        normalized_sha256 = str(
            segment.get("normalized_video_sha256") or ""
        ).strip().lower()
        completed_at = str(segment.get("completed_at") or "").strip()
        video_url = str(segment.get("normalized_video_download_url") or "").strip()
        payload = {
            "normalized_video_sha256": normalized_sha256,
            "completed_at": completed_at,
            # Legacy H3 servers do not expose a result hash or completion time.
            # Keep the URL in the identity so those responses remain usable.
            "video": video_url,
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _segment_signature(
        cls, segments: list[dict[str, Any]]
    ) -> str:
        payload = [
            {
                "segment_id": str(segment.get("segment_id") or ""),
                "index": int(segment.get("index") or 0),
                "result_signature": cls._segment_result_signature(segment),
            }
            for segment in segments
        ]
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _ordered_segments(remote_item: dict[str, Any]) -> list[dict[str, Any]] | None:
        raw_segments = remote_item.get("segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            return None
        segments = [value for value in raw_segments if isinstance(value, dict)]
        if len(segments) != len(raw_segments):
            return None
        try:
            segments.sort(key=lambda value: int(value.get("index")))
            indexes = [int(value.get("index")) for value in segments]
        except (TypeError, ValueError):
            return None
        if indexes != list(range(len(segments))):
            return None
        return segments

    @classmethod
    def _ready_segments(
        cls, remote_item: dict[str, Any]
    ) -> list[dict[str, Any]] | None:
        segments = cls._ordered_segments(remote_item)
        if segments is None:
            return None
        for segment in segments:
            if (
                str(segment.get("status") or "").upper() != "SUCCESS"
                or not str(segment.get("segment_id") or "").strip()
                or not cls._segment_is_downloadable(segment)
                or not str(segment.get("script_text") or "").strip()
            ):
                return None
        return segments

    @staticmethod
    def _segment_is_downloadable(segment: dict[str, Any]) -> bool:
        delivery = (
            segment.get("video_delivery")
            if isinstance(segment.get("video_delivery"), dict)
            else {}
        )
        direct_ready = bool(
            str(delivery.get("mode") or "") == "runninghub_direct"
            and str(delivery.get("download_url") or "").strip()
            and str(delivery.get("result_signature") or "").strip()
        )
        return bool(
            str(segment.get("status") or "").upper() == "SUCCESS"
            and str(segment.get("segment_id") or "").strip()
            and (
                direct_ready
                or str(segment.get("normalized_video_download_url") or "").strip()
            )
        )

    def _segment_cache_files(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        remote_item: dict[str, Any],
        segment: dict[str, Any],
    ) -> tuple[Path, Path]:
        assert self.storage_root is not None
        return _h3_segment_cache_files(
            self.storage_root,
            owner_user_id=owner_user_id,
            project_id=project_id,
            item_id=item_id,
            remote_batch_id=str(remote_item.get("batch_id") or ""),
            remote_item_id=str(remote_item.get("item_id") or ""),
            segment_id=str(segment.get("segment_id") or ""),
        )

    @staticmethod
    def _read_segment_cache_metadata(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _cached_segment_path(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        remote_item: dict[str, Any],
        segment: dict[str, Any],
        *,
        require_current: bool,
    ) -> Path | None:
        video_path, metadata_path = self._segment_cache_files(
            owner_user_id, project_id, item_id, remote_item, segment
        )
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            return None
        if not require_current:
            return video_path
        metadata = self._read_segment_cache_metadata(metadata_path)
        if metadata.get("result_signature") != self._segment_result_signature(segment):
            return None
        return video_path

    def _segment_download_lock(self, video_path: Path) -> threading.Lock:
        key = str(video_path.resolve())
        with self._segment_download_locks_guard:
            return self._segment_download_locks.setdefault(key, threading.Lock())

    def _download_segment_to_cache(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        *,
        item_id: str,
        remote_item: dict[str, Any],
        segment: dict[str, Any],
    ) -> Path:
        video_path, metadata_path = self._segment_cache_files(
            owner_user_id, project_id, item_id, remote_item, segment
        )
        result_signature = self._segment_result_signature(segment)
        lock = self._segment_download_lock(video_path)
        with lock:
            current = self._cached_segment_path(
                owner_user_id,
                project_id,
                item_id,
                remote_item,
                segment,
                require_current=True,
            )
            if current is not None:
                return current
            video_path.parent.mkdir(parents=True, exist_ok=True)
            suffix = uuid.uuid4().hex
            temporary_video = video_path.with_name(f"current.{suffix}.mp4.tmp")
            temporary_metadata = metadata_path.with_name(f"current.{suffix}.json.tmp")
            try:
                self.client.download_h3_segment_video(
                    token,
                    str(segment["segment_id"]),
                    temporary_video,
                    max_bytes=self.max_video_bytes,
                    delivery=(
                        segment.get("video_delivery")
                        if isinstance(segment.get("video_delivery"), dict)
                        else None
                    ),
                )
                if not temporary_video.is_file() or temporary_video.stat().st_size <= 0:
                    raise ValueError("数字人网站返回了空的 H3 分段视频")
                actual_sha256 = self._sha256_file(temporary_video)
                expected_sha256 = str(
                    segment.get("normalized_video_sha256") or ""
                ).strip().lower()
                if expected_sha256 and actual_sha256 != expected_sha256:
                    raise ValueError("H3 分段下载结果与云端版本摘要不一致")
                temporary_video.replace(video_path)
                temporary_metadata.write_text(
                    json.dumps(
                        {
                            "schema": "jyd.h3-segment-cache.v1",
                            "remote_batch_id": str(remote_item.get("batch_id") or ""),
                            "remote_item_id": str(remote_item.get("item_id") or ""),
                            "segment_id": str(segment.get("segment_id") or ""),
                            "segment_index": int(segment.get("index") or 0),
                            "result_signature": result_signature,
                            "normalized_video_sha256": expected_sha256 or None,
                            "local_video_sha256": actual_sha256,
                            "completed_at": str(segment.get("completed_at") or "") or None,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                temporary_metadata.replace(metadata_path)
                return video_path
            finally:
                temporary_video.unlink(missing_ok=True)
                temporary_metadata.unlink(missing_ok=True)

    def _cache_snapshot_segments(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        *,
        project: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Download each successful result immediately and annotate local readiness."""

        cached_snapshot = copy.deepcopy(snapshot)
        local_items_by_row = {
            str(value.get("row_key") or ""): value
            for value in project.get("items", [])
            if isinstance(value, dict)
        }
        pending: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for remote_value in cached_snapshot.get("items", []):
            if not isinstance(remote_value, dict):
                continue
            item = local_items_by_row.get(str(remote_value.get("row_id") or ""))
            if not isinstance(item, dict):
                continue
            remote_item = {"batch_id": cached_snapshot.get("batch_id"), **remote_value}
            raw_segments = remote_value.get("segments")
            if not isinstance(raw_segments, list):
                continue
            for segment in raw_segments:
                if not isinstance(segment, dict) or not str(
                    segment.get("segment_id") or ""
                ).strip():
                    continue
                existing = self._cached_segment_path(
                    owner_user_id,
                    project_id,
                    str(item["item_id"]),
                    remote_item,
                    segment,
                    require_current=False,
                )
                segment["local_preview_ready"] = existing is not None
                segment["local_preview_is_current"] = False
                segment.pop("local_preview_error", None)
                if self._segment_is_downloadable(segment):
                    current = self._cached_segment_path(
                        owner_user_id,
                        project_id,
                        str(item["item_id"]),
                        remote_item,
                        segment,
                        require_current=True,
                    )
                    if current is not None:
                        segment["local_preview_ready"] = True
                        segment["local_preview_is_current"] = True
                    else:
                        pending.append((item, remote_item, segment))

        if not pending:
            return cached_snapshot
        worker_count = min(self.max_segment_download_workers, len(pending))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="h3-segment-download",
        ) as executor:
            futures = {
                executor.submit(
                    self._download_segment_to_cache,
                    owner_user_id,
                    project_id,
                    token,
                    item_id=str(item["item_id"]),
                    remote_item=remote_item,
                    segment=segment,
                ): segment
                for item, remote_item, segment in pending
            }
            for future in as_completed(futures):
                segment = futures[future]
                try:
                    future.result()
                except (OSError, RuntimeError, ValueError) as exc:
                    segment["local_preview_error"] = str(exc)[:500]
                else:
                    segment["local_preview_ready"] = True
                    segment["local_preview_is_current"] = True
        return cached_snapshot

    def _materialize_ready_items(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        *,
        project: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        remote_by_row = {
            str(value.get("row_id") or ""): value
            for value in snapshot.get("items", [])
            if isinstance(value, dict)
        }
        for item in project.get("items", []):
            if not isinstance(item, dict):
                continue
            remote_item = remote_by_row.get(str(item.get("row_key") or ""))
            if not isinstance(remote_item, dict):
                continue
            remote_item = {
                "batch_id": snapshot.get("batch_id"),
                **remote_item,
            }
            segments = self._ready_segments(remote_item)
            if segments is None:
                continue
            self._materialize_item(
                owner_user_id,
                project_id,
                token,
                item=item,
                remote_item=remote_item,
                segments=segments,
            )
            project = self.store.get_project(owner_user_id, project_id)
        return project

    def _materialize_item(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        *,
        item: dict[str, Any],
        remote_item: dict[str, Any],
        segments: list[dict[str, Any]],
    ) -> None:
        signature = self._segment_signature(segments)
        script_text = str(item.get("script_text") or "")
        script_sha256 = hashlib.sha256(script_text.encode("utf-8")).hexdigest()
        script_length = len(script_text)
        item_id = str(item["item_id"])
        segment_paths: list[Path] = []
        for segment in segments:
            cached = self._cached_segment_path(
                owner_user_id,
                project_id,
                item_id,
                remote_item,
                segment,
                require_current=True,
            )
            if cached is None:
                # The cloud result is complete, but at least one current local
                # segment is still downloading or will be retried on next poll.
                return
            segment_paths.append(cached)
        outputs = item.get("outputs") if isinstance(item.get("outputs"), dict) else {}
        current_audio = outputs.get("audio") if isinstance(outputs.get("audio"), dict) else {}
        current_base = (
            outputs.get("base_video")
            if isinstance(outputs.get("base_video"), dict)
            else {}
        )
        current_audio_path = Path(str(current_audio.get("managed_path") or ""))
        current_base_path = Path(str(current_base.get("managed_path") or ""))
        current_files_ready = (
            current_audio_path.is_file()
            and current_audio_path.stat().st_size > 0
            and current_base_path.is_file()
            and current_base_path.stat().st_size > 0
        )
        if (
            current_audio.get("metadata", {}).get("h3_segment_signature") == signature
            and current_base.get("metadata", {}).get("h3_segment_signature") == signature
            and current_files_ready
        ):
            segment_script = "".join(str(value.get("script_text") or "") for value in segments)
            if "".join(segment_script.split()) == "".join(script_text.split()):
                self.store.repair_legacy_h3_script_binding(
                    owner_user_id,
                    project_id,
                    str(item["item_id"]),
                    segment_signature=signature,
                )
            return
        assert self.storage_root is not None
        target_dir = (
            self.storage_root
            / "projects"
            / str(owner_user_id)
            / project_id
            / item_id
            / "h3"
            / signature
        )
        assets = self.media_preparer(
            segment_paths=segment_paths,
            segment_texts=[str(value["script_text"]) for value in segments],
            script_text=script_text,
            target_dir=target_dir,
        )
        duration_us = sum(
            max(1, round(float(value) * 1_000_000))
            for value in assets.segment_durations_seconds
        )
        common_metadata = {
            "h3_segment_signature": signature,
            "remote_batch_id": remote_item.get("batch_id"),
            "remote_item_id": remote_item.get("item_id"),
            "source_segment_ids": [str(value["segment_id"]) for value in segments],
            "authoritative_av": "h3_generated",
            "script_sha256": script_sha256,
            "script_length": script_length,
            "duration_us": duration_us,
        }
        self.store.add_asset(
            owner_user_id=owner_user_id,
            project_id=project_id,
            item_id=item_id,
            asset_type="h3_master_av",
            source_type="h3",
            status="READY",
            filename=f"{item.get('row_key')}-H3原生成片.mp4",
            managed_path=str(assets.master_av_path),
            metadata=common_metadata,
        )
        audio = self.store.add_asset(
            owner_user_id=owner_user_id,
            project_id=project_id,
            item_id=item_id,
            asset_type="audio",
            source_type="h3",
            status="READY",
            filename=f"{item.get('row_key')}-H3权威音频.wav",
            managed_path=str(assets.authoritative_audio_path),
            external_ref={
                "batch_id": remote_item.get("batch_id"),
                "remote_item_id": remote_item.get("item_id"),
                "generation_version": 1,
            },
            metadata={
                **common_metadata,
                "subtitle_cues": [dict(value) for value in assets.raw_cues],
            },
            make_current=True,
        )
        subtitles: dict[str, Any] = {
            "source": "h3_generated_audio",
            "raw_cues": [dict(value) for value in assets.raw_cues],
            "render_cues": [dict(value) for value in assets.raw_cues],
            "bound_audio_asset_id": audio["asset_id"],
            "bound_video_asset_id": None,
            "style": item.get("subtitles", {}).get("style", {}),
            "status": "READY",
            "overflow_risk": False,
            "review_reason": None,
        }
        if self.caption_aligner is not None:
            try:
                subtitles["asr_alignment"] = self.caption_aligner.align(
                    assets.authoritative_audio_path,
                    script=str(item.get("script_text") or ""),
                    raw_cues=assets.raw_cues,
                    audio_asset_id=str(audio["asset_id"]),
                    audio_version=audio.get("version"),
                )
            except CaptionAlignmentError as exc:
                subtitles.update(
                    {
                        "status": "REVIEW_REQUIRED",
                        "review_reason": str(exc),
                        "asr_alignment": {
                            "status": "FAILED",
                            "reason_code": exc.code,
                            "reason_summary": str(exc)[:500],
                        },
                    }
                )
        elif self.require_precise_alignment:
            subtitles.update(
                {
                    "status": "REVIEW_REQUIRED",
                    "review_reason": "当前音频尚未配置本地 ASR 精确字幕服务",
                    "asr_alignment": {
                        "status": "FAILED",
                        "reason_code": "ASR_ALIGNMENT_REQUIRED",
                        "reason_summary": "当前音频尚未配置本地 ASR 精确字幕服务",
                    },
                }
            )
        base = self.store.add_asset(
            owner_user_id=owner_user_id,
            project_id=project_id,
            item_id=item_id,
            asset_type="base_video",
            source_type="h3",
            status="READY",
            filename=f"{item.get('row_key')}-H3静音底片.mp4",
            managed_path=str(assets.silent_base_video_path),
            metadata=common_metadata,
            make_current=True,
        )
        subtitles["bound_video_asset_id"] = base["asset_id"]
        self.store.set_item_subtitles(
            owner_user_id,
            project_id,
            item_id,
            subtitles,
        )

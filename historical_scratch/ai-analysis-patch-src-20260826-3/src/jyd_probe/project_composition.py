from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import hashlib
import json
import logging
from pathlib import Path
import threading
from typing import Any

from .auth_center import AuthCenterClient, AuthCenterError
from .draft_factory import probe_video_duration_us
from .logging_config import log_event
from .project_store import ProjectStore


REMOTE_COMPOSITION_ACTIVE = {
    "COMPOSITION_QUEUED",
    "DIGITAL_HUMAN_RUNNING",
    "VIDEO_ENHANCING",
    "VIDEO_MERGING",
}
logger = logging.getLogger("jyd_probe.workbench")


def _current_audio_item_links(
    links: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    newest_by_item: dict[str, dict[str, Any]] = {}
    for link in links:
        if (
            link.get("system") == "runninghub"
            and link.get("relation") == "digital_human_audio_item"
            and link.get("item_id")
        ):
            newest_by_item[str(link["item_id"])] = link
    return newest_by_item


def _has_saved_remote_video_segments(item: dict[str, Any]) -> bool:
    """Whether the row has evidence of reusable digital-human source output."""

    outputs = item.get("outputs", {})
    segments = outputs.get("original_video_segments", [])
    return isinstance(segments, list) and bool(segments)


def _composition_operations(project: dict[str, Any]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for operation in project.get("operations", []):
        if (
            operation.get("operation_type") == "COMPOSITION_GENERATE"
            and operation.get("item_id")
        ):
            latest[str(operation["item_id"])] = operation
    return latest


def _operation_was_accepted_by_cloud(operation: dict[str, Any]) -> bool:
    """Whether a local start crossed the cloud's immutable batch boundary."""

    if str(operation.get("status") or "") in {"RUNNING", "SUCCEEDED"}:
        return True
    result = operation.get("result", {})
    return isinstance(result, dict) and bool(
        str(result.get("remote_item_id") or "").strip()
    )


def _accepted_item_execution_contracts(
    project: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Recover each cloud-accepted remote item's immutable account snapshot."""

    contracts: dict[str, dict[str, Any]] = {}
    for operation in project.get("operations", []):
        if (
            operation.get("operation_type") != "COMPOSITION_GENERATE"
            or not _operation_was_accepted_by_cloud(operation)
        ):
            continue
        payload = operation.get("payload", {})
        if not isinstance(payload, dict):
            continue
        remote_item_id = str(payload.get("remote_item_id") or "").strip()
        account_ids = _normalized_execution_account_ids(
            payload.get("runninghub_execution_account_ids")
        )
        if not remote_item_id or account_ids is None:
            continue
        seedvr2_account_ids = _normalized_seedvr2_account_ids(
            payload.get("seedvr2_execution_account_ids")
        )
        execution_mode = (
            _normalized_execution_mode(payload.get("execution_mode"))
            or "same_account_v1"
        )
        candidate = {
            "runninghub_execution_account_ids": account_ids,
            "seedvr2_execution_account_ids": seedvr2_account_ids,
            "execution_mode": execution_mode,
        }
        previous = contracts.get(remote_item_id)
        if previous is not None and previous != candidate:
            raise ValueError(
                f"云端任务 {remote_item_id} 的本地已接收执行账号快照互相冲突"
            )
        contracts[remote_item_id] = candidate
    return contracts


def _is_precloud_start_failure(operation: dict[str, Any] | None) -> bool:
    """Whether a failed local operation never reached cloud composition start."""

    if not isinstance(operation, dict) or operation.get("status") != "FAILED":
        return False
    payload = operation.get("payload", {})
    return (
        isinstance(payload, dict)
        and str(payload.get("scope") or "base_video_only") == "base_video_only"
        and bool(str(payload.get("batch_id") or "").strip())
        and bool(str(payload.get("remote_item_id") or "").strip())
        and not _operation_was_accepted_by_cloud(operation)
    )


def _frozen_composition_image(
    operations: list[dict[str, Any]], remote_item_id: str
) -> dict[str, str]:
    """Return the first image snapshot that was accepted for a remote item."""

    fallback: dict[str, str] = {}
    for operation in operations:
        if operation.get("operation_type") != "COMPOSITION_GENERATE":
            continue
        payload = operation.get("payload", {})
        if not isinstance(payload, dict) or str(
            payload.get("remote_item_id") or ""
        ) != str(remote_item_id or ""):
            continue
        asset_id = str(payload.get("input_image_asset_id") or "")
        sha256 = str(payload.get("input_image_sha256") or "").strip().lower()
        if not asset_id or not sha256:
            continue
        candidate = {
            "input_image_asset_id": asset_id,
            "input_image_sha256": sha256,
        }
        if not fallback:
            fallback = candidate
        result = operation.get("result", {})
        if isinstance(result, dict) and str(
            result.get("remote_item_id") or ""
        ) == str(remote_item_id or ""):
            return candidate
    return fallback


def _normalized_execution_account_ids(
    value: list[int] | None,
) -> list[int] | None:
    if value is None:
        return None
    if (
        not value
        or any(type(account_id) is not int or account_id <= 0 for account_id in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("RunningHub 执行账号 ID 必须是非空且不重复的正整数列表")
    return sorted(value)


def _normalized_execution_mode(value: str | None) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    if clean not in {"same_account_v1", "dual_pool_v1"}:
        raise ValueError("RunningHub 执行模式不合法")
    return clean


def _normalized_seedvr2_account_ids(value: list[int] | None) -> list[int] | None:
    if value is None:
        return None
    if (
        not value
        or any(type(value_id) is not int or value_id <= 0 for value_id in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("SeedVR2 执行账号 ID 必须是非空且不重复的正整数列表")
    return sorted(value)


def _execution_modes_match(stored: object, requested: str | None) -> bool:
    """Treat pre-upgrade missing mode as the frozen same-account branch."""

    if stored == requested:
        return True
    return stored in {None, "same_account_v1"} and requested in {
        None,
        "same_account_v1",
    }


def _execution_runtime_fields(composition: dict[str, Any]) -> dict[str, Any]:
    """Copy only the cloud's safe mode and per-segment account summaries."""

    mode = _normalized_execution_mode(composition.get("execution_mode"))
    raw_assignments = composition.get("execution_assignments")
    assignments = raw_assignments if isinstance(raw_assignments, list) else []
    return {
        "execution_mode": mode,
        "execution_assignments": assignments,
    }


def _remote_failure_item_status(composition: dict[str, Any]) -> str:
    """Map a cloud failure back to the retry action for its actual stage."""

    return (
        "AUDIO_FAILED"
        if str(composition.get("failure_stage") or "") == "audio"
        else "COMPOSITION_FAILED"
    )


class ProjectCompositionStartDispatcher:
    """Bounded in-process executor backed by durable per-row operations.

    Tokens are kept only in submitted call arguments and are never persisted or
    logged.  On a process restart, ``STARTING`` claims are reset by the store;
    the next authenticated status poll submits those PENDING rows again with
    the same cloud idempotency keys.
    """

    def __init__(
        self,
        coordinator: "ProjectCompositionCoordinator",
        *,
        max_workers: int = 4,
    ) -> None:
        self.coordinator = coordinator
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="jyd-composition-start",
        )
        self._lock = threading.Lock()
        self._scheduled: set[str] = set()

    def submit(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
    ) -> int:
        project = self.coordinator.store.get_project(owner_user_id, project_id)
        pending = [
            operation
            for operation in project.get("operations", [])
            if operation.get("operation_type") == "COMPOSITION_GENERATE"
            and operation.get("status") == "PENDING"
        ]
        submitted = 0
        for operation in pending:
            operation_id = str(operation.get("operation_id") or "")
            if not operation_id:
                continue
            with self._lock:
                if operation_id in self._scheduled:
                    continue
                self._scheduled.add(operation_id)
            future = self._executor.submit(
                self.coordinator.start_pending_operation,
                owner_user_id,
                project_id,
                operation_id,
                token,
            )
            future.add_done_callback(
                lambda completed, saved_id=operation_id: self._completed(
                    saved_id, completed
                )
            )
            submitted += 1
        if submitted:
            log_event(
                logger,
                "workbench.composition_start_batch_scheduled",
                "4A 逐行启动任务已进入后台协调队列",
                component="workbench",
                user_id=owner_user_id,
                project_id=project_id,
                item_count=submitted,
            )
        return submitted

    def _completed(self, operation_id: str, future: Future[None]) -> None:
        with self._lock:
            self._scheduled.discard(operation_id)
        try:
            future.result()
        except Exception:
            logger.exception(
                "4A 后台启动任务发生未处理异常 operation_id=%s", operation_id
            )

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class ProjectCompositionCoordinator:
    """Bridge workbench projects to the existing digital-human workers.

    Module 4A deliberately stops at a normalized base video. Provider segment
    outputs are retained as immutable source assets; captions, BGM, variants,
    Jianying drafts, and publishing are not part of this coordinator.
    """

    def __init__(
        self,
        store: ProjectStore,
        client: AuthCenterClient,
        *,
        storage_root: Path,
        max_video_bytes: int,
    ) -> None:
        self.store = store
        self.client = client
        self.storage_root = Path(storage_root).resolve()
        self.max_video_bytes = int(max_video_bytes)

    def start(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        *,
        idempotency_key: str,
        resolution: str = "1024",
        runninghub_execution_account_ids: list[int] | None = None,
        seedvr2_execution_account_ids: list[int] | None = None,
        execution_mode: str | None = None,
        item_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        clean_key = str(idempotency_key or "").strip()
        if not clean_key:
            raise ValueError("画面生成请求缺少幂等键")
        clean_resolution = str(resolution or "1024").strip()
        try:
            if int(clean_resolution) <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("数字人最长边分辨率必须是正整数") from exc

        if item_ids is not None:
            requested_ids = [str(value or "").strip() for value in item_ids]
            if (
                not requested_ids
                or any(not value for value in requested_ids)
                or len(set(requested_ids)) != len(requested_ids)
            ):
                raise ValueError("单条画面生成必须指定非空且不重复的脚本行 ID")
            by_id = {str(item["item_id"]): item for item in project["items"]}
            missing = [value for value in requested_ids if value not in by_id]
            if missing:
                raise KeyError("项目脚本行不存在")
            target_items = [by_id[value] for value in requested_ids]
            latest_operations = _composition_operations(project)
            blocked = [
                item for item in target_items
                if item.get("outputs", {}).get("base_video") is None
                and not item.get("allowed_actions", {}).get("start_composition")
                and not (
                    item.get("allowed_actions", {}).get("retry_composition")
                    and _is_precloud_start_failure(
                        latest_operations.get(str(item["item_id"]))
                    )
                )
            ]
            if blocked:
                raise ValueError(f"任务 {blocked[0]['row_key']} 尚未准备好生成画面")
            # Explicit row requests reuse a current base video.  Postprocess
            # can still run independently if only subtitle/BGM changed.
            target_items = [
                item for item in target_items
                if item.get("outputs", {}).get("base_video") is None
            ]
            if not target_items:
                return project
        else:
            if not project["allowed_actions"]["start_composition"]:
                raise ValueError("当前项目状态不能开始画面生成")
            target_items = project["items"]

        selected_account_ids = _normalized_execution_account_ids(
            runninghub_execution_account_ids
        )
        selected_seedvr2_account_ids = _normalized_seedvr2_account_ids(
            seedvr2_execution_account_ids
        )
        selected_execution_mode = _normalized_execution_mode(execution_mode)
        if selected_execution_mode == "dual_pool_v1" and (
            selected_account_ids is None or selected_seedvr2_account_ids is None
        ):
            raise ValueError("双资源池模式必须分别选择数字人和 SeedVR2 执行账号")
        if (
            selected_execution_mode == "same_account_v1"
            and selected_seedvr2_account_ids is not None
        ):
            raise ValueError("同账号模式不能提交独立 SeedVR2 执行账号")
        links_by_item = _current_audio_item_links(project["links"])
        accepted_item_contracts = _accepted_item_execution_contracts(project)

        for item in target_items:
            backfill_seedvr2 = _has_saved_remote_video_segments(item) and (
                item.get("settings", {}).get("composition_invalidated_reason")
                == "DIGITAL_HUMAN_RESOLUTION_CHANGED"
            )
            link = links_by_item.get(str(item["item_id"]))
            if link is None:
                raise ValueError(f"任务 {item['row_key']} 缺少数字人声音任务关联")
            batch_id = str(link.get("metadata", {}).get("batch_id") or "")
            correlation_id = str(
                link.get("metadata", {}).get("correlation_id") or ""
            ).strip()
            remote_item_id = str(link.get("external_id") or "")
            if not batch_id or not remote_item_id:
                raise ValueError(f"任务 {item['row_key']} 的数字人任务关联不完整")
            locked_contract = accepted_item_contracts.get(remote_item_id)
            effective_account_ids = (
                locked_contract["runninghub_execution_account_ids"]
                if locked_contract is not None
                else selected_account_ids
            )
            effective_seedvr2_account_ids = (
                locked_contract["seedvr2_execution_account_ids"]
                if locked_contract is not None
                else selected_seedvr2_account_ids
            )
            effective_execution_mode = (
                locked_contract["execution_mode"]
                if locked_contract is not None
                else selected_execution_mode
            )
            image: dict[str, Any] = {}
            image_path: Path | None = None
            image_sha256 = ""
            if not backfill_seedvr2:
                raw_image = item.get("inputs", {}).get("image")
                if (
                    not isinstance(raw_image, dict)
                    or not raw_image.get("managed_path")
                ):
                    raise ValueError(f"任务 {item['row_key']} 尚未分配图片")
                image = raw_image
                image_path = Path(str(image["managed_path"])).resolve()
                if not image_path.is_file():
                    raise ValueError(f"任务 {item['row_key']} 的图片文件不存在")
                image_sha256 = str(
                    image.get("metadata", {}).get("sha256") or ""
                ).strip().lower()
                if not image_sha256:
                    image_sha256 = hashlib.sha256(
                        image_path.read_bytes()
                    ).hexdigest()
            operation_payload = {
                "batch_id": batch_id,
                "remote_item_id": remote_item_id,
                "scope": (
                    "seedvr2_backfill_only"
                    if backfill_seedvr2
                    else "base_video_only"
                ),
                "resolution": clean_resolution,
                "runninghub_execution_account_ids": effective_account_ids,
                "seedvr2_execution_account_ids": effective_seedvr2_account_ids,
                "execution_mode": effective_execution_mode,
            }
            if not backfill_seedvr2:
                operation_payload.update(
                    {
                        "input_image_asset_id": str(image.get("asset_id") or ""),
                        "input_image_sha256": image_sha256,
                    }
                )
            for existing in project.get("operations", []):
                existing_payload = existing.get("payload", {})
                if (
                    existing.get("operation_type") == "COMPOSITION_GENERATE"
                    and existing.get("idempotency_key") == clean_key
                    and (
                        existing_payload.get("runninghub_execution_account_ids")
                        != effective_account_ids
                        or existing_payload.get("seedvr2_execution_account_ids")
                        != effective_seedvr2_account_ids
                        or not _execution_modes_match(
                            existing_payload.get("execution_mode"),
                            effective_execution_mode,
                        )
                        or str(existing_payload.get("resolution") or "1024")
                        != clean_resolution
                    )
                ):
                    raise ValueError(
                        "该画面生成操作的执行账号或分辨率快照已锁定，不能修改"
                    )
            operation = self.store.create_operation(
                owner_user_id=owner_user_id,
                project_id=project_id,
                item_id=item["item_id"],
                operation_type="COMPOSITION_GENERATE",
                idempotency_key=clean_key,
                payload=operation_payload,
                correlation_id=correlation_id or None,
            )
            if operation.get("payload", {}).get(
                "runninghub_execution_account_ids"
            ) != effective_account_ids:
                raise ValueError(
                    "该画面生成操作的 RunningHub 执行账号快照已锁定，不能修改"
                )
            if operation.get("payload", {}).get(
                "seedvr2_execution_account_ids"
            ) != effective_seedvr2_account_ids:
                raise ValueError(
                    "该画面生成操作的 SeedVR2 执行账号快照已锁定，不能修改"
                )
            if not _execution_modes_match(
                operation.get("payload", {}).get("execution_mode"),
                effective_execution_mode,
            ):
                raise ValueError(
                    "该画面生成操作的 RunningHub 执行模式快照已锁定，不能修改"
                )
            if (
                not backfill_seedvr2
                and operation.get("payload", {}).get("input_image_sha256")
                != image_sha256
            ):
                raise ValueError("同一画面生成幂等键不能用于不同的项目图片")
            if str(operation.get("payload", {}).get("resolution") or "1024") != clean_resolution:
                raise ValueError("同一画面生成幂等键不能用于不同的分辨率")
        # Network uploads and paid cloud starts are deliberately not performed
        # in this request.  The durable PENDING rows are drained by the bounded
        # background dispatcher after the HTTP response has been produced.
        return self.store.get_project(owner_user_id, project_id)

    def start_pending_operation(
        self,
        owner_user_id: str,
        project_id: str,
        operation_id: str,
        token: str,
    ) -> None:
        operation = self.store.claim_pending_operation(
            owner_user_id,
            project_id,
            operation_id,
            operation_type="COMPOSITION_GENERATE",
        )
        if operation is None:
            return
        item_id = str(operation.get("item_id") or "")
        payload = operation.get("payload", {})
        try:
            if not item_id or not isinstance(payload, dict):
                raise ValueError("画面启动操作快照不完整")
            project = self.store.get_project(owner_user_id, project_id)
            item = next(
                (value for value in project["items"] if value["item_id"] == item_id),
                None,
            )
            if item is None:
                raise ValueError("画面启动操作对应的脚本行不存在")
            batch_id = str(payload.get("batch_id") or "")
            remote_item_id = str(payload.get("remote_item_id") or "")
            resolution = str(payload.get("resolution") or "1024")
            selected_account_ids = _normalized_execution_account_ids(
                payload.get("runninghub_execution_account_ids")
            )
            selected_seedvr2_account_ids = _normalized_seedvr2_account_ids(
                payload.get("seedvr2_execution_account_ids")
            )
            selected_execution_mode = _normalized_execution_mode(
                payload.get("execution_mode")
            )
            if not batch_id or not remote_item_id:
                raise ValueError("画面启动操作缺少云端声音任务关联")
            scope = str(payload.get("scope") or "base_video_only")
            backfill_seedvr2 = scope == "seedvr2_backfill_only"
            staged_image: dict[str, Any] = {}
            image_sha256 = ""
            if backfill_seedvr2:
                remote = self.client.backfill_workbench_video_enhancement(
                    token,
                    remote_item_id,
                    idempotency_key=f"{operation['idempotency_key']}:{item_id}",
                )
            else:
                image_asset_id = str(payload.get("input_image_asset_id") or "")
                image_sha256 = str(payload.get("input_image_sha256") or "").lower()
                images = item.get("asset_history", {}).get("input_image", [])
                image = next(
                    (
                        value
                        for value in images
                        if str(value.get("asset_id") or "") == image_asset_id
                    ),
                    None,
                )
                if image is None or not image_sha256:
                    raise ValueError("画面启动操作绑定的图片版本不存在")
                image_path = Path(str(image.get("managed_path") or "")).resolve()
                if not image_path.is_file():
                    raise ValueError("画面启动操作绑定的图片文件不存在")
                if self.storage_root not in image_path.parents:
                    raise ValueError("画面启动操作绑定了非托管图片")
                actual_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
                if actual_sha256 != image_sha256:
                    raise ValueError("画面启动操作绑定的图片内容已变化")
                staged_image = self.client.upload_workbench_batch_asset(
                    token,
                    image_path,
                    kind="image",
                    filename=str(image.get("filename") or image_path.name),
                )
                remote = self.client.start_workbench_composition(
                    token,
                    batch_id,
                    remote_item_id,
                    idempotency_key=f"{operation['idempotency_key']}:{item_id}",
                    image_asset_id=str(staged_image.get("asset_id") or ""),
                    image_sha256=image_sha256,
                    resolution=resolution,
                    correlation_id=str(operation.get("correlation_id") or ""),
                    runninghub_execution_account_ids=selected_account_ids,
                    seedvr2_execution_account_ids=selected_seedvr2_account_ids,
                )
            composition = remote.get("composition", {})
            authoritative_mode = _normalized_execution_mode(
                composition.get("execution_mode")
            )
            if (
                selected_execution_mode is not None
                and authoritative_mode is not None
                and selected_execution_mode != authoritative_mode
            ):
                raise ValueError(
                    "云端锁定的 RunningHub 执行模式与本地费用确认快照不一致"
                )
            remote_status = str(
                composition.get("status") or "COMPOSITION_QUEUED"
            )
            if remote_status == "COMPOSITION_FAILED":
                operation_status = "FAILED"
                item_status = _remote_failure_item_status(composition)
            elif remote_status in REMOTE_COMPOSITION_ACTIVE:
                operation_status = "RUNNING"
                item_status = remote_status
            elif remote_status == "BASE_VIDEO_READY":
                # Keep the durable operation active until the normal sync path
                # downloads and verifies the cloud result.
                operation_status = "RUNNING"
                item_status = "VIDEO_MERGING"
            else:
                raise ValueError(f"云端返回了未知的画面生成状态：{remote_status}")
            self.store.transition_operation(
                owner_user_id,
                project_id,
                item_id,
                operation_id=operation_id,
                operation_type="COMPOSITION_GENERATE",
                status=operation_status,
                item_status=item_status,
                result={
                    "batch_id": batch_id,
                    "remote_item_id": remote_item_id,
                    "remote_status": remote_status,
                    "operation_id": operation_id,
                    "image_asset_id": staged_image.get("asset_id"),
                    "input_image_sha256": image_sha256,
                    "seedvr2_backfill_only": backfill_seedvr2,
                    "runninghub_execution_account_ids": selected_account_ids,
                    "seedvr2_execution_account_ids": selected_seedvr2_account_ids,
                    "execution_mode": authoritative_mode or selected_execution_mode,
                    "execution_assignments": (
                        composition.get("execution_assignments")
                        if isinstance(composition.get("execution_assignments"), list)
                        else []
                    ),
                },
                error_code=(
                    str(composition.get("error_code") or "REMOTE_COMPOSITION_FAILED")
                    if operation_status == "FAILED"
                    else None
                ),
                error_message=(
                    str(composition.get("error_message") or "画面生成前置任务失败")
                    if operation_status == "FAILED"
                    else None
                ),
            )
        except AuthCenterError as exc:
            retryable = exc.retryable
            self.store.transition_operation(
                owner_user_id,
                project_id,
                item_id,
                operation_id=operation_id,
                operation_type="COMPOSITION_GENERATE",
                status="PENDING" if retryable else "FAILED",
                item_status=(
                    "COMPOSITION_QUEUED" if retryable else "COMPOSITION_FAILED"
                ),
                error_code=exc.error_code,
                error_message=str(exc),
            )
        except Exception as exc:
            self.store.transition_operation(
                owner_user_id,
                project_id,
                item_id,
                operation_id=operation_id,
                operation_type="COMPOSITION_GENERATE",
                status="FAILED",
                item_status="COMPOSITION_FAILED",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )

    def sync(
        self, owner_user_id: str, project_id: str, token: str
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        links_by_item = _current_audio_item_links(project["links"])
        operations = _composition_operations(project)
        for item in project["items"]:
            item_id = str(item["item_id"])
            link = links_by_item.get(item_id)
            operation = operations.get(item_id)
            if link is None or operation is None:
                continue
            # Terminal 4A operations are immutable history. Re-querying every
            # completed row on each browser poll multiplies cloud requests and
            # lets one stale row abort the status refresh for the active row.
            if operation.get("status") != "RUNNING":
                continue
            remote_item_id = str(link.get("external_id") or "")
            if not remote_item_id:
                continue
            remote = self.client.get_workbench_task(token, remote_item_id)
            composition = remote.get("composition", {})
            if not isinstance(composition, dict):
                composition = {}
            remote_status = str(composition.get("status") or "")
            expected_image_sha256 = str(
                operation.get("payload", {}).get("input_image_sha256") or ""
            ).strip().lower()
            remote_image_sha256 = str(
                composition.get("image_sha256") or ""
            ).strip().lower()
            if expected_image_sha256 and remote_image_sha256 != expected_image_sha256:
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item_id,
                    operation_id=operation.get("operation_id"),
                    operation_type="COMPOSITION_GENERATE",
                    status="FAILED",
                    item_status="COMPOSITION_FAILED",
                    result={
                        "remote_item_id": remote_item_id,
                        "remote_status": remote_status,
                        "expected_image_sha256": expected_image_sha256,
                        "remote_image_sha256": remote_image_sha256 or None,
                    },
                    error_code="REMOTE_IMAGE_VERSION_MISMATCH",
                    error_message="云端返回的视频未绑定当前项目图片，请更新云端服务后重试",
                )
                continue

            project = self.store.get_project(owner_user_id, project_id)
            local_item = next(
                value for value in project["items"] if value["item_id"] == item_id
            )
            self._download_ready_segments(
                owner_user_id,
                project_id,
                local_item,
                remote_item_id,
                remote,
                token,
            )

            if remote_status == "BASE_VIDEO_READY":
                project = self.store.get_project(owner_user_id, project_id)
                local_item = next(
                    value for value in project["items"] if value["item_id"] == item_id
                )
                self._download_base_video(
                    owner_user_id,
                    project_id,
                    local_item,
                    remote_item_id,
                    remote,
                    token,
                    input_image_asset_id=str(
                        operation.get("payload", {}).get("input_image_asset_id")
                        or ""
                    ),
                    input_image_sha256=expected_image_sha256,
                )
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item_id,
                    operation_id=operation.get("operation_id"),
                    operation_type="COMPOSITION_GENERATE",
                    status="SUCCEEDED",
                    item_status="BASE_VIDEO_READY",
                    result={
                        "remote_item_id": remote_item_id,
                        "remote_status": remote_status,
                        "segment_count": int(composition.get("segment_count") or 0),
                        **_execution_runtime_fields(composition),
                    },
                )
            elif remote_status == "COMPOSITION_FAILED":
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item_id,
                    operation_type="COMPOSITION_GENERATE",
                    status="FAILED",
                    item_status=_remote_failure_item_status(composition),
                    result={
                        "remote_item_id": remote_item_id,
                        "remote_status": remote_status,
                        **_execution_runtime_fields(composition),
                    },
                    error_code=str(
                        composition.get("error_code")
                        or "REMOTE_COMPOSITION_FAILED"
                    ),
                    error_message=str(
                        composition.get("error_message") or "数字人画面生成失败"
                    ),
                )
            elif remote_status in REMOTE_COMPOSITION_ACTIVE:
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item_id,
                    operation_type="COMPOSITION_GENERATE",
                    status="RUNNING",
                    item_status=remote_status,
                    result={
                        "remote_item_id": remote_item_id,
                        "remote_status": remote_status,
                        "segment_count": int(composition.get("segment_count") or 0),
                        **_execution_runtime_fields(composition),
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
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        item = next(
            (value for value in project["items"] if value["item_id"] == item_id),
            None,
        )
        if item is None:
            raise KeyError("项目脚本行不存在")
        if not item["allowed_actions"]["retry_composition"]:
            raise ValueError("当前画面任务没有可重试的失败阶段")
        link = _current_audio_item_links(project["links"]).get(item_id)
        if link is None:
            raise ValueError("当前脚本行没有数字人任务")
        clean_key = str(idempotency_key or "").strip()
        if not clean_key:
            raise ValueError("画面重试请求缺少幂等键")
        latest_operation = _composition_operations(project).get(item_id)
        if _is_precloud_start_failure(latest_operation):
            original_payload = dict(latest_operation.get("payload", {}))
            original_image_sha256 = str(
                original_payload.get("input_image_sha256") or ""
            ).strip().lower()
            current_image_sha256 = str(
                item.get("inputs", {})
                .get("image", {})
                .get("metadata", {})
                .get("sha256")
                or ""
            ).strip().lower()
            if (
                original_image_sha256
                and current_image_sha256 != original_image_sha256
            ):
                raise ValueError(
                    "当前人物图与失败启动时冻结图片不一致，不能重放旧启动请求"
                )
            remote_item_id = str(
                original_payload.get("remote_item_id") or ""
            ).strip()
            locked_contract = _accepted_item_execution_contracts(project).get(
                remote_item_id
            )
            if locked_contract is not None:
                original_payload.update(locked_contract)
            original_payload["retry"] = True
            original_payload["retry_of_operation_id"] = str(
                latest_operation.get("operation_id") or ""
            )
            self.store.create_operation(
                owner_user_id=owner_user_id,
                project_id=project_id,
                item_id=item_id,
                operation_type="COMPOSITION_GENERATE",
                idempotency_key=clean_key,
                payload=original_payload,
                correlation_id=(
                    str(link.get("metadata", {}).get("correlation_id") or "").strip()
                    or None
                ),
            )
            # The normal authenticated status poll drains this durable PENDING
            # operation through start_pending_operation.  Calling the cloud
            # retry endpoint here would be invalid because no remote 4A start
            # was accepted for the failed local operation.
            return self.store.get_project(owner_user_id, project_id)
        resolution_invalidated = (
            item.get("settings", {}).get("composition_invalidated_reason")
            == "DIGITAL_HUMAN_RESOLUTION_CHANGED"
        )
        current_resolution = str(
            project.get("settings", {})
            .get("digital_human", {})
            .get("resolution")
            or "1024"
        ).strip()
        seedvr2_backfill = resolution_invalidated and _has_saved_remote_video_segments(
            item
        )
        if resolution_invalidated and not seedvr2_backfill:
            # No digital-human source exists to enhance (for example, the 4A
            # command was cancelled in RunningHub). Retry through the normal
            # composition-start contract so the current image is uploaded and
            # the cloud can rebuild a fresh 4A command from approved audio.
            return self.start(
                owner_user_id,
                project_id,
                token,
                idempotency_key=clean_key,
                resolution=current_resolution,
                item_ids=[item_id],
            )
        frozen_image = _frozen_composition_image(
            project.get("operations", []), str(link["external_id"])
        )
        if not seedvr2_backfill and frozen_image:
            current_image_sha256 = str(
                item.get("inputs", {})
                .get("image", {})
                .get("metadata", {})
                .get("sha256")
                or ""
            ).strip().lower()
            if current_image_sha256 != frozen_image["input_image_sha256"]:
                raise ValueError(
                    "当前人物图与原付费任务冻结图片不一致，不能重试旧任务"
                )
        retry_payload = {
            "retry": True,
            "remote_item_id": link["external_id"],
            "resolution": current_resolution,
            "scope": (
                "seedvr2_backfill_only"
                if seedvr2_backfill
                else "failed_remote_stage"
            ),
        }
        if not seedvr2_backfill and frozen_image:
            retry_payload.update(frozen_image)
        operation = self.store.create_operation(
            owner_user_id=owner_user_id,
            project_id=project_id,
            item_id=item_id,
            operation_type="COMPOSITION_GENERATE",
            idempotency_key=clean_key,
            payload=retry_payload,
        )
        if operation.get("status") == "PENDING":
            try:
                if seedvr2_backfill:
                    self.client.backfill_workbench_video_enhancement(
                        token,
                        str(link["external_id"]),
                        idempotency_key=f"{clean_key}:{item_id}",
                    )
                else:
                    self.client.retry_workbench_composition(
                        token,
                        str(link["external_id"]),
                        resolution=current_resolution,
                    )
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item_id,
                    operation_type="COMPOSITION_GENERATE",
                    status="RUNNING",
                    item_status="COMPOSITION_QUEUED",
                    result={
                        "remote_item_id": link["external_id"],
                        "retry": True,
                        "seedvr2_backfill_only": seedvr2_backfill,
                        "resolution": current_resolution,
                    },
                )
            except Exception as exc:
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item_id,
                    operation_type="COMPOSITION_GENERATE",
                    status="FAILED",
                    item_status="COMPOSITION_FAILED",
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )
                raise
        return self.sync(owner_user_id, project_id, token)

    def backfill_seedvr2(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        token: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        item = next(
            (value for value in project["items"] if value["item_id"] == item_id),
            None,
        )
        if item is None:
            raise KeyError("项目脚本行不存在")
        if not item.get("allowed_actions", {}).get("backfill_seedvr2"):
            raise ValueError("当前画面已经高清化、正在处理，或没有可复用的数字人分段")
        link = _current_audio_item_links(project["links"]).get(item_id)
        if link is None or not str(link.get("external_id") or "").strip():
            raise ValueError("当前脚本行没有可复用的云端数字人任务")
        clean_key = str(idempotency_key or "").strip()
        if not clean_key:
            raise ValueError("SeedVR2 补跑请求缺少幂等键")
        remote_item_id = str(link["external_id"])
        operation = self.store.create_operation(
            owner_user_id=owner_user_id,
            project_id=project_id,
            item_id=item_id,
            operation_type="COMPOSITION_GENERATE",
            idempotency_key=clean_key,
            payload={
                "remote_item_id": remote_item_id,
                "scope": "seedvr2_backfill_only",
                "reuse_paid_digital_human": True,
            },
        )
        if operation.get("status") == "PENDING":
            try:
                remote = self.client.backfill_workbench_video_enhancement(
                    token,
                    remote_item_id,
                    idempotency_key=f"{clean_key}:{item_id}",
                )
                composition = remote.get("composition", {})
                remote_status = str(
                    composition.get("status") or "VIDEO_ENHANCING"
                )
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item_id,
                    operation_type="COMPOSITION_GENERATE",
                    status="RUNNING",
                    item_status=(
                        remote_status
                        if remote_status in REMOTE_COMPOSITION_ACTIVE
                        else "VIDEO_ENHANCING"
                    ),
                    result={
                        "remote_item_id": remote_item_id,
                        "remote_status": remote_status,
                        "operation_id": operation["operation_id"],
                        "seedvr2_backfill_only": True,
                        "reuse_paid_digital_human": True,
                    },
                )
            except Exception as exc:
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item_id,
                    operation_type="COMPOSITION_GENERATE",
                    status="FAILED",
                    item_status="COMPOSITION_FAILED",
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )
                raise
        return self.sync(owner_user_id, project_id, token)

    def _download_ready_segments(
        self,
        owner_user_id: str,
        project_id: str,
        item: dict[str, Any],
        remote_item_id: str,
        remote: dict[str, Any],
        token: str,
    ) -> None:
        existing_variants = {
            (
                str(asset.get("external_ref", {}).get("remote_task_id") or ""),
                str(
                    asset.get("external_ref", {}).get("quality_variant")
                    or asset.get("metadata", {}).get("quality_variant")
                    or ""
                ),
            )
            for asset in item.get("asset_history", {}).get(
                "original_video_segment", []
            )
        }
        videos = remote.get("source", {}).get("videos", [])
        if not isinstance(videos, list):
            return
        for video in videos:
            if not isinstance(video, dict) or str(video.get("status") or "") != "SUCCESS":
                continue
            task_id = str(video.get("task_id") or "")
            quality_variant = str(video.get("quality_variant") or "")
            index = int(video.get("index") or 0)
            if (
                not task_id
                or index < 1
                or (task_id, quality_variant) in existing_variants
            ):
                continue
            directory = (
                self.storage_root
                / "projects"
                / str(owner_user_id)
                / project_id
                / str(item["item_id"])
                / "composition"
                / remote_item_id
                / "segments"
            )
            task_suffix = hashlib.sha256(
                f"{task_id}:{quality_variant}".encode("utf-8")
            ).hexdigest()[:12]
            target = directory / f"segment-{index:03d}-{task_suffix}.mp4"
            temporary = target.with_suffix(".mp4.tmp")
            try:
                self.client.download_workbench_video(
                    token,
                    remote_item_id,
                    index,
                    temporary,
                    max_bytes=self.max_video_bytes,
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary.replace(target)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            actual_duration_us = probe_video_duration_us(target)
            self.store.add_asset(
                owner_user_id=owner_user_id,
                project_id=project_id,
                item_id=item["item_id"],
                asset_type="original_video_segment",
                source_type="runninghub",
                status="READY",
                filename=f"{item['row_key']}-segment-{index:03d}.mp4",
                managed_path=str(target),
                external_ref={
                    "remote_item_id": remote_item_id,
                    "remote_task_id": task_id,
                    "video_index": index,
                    "quality_variant": quality_variant or None,
                },
                metadata={
                    "start_seconds": video.get("start_seconds"),
                    "end_seconds": video.get("end_seconds"),
                    "actual_duration_us": actual_duration_us,
                    "speech_duration_seconds": video.get("speech_duration_seconds"),
                    "generation_tail_seconds": video.get("generation_tail_seconds"),
                    "script_text": video.get("script_text"),
                    "quality_variant": video.get("quality_variant"),
                    "enhanced_by": (
                        "runninghub_seedvr2"
                        if video.get("quality_variant") == "seedvr2_upscaled"
                        else None
                    ),
                    "source_is_available_on_cloud": bool(
                        video.get("source_download_url")
                    ),
                },
            )
            existing_variants.add((task_id, quality_variant))

    def _download_base_video(
        self,
        owner_user_id: str,
        project_id: str,
        item: dict[str, Any],
        remote_item_id: str,
        remote: dict[str, Any],
        token: str,
        *,
        input_image_asset_id: str = "",
        input_image_sha256: str = "",
    ) -> None:
        videos = remote.get("source", {}).get("videos", [])
        task_ids = [
            str(video.get("task_id") or "")
            for video in videos
            if isinstance(video, dict) and video.get("task_id")
        ] if isinstance(videos, list) else []
        source_quality_variants = [
            str(video.get("quality_variant") or "")
            for video in videos
            if isinstance(video, dict) and video.get("task_id")
        ] if isinstance(videos, list) else []
        quality_variant = str(
            remote.get("composition", {}).get("quality_variant") or ""
        )
        if (
            not quality_variant
            and source_quality_variants
            and len(set(source_quality_variants)) == 1
        ):
            quality_variant = source_quality_variants[0]
        duration_us = max(
            (
                round(float(video.get("end_seconds")) * 1_000_000)
                for video in videos
                if isinstance(video, dict)
                and isinstance(video.get("end_seconds"), (int, float))
                and not isinstance(video.get("end_seconds"), bool)
                and float(video["end_seconds"]) > 0
            ),
            default=0,
        )
        signature = {
            "remote_item_id": remote_item_id,
            "source_task_ids": task_ids,
            "source_quality_variants": source_quality_variants,
            "quality_variant": quality_variant or None,
            "remote_updated_at": str(remote.get("updated_at") or ""),
            "image_sha256": str(
                remote.get("composition", {}).get("image_sha256") or ""
            ),
        }
        current = item.get("outputs", {}).get("base_video")
        if isinstance(current, dict) and current.get("external_ref") == signature:
            return
        directory = (
            self.storage_root
            / "projects"
            / str(owner_user_id)
            / project_id
            / str(item["item_id"])
            / "composition"
            / remote_item_id
            / "base"
        )
        signature_hash = hashlib.sha256(
            json.dumps(signature, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]
        target = directory / f"base-{signature_hash}.mp4"
        temporary = target.with_suffix(".mp4.tmp")
        try:
            self.client.download_workbench_base_video(
                token,
                remote_item_id,
                temporary,
                max_bytes=self.max_video_bytes,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.replace(target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        planned_duration_us = duration_us
        try:
            duration_us = probe_video_duration_us(target)
        except Exception as exc:
            duration_us = planned_duration_us
            logger.warning(
                "基础视频实际时长读取失败，将使用云端计划时长: error_type=%s",
                type(exc).__name__,
            )
        self.store.add_asset(
            owner_user_id=owner_user_id,
            project_id=project_id,
            item_id=item["item_id"],
            asset_type="base_video",
            source_type=(
                "runninghub_single" if len(task_ids) <= 1 else "runninghub_merge"
            ),
            status="READY",
            filename=f"{item['row_key']}-base.mp4",
            managed_path=str(target),
            external_ref=signature,
            metadata={
                "segment_count": len(task_ids),
                "duration_us": duration_us or None,
                "planned_duration_us": planned_duration_us or None,
                "normalized_to_approved_audio": True,
                "input_image_asset_id": input_image_asset_id
                or str(item.get("inputs", {}).get("image", {}).get("asset_id") or ""),
                "input_image_sha256": input_image_sha256
                or signature["image_sha256"],
                "module": "4A",
                "quality_variant": quality_variant or None,
                "enhanced_by": (
                    "runninghub_seedvr2"
                    if quality_variant == "seedvr2_upscaled"
                    else None
                ),
            },
            make_current=True,
        )

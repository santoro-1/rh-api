from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import logging
import time
from typing import Any, Mapping

from .auth_center import AuthCenterClient, AuthCenterError
from .logging_config import log_event
from .project_music import ProjectMusicSelector, automatic_music_identity_counts
from .project_postprocess import (
    GENERATED_TITLE_MAX_LINE_2_CHARS,
    normalize_cover_title,
)
from .project_store import ProjectStore
from .semantic_subtitles import SemanticSubtitleMappingError
from .semantic_visuals import SemanticVisualCatalog
from .unified_visual_plan import (
    UnifiedVisualInput,
    build_local_visual_result,
    candidate_set_sha256,
    empty_visual_recipe,
    prepare_unified_visual_input,
    remap_saved_visual_plan,
    validate_remote_visual_plan,
)


CONTENT_ANALYSIS_BATCH_CONCURRENCY = 10
analysis_logger = logging.getLogger(__name__)
_UNCHANGED_SCRIPT_STATUSES = {"PENDING", "SUCCESS", "PARTIAL", "FAILED"}
_BRANCH_STATUSES = {"SUCCESS", "FAILED"}


def _script_sha256(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def _safe_error(error: object) -> dict[str, str]:
    if isinstance(error, AuthCenterError):
        code = f"DIGITAL_HUMAN_HTTP_{error.status_code}"
    else:
        code = type(error).__name__.upper()[:100] or "CONTENT_ANALYSIS_FAILED"
    summary = str(error).strip() or "内容分析请求失败"
    return {"code": code, "summary": summary[:500]}


def _is_visual_context_contract_rejection(error: BaseException) -> bool:
    return (
        isinstance(error, AuthCenterError)
        and error.status_code == 400
        and "visual_context" in str(error)
        and "统一分析契约" in str(error)
    )


def _legacy_content_visual_context(
    visual_context: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Build the safe subset accepted by the pre-enrichment cloud contract.

    A rolling local/cloud upgrade must never reinterpret enrichment B-roll as
    an explicit semantic hit.  Legacy requests therefore retain only explicit
    anchors, remove the new ``usage`` field, and include just their concepts.
    """

    anchors: list[dict[str, Any]] = []
    referenced_concepts: set[str] = set()
    for raw in visual_context.get("anchors", []):
        if not isinstance(raw, Mapping) or str(raw.get("usage") or "explicit") != "explicit":
            continue
        anchor = {str(key): value for key, value in raw.items() if key != "usage"}
        char_start = int(anchor.get("char_start") or 0)
        anchor["anchor_id"] = "START" if char_start == 0 else f"B{char_start}"
        allowed = anchor.get("allowed_concepts")
        if not isinstance(allowed, list) or not allowed:
            continue
        referenced_concepts.update(str(value) for value in allowed)
        anchors.append(anchor)
    if not anchors:
        return None
    concepts = [
        dict(raw)
        for raw in visual_context.get("concepts", [])
        if isinstance(raw, Mapping)
        and str(raw.get("concept_id") or "") in referenced_concepts
    ]
    return {
        "catalog_version": str(visual_context.get("catalog_version") or "none"),
        "concepts": concepts,
        "anchors": anchors,
    }


def _analyze_with_visual_context_compat(
    client: AuthCenterClient,
    token: str,
    original_script: str,
    *,
    force_refresh: bool,
    visual_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"force_refresh": force_refresh}
    if visual_context is not None:
        kwargs["visual_context"] = dict(visual_context)
    try:
        return client.analyze_workbench_content(token, original_script, **kwargs)
    except AuthCenterError as exc:
        if visual_context is None or not _is_visual_context_contract_rejection(exc):
            raise

    legacy_context = _legacy_content_visual_context(visual_context)
    legacy_kwargs: dict[str, Any] = {"force_refresh": force_refresh}
    if legacy_context is not None:
        legacy_kwargs["visual_context"] = legacy_context
    try:
        result = client.analyze_workbench_content(
            token, original_script, **legacy_kwargs
        )
        result["_workbench_visual_context_mode"] = (
            "legacy_explicit" if legacy_context is not None else "content_only"
        )
        return result
    except AuthCenterError as exc:
        if legacy_context is None or not _is_visual_context_contract_rejection(exc):
            raise

    result = client.analyze_workbench_content(
        token, original_script, force_refresh=force_refresh
    )
    result["_workbench_visual_context_mode"] = "content_only"
    return result


def _branch_error(payload: Mapping[str, Any], branch: str) -> dict[str, str] | None:
    errors = payload.get("errors")
    raw = errors.get(branch) if isinstance(errors, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    code = str(raw.get("code") or "").strip()[:100]
    summary = str(raw.get("summary") or "").strip()[:500]
    if not code and not summary:
        return None
    return {"code": code or "CONTENT_ANALYSIS_FAILED", "summary": summary}


def _validated_remote_result(
    payload: Mapping[str, Any], *, original_script: str
) -> dict[str, Any]:
    expected_hash = _script_sha256(original_script)
    if str(payload.get("script_sha256") or "") != expected_hash:
        raise ValueError("数字人网站返回的内容分析脚本摘要不匹配")
    if int(payload.get("script_length") or -1) != len(original_script):
        raise ValueError("数字人网站返回的内容分析脚本长度不匹配")

    music_status = str(payload.get("music_analysis_status") or "").upper()
    subtitle_status = str(payload.get("subtitle_analysis_status") or "").upper()
    # Rolling upgrades may briefly pair the new workbench with an older cloud
    # response. Preserve music/subtitle results while treating the absent title
    # branch as independently unavailable.
    title_status = str(payload.get("title_analysis_status") or "FAILED").upper()
    if (
        music_status not in _BRANCH_STATUSES
        or subtitle_status not in _BRANCH_STATUSES
        or title_status not in _BRANCH_STATUSES
    ):
        raise ValueError("数字人网站返回的内容分析分支状态无效")

    music_intent = payload.get("music_intent")
    if music_status == "SUCCESS" and not isinstance(music_intent, Mapping):
        raise ValueError("音乐分析成功但缺少 music_intent")
    subtitle_units = payload.get("subtitle_units")
    if subtitle_status == "SUCCESS":
        if not isinstance(subtitle_units, list) or not subtitle_units:
            raise ValueError("字幕分析成功但缺少 subtitle_units")
        texts: list[str] = []
        for unit in subtitle_units:
            if not isinstance(unit, Mapping) or not isinstance(unit.get("text"), str):
                raise ValueError("subtitle_units 结构无效")
            texts.append(str(unit["text"]))
        if "".join(texts) != original_script:
            raise ValueError("subtitle_units 未逐字符重建原始脚本")
    title = payload.get("title")
    if title_status == "SUCCESS":
        title = normalize_cover_title(title)
        if not title["line_1"] or not title["line_2"]:
            raise ValueError("标题分析成功但缺少两行标题")
        if len(title["line_2"]) > GENERATED_TITLE_MAX_LINE_2_CHARS:
            raise ValueError(
                f"AI 标题第二行最多 {GENERATED_TITLE_MAX_LINE_2_CHARS} 个字符"
            )

    return {
        "schema_version": str(payload.get("schema_version") or "")[:100],
        "prompt_version": str(payload.get("prompt_version") or "")[:100],
        "subtitle_prompt_version": str(
            payload.get("subtitle_prompt_version") or ""
        )[:100],
        "model": str(payload.get("model") or "")[:200],
        "music_analysis_status": music_status,
        "subtitle_analysis_status": subtitle_status,
        "title_analysis_status": title_status,
        "music_intent": dict(music_intent) if isinstance(music_intent, Mapping) else None,
        "subtitle_units": subtitle_units if isinstance(subtitle_units, list) else None,
        "title": dict(title) if isinstance(title, Mapping) else None,
        "errors": {
            "music": _branch_error(payload, "music"),
            "subtitle": _branch_error(payload, "subtitle"),
            "title": _branch_error(payload, "title"),
        },
        "provider_request_id": (
            str(payload.get("provider_request_id"))[:200]
            if payload.get("provider_request_id")
            else None
        ),
        "provider_attempts": max(0, int(payload.get("provider_attempts") or 0)),
        "cache_hit": payload.get("cache_hit") is True,
        "cacheable": payload.get("cacheable") is True,
    }


@dataclass(frozen=True)
class _Target:
    item_id: str
    original_script: str
    script_sha256: str
    previous: dict[str, Any]
    visual: UnifiedVisualInput | None


class ProjectContentAnalysisCoordinator:
    """Analyze project scripts independently while preserving per-item snapshots."""

    def __init__(
        self,
        store: ProjectStore,
        client: AuthCenterClient,
        *,
        max_concurrency: int = CONTENT_ANALYSIS_BATCH_CONCURRENCY,
        music_selector: ProjectMusicSelector | None = None,
        visual_catalog: SemanticVisualCatalog | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.max_concurrency = max(
            1, min(int(max_concurrency), CONTENT_ANALYSIS_BATCH_CONCURRENCY)
        )
        self.music_selector = music_selector
        self.visual_catalog = visual_catalog

    def analyze(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        *,
        item_ids: list[str] | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        batch_started_at = time.monotonic()
        project = self.store.get_project(owner_user_id, project_id)
        requested = {
            str(item_id).strip()
            for item_id in (item_ids or [])
            if str(item_id).strip()
        }
        known = {str(item["item_id"]) for item in project["items"]}
        if requested.difference(known):
            raise KeyError("项目脚本行不存在")

        if self.visual_catalog is not None and not force_refresh:
            remapped = False
            for item in project["items"]:
                item_id = str(item["item_id"])
                visual_snapshot = item.get("visual_analysis") or {}
                if requested and item_id not in requested:
                    continue
                if (
                    visual_snapshot.get("analysis_status") == "SUCCESS"
                    and visual_snapshot.get("mapping_status") != "SUCCESS"
                ):
                    remapped = (
                        remap_saved_visual_plan(
                            self.store,
                            owner_user_id=owner_user_id,
                            project_id=project_id,
                            item=item,
                            catalog=self.visual_catalog,
                        )
                        or remapped
                    )
            if remapped:
                project = self.store.get_project(owner_user_id, project_id)

        targets: list[_Target] = []
        for item in project["items"]:
            item_id = str(item["item_id"])
            if requested and item_id not in requested:
                continue
            if not item.get("allowed_actions", {}).get("analyze_content", False):
                raise ValueError(f"任务 {item.get('row_key')} 正在生成或分析，请稍后重试")
            script = str(item["script_text"])
            script_hash = _script_sha256(script)
            previous = dict(item.get("content_analysis") or {})
            visual = (
                prepare_unified_visual_input(item, self.visual_catalog)
                if self.visual_catalog is not None
                else None
            )
            is_current = previous.get("script_sha256") == script_hash
            title_is_current = (
                str(previous.get("title_analysis_status") or "NOT_REQUESTED").upper()
                in _BRANCH_STATUSES
            )
            visual_is_current = True
            if visual is not None:
                previous_visual = visual.previous
                visual_is_current = (
                    previous_visual.get("script_sha256") == script_hash
                    and previous_visual.get("catalog_version")
                    == visual.candidate_request["catalog_version"]
                    and previous_visual.get("candidate_set_sha256")
                    == candidate_set_sha256(visual.candidate_request)
                    and previous_visual.get("analysis_status") in {"SUCCESS", "FAILED"}
                )
            if (
                not force_refresh
                and is_current
                and title_is_current
                and visual_is_current
                and str(previous.get("overall_status") or "")
                in _UNCHANGED_SCRIPT_STATUSES
            ):
                continue
            targets.append(_Target(item_id, script, script_hash, previous, visual))

        log_event(
            analysis_logger,
            "content_analysis.batch_planned",
            "工作台统一内容分析已规划",
            component="workbench",
            project_id=project_id,
            requested_item_count=len(requested) if requested else len(project["items"]),
            target_item_count=len(targets),
            force_refresh=bool(force_refresh),
            max_concurrency=self.max_concurrency,
        )

        for target in targets:
            content_pending_saved = self.store.mark_item_content_analysis_pending(
                owner_user_id,
                project_id,
                target.item_id,
                expected_script_sha256=target.script_sha256,
            )
            visual_pending_saved = False
            if target.visual is not None:
                visual_pending_saved = self.store.mark_item_visual_analysis_pending(
                    owner_user_id,
                    project_id,
                    target.item_id,
                    expected_script_sha256=target.script_sha256,
                    candidate_request=target.visual.candidate_request,
                )
            log_event(
                analysis_logger,
                "content_analysis.pending_saved",
                "本地分析状态已写入等待中",
                component="workbench",
                project_id=project_id,
                item_id=target.item_id,
                script_sha256=target.script_sha256,
                content_pending_saved=content_pending_saved,
                visual_pending_saved=visual_pending_saved,
                force_refresh=bool(force_refresh),
            )

        if not targets:
            return self.store.get_project(owner_user_id, project_id)

        workers = min(self.max_concurrency, len(targets))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            future_started_at = {}
            for target in targets:
                log_event(
                    analysis_logger,
                    "content_analysis.item_dispatched",
                    "脚本行已交给数字人网站请求线程",
                    component="workbench",
                    project_id=project_id,
                    item_id=target.item_id,
                    script_sha256=target.script_sha256,
                    force_refresh=bool(force_refresh),
                )
                future = pool.submit(
                    _analyze_with_visual_context_compat,
                    self.client,
                    token,
                    target.original_script,
                    force_refresh=force_refresh,
                    visual_context=(
                        target.visual.visual_context
                        if target.visual is not None
                        and target.visual.visual_context["anchors"]
                        else None
                    ),
                )
                futures[future] = target
                future_started_at[future] = time.monotonic()
            for future in as_completed(futures):
                target = futures[future]
                item_elapsed_ms = round(
                    (time.monotonic() - future_started_at[future]) * 1000
                )
                content_completed = False
                try:
                    remote = future.result()
                    log_event(
                        analysis_logger,
                        "content_analysis.item_response_received",
                        "脚本行已收到数字人网站响应，开始本地校验",
                        component="workbench",
                        project_id=project_id,
                        item_id=target.item_id,
                        script_sha256=target.script_sha256,
                        elapsed_ms=item_elapsed_ms,
                        trace_id=remote.get("_workbench_client_trace_id"),
                        provider_request_id=remote.get("provider_request_id"),
                        provider_attempts=remote.get("provider_attempts"),
                    )
                    validated = _validated_remote_result(
                        remote, original_script=target.original_script
                    )
                    content_completed = self.store.complete_item_content_analysis(
                        owner_user_id,
                        project_id,
                        target.item_id,
                        expected_script_sha256=target.script_sha256,
                        result=validated,
                        previous=target.previous,
                    )
                    log_event(
                        analysis_logger,
                        "content_analysis.item_content_saved",
                        "脚本行内容分析结果已完成本地校验并写入数据库",
                        component="workbench",
                        project_id=project_id,
                        item_id=target.item_id,
                        script_sha256=target.script_sha256,
                        database_write_applied=content_completed,
                        music_status=validated.get("music_analysis_status"),
                        subtitle_status=validated.get("subtitle_analysis_status"),
                        title_status=validated.get("title_analysis_status"),
                        provider_request_id=validated.get("provider_request_id"),
                        provider_attempts=validated.get("provider_attempts"),
                    )
                    if target.visual is not None and self.visual_catalog is not None:
                        visual = (
                            {
                                "analysis_status": "SUCCESS",
                                "visual_plan": [],
                                "error": None,
                            }
                            if remote.get("_workbench_visual_context_mode")
                            == "content_only"
                            else validate_remote_visual_plan(
                                remote,
                                candidate_request=target.visual.candidate_request,
                            )
                            if target.visual.candidate_request["candidates"]
                            else {
                                "analysis_status": "SUCCESS",
                                "visual_plan": [],
                                "error": None,
                            }
                        )
                        if visual["analysis_status"] != "SUCCESS":
                            self.store.fail_item_visual_analysis(
                                owner_user_id,
                                project_id,
                                target.item_id,
                                expected_script_sha256=target.script_sha256,
                                expected_catalog_version=target.visual.candidate_request[
                                    "catalog_version"
                                ],
                                expected_candidate_set_sha256=candidate_set_sha256(
                                    target.visual.candidate_request
                                ),
                                error=dict(visual["error"]),
                            )
                        else:
                            try:
                                latest_project = self.store.get_project(
                                    owner_user_id, project_id
                                )
                                latest_item = next(
                                    (
                                        item
                                        for item in latest_project.get("items", [])
                                        if item.get("item_id") == target.item_id
                                    ),
                                    None,
                                )
                                latest_visual = (
                                    prepare_unified_visual_input(
                                        latest_item, self.visual_catalog
                                    )
                                    if isinstance(latest_item, Mapping)
                                    else target.visual
                                )
                                visual_result, recipe = build_local_visual_result(
                                    script=target.original_script,
                                    visual_input=latest_visual,
                                    plan=visual["visual_plan"],
                                    catalog=self.visual_catalog,
                                    provider_payload=remote,
                                )
                                self.store.complete_item_visual_analysis(
                                    owner_user_id,
                                    project_id,
                                    target.item_id,
                                    expected_script_sha256=target.script_sha256,
                                    result=visual_result,
                                    recipe=recipe,
                                    mapping_status="SUCCESS",
                                )
                            except SemanticSubtitleMappingError as exc:
                                self.store.complete_item_visual_analysis(
                                    owner_user_id,
                                    project_id,
                                    target.item_id,
                                    expected_script_sha256=target.script_sha256,
                                    result={
                                        "analysis_status": "SUCCESS",
                                        "catalog_version": target.visual.candidate_request[
                                            "catalog_version"
                                        ],
                                        "candidate_set_sha256": candidate_set_sha256(
                                            target.visual.candidate_request
                                        ),
                                        "visual_plan": visual["visual_plan"],
                                        "decisions": [],
                                        "mapped_candidates": [],
                                        "provider_request_id": remote.get(
                                            "provider_request_id"
                                        ),
                                        "provider_attempts": int(
                                            remote.get("provider_attempts") or 0
                                        ),
                                        "cache_hit": remote.get("cache_hit") is True,
                                        "cacheable": remote.get("cacheable") is True,
                                    },
                                    recipe=empty_visual_recipe(
                                        target.visual, self.visual_catalog
                                    ),
                                    mapping_status="FAILED",
                                    mapping_error=_safe_error(exc),
                                )
                                current_project = self.store.get_project(
                                    owner_user_id, project_id
                                )
                                current_item = next(
                                    (
                                        item
                                        for item in current_project.get("items", [])
                                        if item.get("item_id") == target.item_id
                                    ),
                                    None,
                                )
                                if isinstance(current_item, Mapping):
                                    remap_saved_visual_plan(
                                        self.store,
                                        owner_user_id=owner_user_id,
                                        project_id=project_id,
                                        item=current_item,
                                        catalog=self.visual_catalog,
                                    )
                except Exception as exc:
                    safe_error = _safe_error(exc)
                    log_event(
                        analysis_logger,
                        "content_analysis.item_failed",
                        "脚本行统一内容分析失败",
                        level=logging.ERROR,
                        component="workbench",
                        project_id=project_id,
                        item_id=target.item_id,
                        script_sha256=target.script_sha256,
                        elapsed_ms=item_elapsed_ms,
                        error_code=safe_error.get("code"),
                        error_summary=safe_error.get("summary"),
                        remote_error_code=(
                            exc.error_code if isinstance(exc, AuthCenterError) else None
                        ),
                        http_status=(
                            exc.status_code if isinstance(exc, AuthCenterError) else None
                        ),
                    )
                    if not content_completed:
                        self.store.fail_item_content_analysis(
                            owner_user_id,
                            project_id,
                            target.item_id,
                            expected_script_sha256=target.script_sha256,
                            error=_safe_error(exc),
                            previous=target.previous,
                        )
                    if target.visual is not None:
                        self.store.fail_item_visual_analysis(
                            owner_user_id,
                            project_id,
                            target.item_id,
                            expected_script_sha256=target.script_sha256,
                            expected_catalog_version=target.visual.candidate_request[
                                "catalog_version"
                            ],
                            expected_candidate_set_sha256=candidate_set_sha256(
                                target.visual.candidate_request
                            ),
                            error=_safe_error(exc),
                        )
        project = self.store.get_project(owner_user_id, project_id)
        if self.music_selector is not None:
            targets_by_id = {target.item_id: target for target in targets}
            recent_identity_counts = automatic_music_identity_counts(
                project, excluded_item_ids=set(targets_by_id)
            )
            for item in project["items"]:
                item_id = str(item["item_id"])
                target = targets_by_id.get(item_id)
                if target is None:
                    continue
                analysis = item.get("content_analysis") or {}
                if analysis.get("music_analysis_status") != "SUCCESS":
                    continue
                postprocess = (item.get("settings") or {}).get("postprocess") or {}
                if postprocess.get("bgm_selection_mode") == "manual":
                    continue
                previous_selection = postprocess.get("music_selection") or {}
                same_script_retry = (
                    target.previous.get("script_sha256")
                    == analysis.get("script_sha256")
                )
                has_saved_auto_music = bool(
                    str(postprocess.get("bgm_identity") or "").strip()
                    and previous_selection.get("status") in {"SUCCESS", "STALE"}
                )
                if same_script_retry and has_saved_auto_music:
                    # Retrying subtitle analysis for unchanged copy must not make
                    # the already-approved automatic BGM jump to a different track.
                    saved_identity = str(postprocess.get("bgm_identity") or "").strip()
                    if saved_identity:
                        recent_identity_counts[saved_identity] = (
                            recent_identity_counts.get(saved_identity, 0) + 1
                        )
                    continue
                identity, selection = self.music_selector.resolve_for_analysis(
                    project,
                    item,
                    recent_identity_counts=recent_identity_counts,
                )
                self.store.save_item_auto_music_selection(
                    owner_user_id,
                    project_id,
                    item_id,
                    expected_script_sha256=str(analysis.get("script_sha256") or ""),
                    bgm_identity=identity,
                    music_selection=selection,
                )
                if identity:
                    recent_identity_counts[identity] = (
                        recent_identity_counts.get(identity, 0) + 1
                    )
            project = self.store.get_project(owner_user_id, project_id)
        target_ids = {target.item_id for target in targets}
        status_counts: dict[str, int] = {}
        for item in project.get("items", []):
            if str(item.get("item_id")) not in target_ids:
                continue
            status = str(
                (item.get("content_analysis") or {}).get("overall_status") or "UNKNOWN"
            )
            status_counts[status] = status_counts.get(status, 0) + 1
        log_event(
            analysis_logger,
            "content_analysis.batch_completed",
            "工作台统一内容分析批次已结束",
            component="workbench",
            project_id=project_id,
            target_item_count=len(targets),
            elapsed_ms=round((time.monotonic() - batch_started_at) * 1000),
            status_counts=status_counts,
        )
        return project

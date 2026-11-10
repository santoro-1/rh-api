from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

from .auth_center import AuthCenterClient, AuthCenterError
from .project_store import ProjectStore
from .semantic_subtitles import SemanticSubtitleMappingError
from .semantic_visuals import (
    DEFAULT_LIBRARY_ID,
    RECIPE_SCHEMA,
    SemanticVisualCatalog,
    build_visual_recipe,
    map_visual_candidates_to_raw_cues,
    recall_semantic_visual_candidates,
)
from .unified_visual_plan import UnifiedVisualInput, prepare_unified_visual_input


def _seam_recipe_fingerprint(recipe: Mapping[str, Any]) -> str:
    """Hash automatic seam overlays so stale success metadata cannot skip repair."""

    rows = []
    for raw in recipe.get("overlays", []):
        if (
            not isinstance(raw, Mapping)
            or raw.get("manual") is True
            or (
                raw.get("usage") != "seam_broll"
                and raw.get("timing_mode") != "seam_broll"
            )
        ):
            continue
        rows.append(
            {
                key: raw.get(key)
                for key in (
                    "overlay_id",
                    "candidate_id",
                    "concept_id",
                    "asset_id",
                    "segment_boundary_us",
                    "start_us",
                    "duration_us",
                    "source_start_us",
                    "source_duration_us",
                    "enabled",
                )
            }
        )
    encoded = json.dumps(
        sorted(
            rows,
            key=lambda item: (
                int(item.get("start_us") or 0),
                str(item.get("overlay_id") or ""),
            ),
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


VISUAL_ANALYSIS_BATCH_CONCURRENCY = 10
_DECISION_KEYS = {
    "candidate_id",
    "decision",
    "concept_id",
    "usage",
    "importance",
    "confidence",
    "reason_code",
}
_DECISIONS = {"SHOW", "REVIEW", "SKIP"}
_USAGES = {
    "literal",
    "ingredient",
    "meal_example",
    "idiom",
    "metaphor",
    "negated",
    "meta_mention",
    "passing_mention",
    "uncertain",
    "no_asset",
    "action",
    "scene",
    "editorial_context",
}
_REASON_CODES = {
    "LITERAL_CONCRETE_OBJECT",
    "MATCH_EXACT_OBJECT",
    "MATCH_SAME_ACTION",
    "MATCH_SAME_SCENE",
    "MATCH_EDITORIAL_CONTEXT",
    "SKIP_IDIOM",
    "SKIP_METAPHOR",
    "SKIP_NEGATED",
    "SKIP_META_MENTION",
    "SKIP_PASSING_MENTION",
    "SKIP_UNCERTAIN",
    "SKIP_NO_ASSET",
    "SKIP_UNRELATED",
}
_STRONG_AUTOMATIC_BROLL_REASONS = {
    "LITERAL_CONCRETE_OBJECT",
    "MATCH_EXACT_OBJECT",
    "MATCH_SAME_ACTION",
    "MATCH_SAME_SCENE",
}
_STRONG_AUTOMATIC_BROLL_USAGES = {
    "literal",
    "ingredient",
    "meal_example",
    "action",
    "scene",
}
_STRONG_AUTOMATIC_BROLL_MIN_CONFIDENCE = 0.90


def _is_strong_automatic_broll_decision(
    decision: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    """Accept only a confident concept directly recalled from the script."""

    direct_concept_ids = {
        str(value)
        for value in candidate.get("direct_concept_ids", ())
        if str(value)
    }
    return (
        decision.get("decision") == "SHOW"
        and float(decision.get("confidence") or 0.0)
        >= _STRONG_AUTOMATIC_BROLL_MIN_CONFIDENCE
        and str(decision.get("concept_id") or "") in direct_concept_ids
        and decision.get("reason_code") in _STRONG_AUTOMATIC_BROLL_REASONS
        and decision.get("usage") in _STRONG_AUTOMATIC_BROLL_USAGES
    )


def _candidate_set_sha256(request: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            request.get("candidates", []),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _safe_error(error: object) -> dict[str, str]:
    if isinstance(error, AuthCenterError):
        code = f"DIGITAL_HUMAN_HTTP_{error.status_code}"
    elif isinstance(error, SemanticSubtitleMappingError):
        code = error.code
    else:
        code = type(error).__name__.upper()[:100] or "VISUAL_ANALYSIS_FAILED"
    summary = str(error).strip() or "语义视觉分析失败"
    return {"code": code, "summary": summary[:500]}


def _validated_remote_result(
    payload: Mapping[str, Any], *, candidate_request: Mapping[str, Any]
) -> dict[str, Any]:
    expected_ids = {
        str(item["candidate_id"]): item
        for item in candidate_request.get("candidates", [])
        if isinstance(item, Mapping)
    }
    if payload.get("analysis_status") != "SUCCESS":
        error = payload.get("error")
        if not isinstance(error, Mapping):
            error = {"code": "VISUAL_ANALYSIS_FAILED", "summary": "云端视觉分析失败"}
        return {
            **dict(payload),
            "analysis_status": "FAILED",
            "decisions": [],
            "error": {
                "code": str(error.get("code") or "VISUAL_ANALYSIS_FAILED")[:100],
                "summary": str(error.get("summary") or "云端视觉分析失败")[:500],
            },
        }
    if payload.get("schema_version") != "jyd.visual-analysis.v1":
        raise ValueError("数字人网站返回了未知的视觉分析契约")
    if payload.get("script_sha256") != candidate_request.get("script_sha256"):
        raise ValueError("数字人网站返回的视觉分析脚本摘要不匹配")
    if payload.get("catalog_version") != candidate_request.get("catalog_version"):
        raise ValueError("数字人网站返回的视觉素材目录版本不匹配")
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("数字人网站返回的视觉决策不是数组")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for raw in decisions:
        if not isinstance(raw, Mapping) or set(raw) != _DECISION_KEYS:
            raise ValueError("数字人网站返回的视觉决策字段无效")
        candidate_id = str(raw.get("candidate_id") or "")
        if not candidate_id or candidate_id in seen or candidate_id not in expected_ids:
            raise ValueError("数字人网站返回了未知或重复的视觉候选")
        allowed = {
            str(item.get("concept_id") or "")
            for item in expected_ids[candidate_id].get("allowed_concepts", [])
            if isinstance(item, Mapping)
        }
        concept_id = str(raw.get("concept_id") or "")
        if concept_id not in allowed:
            raise ValueError("数字人网站返回了候选范围外的视觉概念")
        if raw.get("decision") not in _DECISIONS or raw.get("usage") not in _USAGES:
            raise ValueError("数字人网站返回了未知的视觉判定枚举")
        if raw.get("reason_code") not in _REASON_CODES:
            raise ValueError("数字人网站返回了未知的视觉原因码")
        importance = raw.get("importance")
        confidence = raw.get("confidence")
        if (
            isinstance(importance, bool)
            or not isinstance(importance, (int, float))
            or not 0 <= float(importance) <= 1
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= float(confidence) <= 1
        ):
            raise ValueError("数字人网站返回的视觉评分无效")
        seen.add(candidate_id)
        validated.append(dict(raw))
    if seen != set(expected_ids):
        raise ValueError("数字人网站没有逐一返回全部视觉候选")
    return {**dict(payload), "decisions": validated, "error": None}


@dataclass(frozen=True)
class _Target:
    item_id: str
    script: str
    request: dict[str, Any]
    raw_cues: list[dict[str, Any]]
    video_duration_us: int | None
    previous: dict[str, Any]


@dataclass(frozen=True)
class _SeamTarget:
    item_id: str
    script: str
    request: dict[str, Any]
    visual_input: UnifiedVisualInput


class ProjectVisualAnalysisCoordinator:
    """Own local recall/timing and use the cloud only for context decisions."""

    def __init__(
        self,
        store: ProjectStore,
        client: AuthCenterClient,
        catalog: SemanticVisualCatalog,
        *,
        max_concurrency: int = VISUAL_ANALYSIS_BATCH_CONCURRENCY,
    ) -> None:
        self.store = store
        self.client = client
        self.catalog = catalog
        self.max_concurrency = max(
            1, min(int(max_concurrency), VISUAL_ANALYSIS_BATCH_CONCURRENCY)
        )

    def analyze(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        *,
        item_ids: list[str] | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        requested = {str(item_id).strip() for item_id in (item_ids or []) if str(item_id).strip()}
        known = {str(item["item_id"]) for item in project["items"]}
        if requested.difference(known):
            raise KeyError("项目脚本行不存在")
        targets: list[_Target] = []
        for item in project["items"]:
            item_id = str(item["item_id"])
            if requested and item_id not in requested:
                continue
            if not item.get("allowed_actions", {}).get("analyze_visuals", False):
                raise ValueError(f"任务 {item.get('row_key')} 正在生成或分析，请稍后重试")
            script = str(item["script_text"])
            settings = item.get("settings") if isinstance(item.get("settings"), Mapping) else {}
            source_metadata = (
                settings.get("source_metadata") if isinstance(settings, Mapping) else {}
            )
            article_type = (
                str(source_metadata.get("article_type") or "")
                if isinstance(source_metadata, Mapping)
                else ""
            )
            candidate_request = recall_semantic_visual_candidates(
                script, self.catalog, article_type=article_type
            )
            previous = dict(item.get("visual_analysis") or {})
            if (
                not force_refresh
                and previous.get("script_sha256") == candidate_request["script_sha256"]
                and previous.get("catalog_version") == self.catalog.catalog_version
                and previous.get("analysis_status") in {"SUCCESS", "FAILED"}
            ):
                continue
            base_video = item.get("outputs", {}).get("base_video") or {}
            metadata = base_video.get("metadata") if isinstance(base_video, Mapping) else {}
            duration = metadata.get("duration_us") if isinstance(metadata, Mapping) else None
            targets.append(
                _Target(
                    item_id=item_id,
                    script=script,
                    request=candidate_request,
                    raw_cues=list(item.get("subtitles", {}).get("raw_cues") or []),
                    video_duration_us=(int(duration) if isinstance(duration, int) and duration > 0 else None),
                    previous=previous,
                )
            )
        for target in targets:
            self.store.mark_item_visual_analysis_pending(
                owner_user_id,
                project_id,
                target.item_id,
                expected_script_sha256=target.request["script_sha256"],
                candidate_request=target.request,
            )
        if not targets:
            return self.store.get_project(owner_user_id, project_id)

        local_only = [target for target in targets if not target.request["candidates"]]
        remote_targets = [target for target in targets if target.request["candidates"]]
        for target in local_only:
            self.store.complete_item_visual_analysis(
                owner_user_id,
                project_id,
                target.item_id,
                expected_script_sha256=target.request["script_sha256"],
                result={
                    "analysis_status": "SUCCESS",
                    "catalog_version": self.catalog.catalog_version,
                    "candidate_set_sha256": _candidate_set_sha256(target.request),
                    "decisions": [],
                    "cache_hit": True,
                    "cacheable": True,
                },
                recipe={
                    "schema": RECIPE_SCHEMA,
                    "library_id": self.catalog.library_id or DEFAULT_LIBRARY_ID,
                    "catalog_version": self.catalog.catalog_version,
                    "media_policy": "mixed",
                    "overlays": self._locked_overlays(target.previous),
                },
                mapping_status="SUCCESS",
            )

        workers = min(self.max_concurrency, len(remote_targets)) if remote_targets else 0
        if workers:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        self.client.analyze_workbench_visuals,
                        token,
                        target.request,
                        force_refresh=force_refresh,
                    ): target
                    for target in remote_targets
                }
                for future in as_completed(futures):
                    target = futures[future]
                    try:
                        result = _validated_remote_result(
                            future.result(), candidate_request=target.request
                        )
                        result["candidate_set_sha256"] = _candidate_set_sha256(
                            target.request
                        )
                        if result["analysis_status"] != "SUCCESS":
                            self.store.fail_item_visual_analysis(
                                owner_user_id,
                                project_id,
                                target.item_id,
                                expected_script_sha256=target.request["script_sha256"],
                                expected_catalog_version=target.request["catalog_version"],
                                expected_candidate_set_sha256=_candidate_set_sha256(
                                    target.request
                                ),
                                error=dict(result["error"]),
                            )
                            continue
                        mapped = map_visual_candidates_to_raw_cues(
                            target.script,
                            target.request["candidates"],
                            target.raw_cues,
                            video_duration_us=target.video_duration_us,
                        )
                        locked = self._locked_overlays(target.previous)
                        recipe = build_visual_recipe(
                            catalog=self.catalog,
                            mapped_candidates=mapped,
                            decisions=result["decisions"],
                            media_policy="mixed",
                            locked_overlays=locked,
                        )
                        self.store.complete_item_visual_analysis(
                            owner_user_id,
                            project_id,
                            target.item_id,
                            expected_script_sha256=target.request["script_sha256"],
                            result={**result, "mapped_candidates": mapped},
                            recipe=recipe,
                            mapping_status="SUCCESS",
                        )
                    except SemanticSubtitleMappingError as exc:
                        self.store.complete_item_visual_analysis(
                            owner_user_id,
                            project_id,
                            target.item_id,
                            expected_script_sha256=target.request["script_sha256"],
                            result={
                                **result,
                                "candidate_set_sha256": _candidate_set_sha256(
                                    target.request
                                ),
                                "mapped_candidates": [],
                            },
                            recipe={
                                "schema": RECIPE_SCHEMA,
                                "library_id": self.catalog.library_id or DEFAULT_LIBRARY_ID,
                                "catalog_version": self.catalog.catalog_version,
                                "media_policy": "mixed",
                                "overlays": self._locked_overlays(target.previous),
                            },
                            mapping_status="FAILED",
                            mapping_error=_safe_error(exc),
                        )
                    except Exception as exc:
                        self.store.fail_item_visual_analysis(
                            owner_user_id,
                            project_id,
                            target.item_id,
                            expected_script_sha256=target.request["script_sha256"],
                            expected_catalog_version=target.request["catalog_version"],
                            expected_candidate_set_sha256=_candidate_set_sha256(
                                target.request
                            ),
                            error=_safe_error(exc),
                        )
        return self.store.get_project(owner_user_id, project_id)

    def supplement_seams(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        *,
        item_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Analyze only newly available multi-segment seams and merge them in place.

        Digital-human segment boundaries do not exist during the first unified
        analysis.  This lightweight second pass runs after composition is ready;
        it never replaces the original visual plan or any manual overlay.
        """

        project = self.store.get_project(owner_user_id, project_id)
        requested = {
            str(item_id).strip()
            for item_id in (item_ids or [])
            if str(item_id).strip()
        }
        known = {str(item["item_id"]) for item in project["items"]}
        if requested.difference(known):
            raise KeyError("项目脚本行不存在")
        targets: list[_SeamTarget] = []
        for item in project["items"]:
            item_id = str(item["item_id"])
            if requested and item_id not in requested:
                continue
            visual_input = prepare_unified_visual_input(item, self.catalog)
            seam_candidates = [
                dict(candidate)
                for candidate in visual_input.candidate_request.get("candidates", [])
                if isinstance(candidate, Mapping)
                and str(candidate.get("usage") or "") == "seam_broll"
            ]
            if not seam_candidates:
                continue
            request_payload = self._seam_request(
                visual_input.candidate_request, seam_candidates
            )
            signature = _candidate_set_sha256(request_payload)
            previous_seam = (item.get("visual_analysis") or {}).get("seam_analysis")
            current_recipe = (item.get("visual_analysis") or {}).get("recipe")
            current_fingerprint = _seam_recipe_fingerprint(
                current_recipe if isinstance(current_recipe, Mapping) else {}
            )
            if (
                isinstance(previous_seam, Mapping)
                and previous_seam.get("status") == "SUCCESS"
                and previous_seam.get("candidate_set_sha256") == signature
                and previous_seam.get("catalog_version") == self.catalog.catalog_version
                and previous_seam.get("recipe_fingerprint") == current_fingerprint
            ):
                continue
            targets.append(
                _SeamTarget(
                    item_id=item_id,
                    script=str(item.get("script_text") or ""),
                    request=request_payload,
                    visual_input=visual_input,
                )
            )
        workers = min(self.max_concurrency, len(targets)) if targets else 0
        if workers:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        self.client.analyze_workbench_visuals,
                        token,
                        target.request,
                    ): target
                    for target in targets
                }
                for future in as_completed(futures):
                    target = futures[future]
                    signature = _candidate_set_sha256(target.request)
                    try:
                        result = _validated_remote_result(
                            future.result(), candidate_request=target.request
                        )
                        if result["analysis_status"] != "SUCCESS":
                            self._store_seam_failure(
                                owner_user_id,
                                project_id,
                                target,
                                signature,
                                dict(result["error"]),
                            )
                            continue
                        mapped = map_visual_candidates_to_raw_cues(
                            target.script,
                            target.request["candidates"],
                            target.visual_input.raw_cues,
                            video_duration_us=target.visual_input.video_duration_us,
                            asr_alignment=target.visual_input.asr_alignment,
                        )
                        candidates_by_id = {
                            str(candidate["candidate_id"]): candidate
                            for candidate in target.request["candidates"]
                        }
                        approved = [
                            {
                                **dict(decision),
                                "usage": "seam_broll",
                            }
                            for decision in result["decisions"]
                            if _is_strong_automatic_broll_decision(
                                decision,
                                candidates_by_id.get(
                                    str(decision.get("candidate_id") or ""), {}
                                ),
                            )
                        ]
                        manual = self._manual_overlays(target.visual_input.previous)
                        seam_recipe = build_visual_recipe(
                            catalog=self.catalog,
                            mapped_candidates=mapped,
                            decisions=approved,
                            media_policy="mixed",
                            locked_overlays=manual,
                            segment_boundaries=target.visual_input.segment_boundaries or [],
                            final_video_duration_us=target.visual_input.video_duration_us,
                        )
                        merged_recipe = self._merge_seam_recipe(
                            target.visual_input.previous, seam_recipe
                        )
                        recipe_fingerprint = _seam_recipe_fingerprint(merged_recipe)
                        self.store.update_item_seam_visual_analysis(
                            owner_user_id,
                            project_id,
                            target.item_id,
                            expected_script_sha256=target.request["script_sha256"],
                            seam_analysis={
                                "status": "SUCCESS",
                                "candidate_set_sha256": signature,
                                "catalog_version": self.catalog.catalog_version,
                                "recipe_fingerprint": recipe_fingerprint,
                                "decisions": list(result["decisions"]),
                                "mapped_candidates": mapped,
                                "provider_request_id": result.get("provider_request_id"),
                                "provider_attempts": int(result.get("provider_attempts") or 0),
                                "cache_hit": result.get("cache_hit") is True,
                                "error": None,
                            },
                            recipe=merged_recipe,
                        )
                    except Exception as exc:
                        self._store_seam_failure(
                            owner_user_id,
                            project_id,
                            target,
                            signature,
                            _safe_error(exc),
                        )
        return self.store.get_project(owner_user_id, project_id)

    @staticmethod
    def _seam_request(
        candidate_request: Mapping[str, Any],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        clean_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            allowed = [
                {
                    "concept_id": str(value.get("concept_id") or ""),
                    "description": str(value.get("description") or ""),
                }
                for value in candidate.get("allowed_concepts", [])
                if isinstance(value, Mapping)
            ]
            allowed_ids = {value["concept_id"] for value in allowed}
            clean_candidates.append(
                {
                    "candidate_id": str(candidate["candidate_id"]),
                    "text": str(candidate["text"]),
                    "char_start": int(candidate["char_start"]),
                    "char_end": int(candidate["char_end"]),
                    "allowed_concepts": allowed,
                    "usage": "seam_broll",
                    "direct_concept_ids": [
                        str(value)
                        for value in candidate.get("direct_concept_ids", [])
                        if str(value) in allowed_ids
                    ],
                    "segment_boundary_us": int(candidate["segment_boundary_us"]),
                }
            )
        return {
            "schema_version": "jyd.visual-analysis.request.v1",
            "original_script": str(candidate_request["original_script"]),
            "script_sha256": str(candidate_request["script_sha256"]),
            "catalog_version": str(candidate_request["catalog_version"]),
            "candidates": clean_candidates,
        }

    @staticmethod
    def _manual_overlays(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
        recipe = snapshot.get("recipe") if isinstance(snapshot.get("recipe"), Mapping) else {}
        return [
            dict(overlay)
            for overlay in recipe.get("overlays", [])
            if isinstance(overlay, Mapping) and overlay.get("manual") is True
        ]

    def _merge_seam_recipe(
        self,
        snapshot: Mapping[str, Any],
        seam_recipe: Mapping[str, Any],
    ) -> dict[str, Any]:
        previous_recipe = (
            dict(snapshot.get("recipe"))
            if isinstance(snapshot.get("recipe"), Mapping)
            else {}
        )
        new_seams = [
            dict(overlay)
            for overlay in seam_recipe.get("overlays", [])
            if isinstance(overlay, Mapping)
            and overlay.get("manual") is not True
            and (
                overlay.get("usage") == "seam_broll"
                or overlay.get("timing_mode") == "seam_broll"
            )
        ]

        def overlaps_seam(overlay: Mapping[str, Any]) -> bool:
            start = int(overlay.get("start_us") or 0)
            end = start + int(overlay.get("duration_us") or 0)
            asset_id = str(overlay.get("asset_id") or "")
            return any(
                (
                    start
                    < int(seam.get("start_us") or 0)
                    + int(seam.get("duration_us") or 0)
                    and int(seam.get("start_us") or 0) < end
                )
                or (
                    asset_id
                    and asset_id == str(seam.get("asset_id") or "")
                )
                for seam in new_seams
            )

        retained: list[dict[str, Any]] = []
        for raw in previous_recipe.get("overlays", []):
            if not isinstance(raw, Mapping):
                continue
            overlay = dict(raw)
            automatic_seam = overlay.get("manual") is not True and (
                overlay.get("usage") == "seam_broll"
                or overlay.get("timing_mode") == "seam_broll"
            )
            if automatic_seam:
                continue
            if overlay.get("manual") is not True and overlaps_seam(overlay):
                continue
            retained.append(overlay)
        overlays = sorted(
            [*retained, *new_seams],
            key=lambda value: (
                int(value.get("start_us") or 0),
                str(value.get("overlay_id") or ""),
            ),
        )
        return {
            **previous_recipe,
            "schema": seam_recipe.get("schema") or RECIPE_SCHEMA,
            "library_id": seam_recipe.get("library_id")
            or self.catalog.library_id
            or DEFAULT_LIBRARY_ID,
            "catalog_version": self.catalog.catalog_version,
            "media_policy": "mixed",
            "timing_policy_version": seam_recipe.get("timing_policy_version")
            or previous_recipe.get("timing_policy_version")
            or "sentence-v1",
            "used_asset_ids": sorted(
                {
                    str(overlay.get("asset_id") or "")
                    for overlay in overlays
                    if overlay.get("enabled") is not False
                    and str(overlay.get("asset_id") or "")
                }
            ),
            "overlays": overlays,
        }

    def _store_seam_failure(
        self,
        owner_user_id: str,
        project_id: str,
        target: _SeamTarget,
        signature: str,
        error: dict[str, str],
    ) -> None:
        self.store.update_item_seam_visual_analysis(
            owner_user_id,
            project_id,
            target.item_id,
            expected_script_sha256=target.request["script_sha256"],
            seam_analysis={
                "status": "FAILED",
                "candidate_set_sha256": signature,
                "decisions": [],
                "mapped_candidates": [],
                "error": error,
            },
        )

    def _locked_overlays(self, snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
        recipe = snapshot.get("recipe") if isinstance(snapshot.get("recipe"), Mapping) else {}
        locked: list[dict[str, Any]] = []
        for item in recipe.get("overlays", []):
            if (
                not isinstance(item, Mapping)
                or item.get("manual") is not True
                or item.get("locked") is not True
            ):
                continue
            overlay = dict(item)
            asset = self.catalog.asset(str(overlay.get("asset_id") or ""))
            for absolute_key in ("bundle_path", "image_path", "video_path"):
                overlay.pop(absolute_key, None)
            if asset is None:
                overlay["requires_review"] = True
            else:
                media_type = asset["media_type"]
                overlay.update(
                    {
                        "media_type": media_type,
                        "renderer": asset["renderer"],
                        "resource_path": (
                            asset["resource"]["bundle"]
                            if media_type == "image"
                            else asset["resource"]["video"]
                        ),
                    }
                )
            locked.append(overlay)
        return locked

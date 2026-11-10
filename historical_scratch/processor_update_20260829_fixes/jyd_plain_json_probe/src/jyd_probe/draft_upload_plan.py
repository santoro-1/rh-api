from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any


DRAFT_UPLOAD_PLAN_SCHEMA = "jyd_probe.draft_upload_plan.v1"
VALID_POLICIES = {"keep", "replace", "remove"}
DEPENDENCY_POLICY_GROUPS = {
    "audio": "audio",
    "sound_effect": "audio",
    "video_effect": "video_effects",
    "font": "text_style",
    "text_effect": "text_effects",
    "text_template_resource": "text_templates",
}


def build_draft_upload_plan(
    report: dict[str, Any],
    policies: dict[str, str] | None = None,
    *,
    mode: str = "default",
) -> dict[str, Any]:
    """Decide which external dependencies remain necessary after replacements."""

    normalized_mode = str(mode or "default").strip().lower()
    if normalized_mode not in {"default", "template_center"}:
        raise ValueError(f"不支持的上传清单模式: {mode}")
    normalized_policies = _normalize_policies(policies or {})
    draft = report.get("draft", {})
    primary_video = draft.get("main_video", {}) if isinstance(draft, dict) else {}
    primary_video_material_id = (
        str(primary_video.get("material_id") or "")
        if isinstance(primary_video, dict)
        else ""
    )
    decisions: list[dict[str, Any]] = []
    for dependency in report.get("dependencies", []):
        if not isinstance(dependency, dict):
            continue
        item = dict(dependency)
        kind = str(item.get("kind", "resource"))
        group = DEPENDENCY_POLICY_GROUPS.get(kind, "fixed_content")
        referenced_material_ids = {
            str(reference.get("material_id") or "")
            for reference in item.get("references", [])
            if isinstance(reference, dict)
        }
        is_primary_video = bool(
            kind == "video"
            and primary_video_material_id
            and primary_video_material_id in referenced_material_ids
        )
        if normalized_mode == "template_center" and kind in {"audio", "sound_effect"}:
            policy = "replace"
            decision, reason = (
                "skip_replaced",
                "模板中心会按项目重新生成语音和 BGM，旧音频不上传",
            )
        elif normalized_mode == "template_center" and is_primary_video:
            policy = "replace"
            decision, reason = (
                "skip_replaced",
                "模板中心只保留主视频占位结构，原主视频文件不上传",
            )
        elif normalized_mode == "template_center" and item.get("status") == "central_library":
            policy = "keep"
            decision, reason = (
                "upload",
                "账号模板保存独立资源副本，避免依赖另一台机器的素材库路径",
            )
        else:
            can_skip = bool(item.get("can_skip_if_replaced")) and group != "fixed_content"
            policy = normalized_policies.get(group, "keep") if can_skip else "keep"
            decision, reason = _dependency_decision(
                str(item.get("status", "external")), policy
            )
        item.update(
            {
                "policy_group": group,
                "policy": policy,
                "decision": decision,
                "decision_reason": reason,
            }
        )
        decisions.append(item)

    decision_counts = Counter(str(item.get("decision", "unknown")) for item in decisions)
    upload_items = [item for item in decisions if item.get("decision") == "upload"]
    blocked_items = [
        item
        for item in decisions
        if item.get("decision") in {"blocked_missing", "blocked_external"}
    ]
    skipped_items = [
        item
        for item in decisions
        if item.get("decision") in {"skip_replaced", "skip_removed"}
    ]
    return {
        "schema": DRAFT_UPLOAD_PLAN_SCHEMA,
        "mode": normalized_mode,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "report_id": str(report.get("report_id", "")),
        "draft": report.get("draft", {}),
        "policies": normalized_policies,
        "draft_package": {
            "required": True,
            "reason": "草稿 JSON、时间线和内部结构始终需要迁移",
        },
        "dependencies": decisions,
        "summary": {
            "dependency_count": len(decisions),
            "decision_counts": dict(sorted(decision_counts.items())),
            "upload_count": len(upload_items),
            "upload_size_bytes": sum(int(item.get("size_bytes", 0) or 0) for item in upload_items),
            "reuse_library_count": decision_counts.get("reuse_library", 0),
            "skipped_count": len(skipped_items),
            "blocked_count": len(blocked_items),
            "ready_for_upload": not blocked_items,
        },
    }


def _normalize_policies(values: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for group in {"audio", "video_effects", "text_style", "text_effects", "text_templates"}:
        policy = str(values.get(group, "keep")).strip().lower()
        if policy not in VALID_POLICIES:
            raise ValueError(f"{group} 的迁移策略不合法: {policy}")
        result[group] = policy
    # Composite text templates are manually designed parts of a mother draft.
    # Their resources must always migrate with the draft and remain unchanged.
    result["text_templates"] = "keep"
    return result


def _dependency_decision(status: str, policy: str) -> tuple[str, str]:
    if policy == "replace":
        return "skip_replaced", "该类内容会被新素材替换，旧资源不上传"
    if policy == "remove":
        return "skip_removed", "该类内容会从输出草稿清除，旧资源不上传"
    if status == "central_library":
        return "reuse_library", "服务器素材库已有，无需重复上传"
    if status == "upload_required":
        return "upload", "保留该内容且服务器没有，需要上传"
    if status == "missing":
        return "blocked_missing", "需要保留，但本机文件已缺失"
    return "blocked_external", "需要保留，但当前不是可上传的本地文件"

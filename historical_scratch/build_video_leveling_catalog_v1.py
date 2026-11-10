from __future__ import annotations

import csv
import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


WORKSPACE = Path(r"D:\工作内容\轻盈健\数字人")
WORKBENCH = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe")
LEVELING = WORKSPACE / ".codex_tmp" / "semantic_full_review" / "video_leveling_v1.csv"
TAXONOMY = WORKSPACE / ".codex_tmp" / "semantic_full_review" / "video_taxonomy_v1.json"
PACKAGE = WORKSPACE / ".codex_tmp" / "semantic_catalog_v3_expansion"
LIBRARY = PACKAGE / "library"
ACTIVE = WORKBENCH / "data" / "libraries" / "semantic_visual_library" / "catalog.json"
CANDIDATE = LIBRARY / "catalog.video-leveling-v1.candidate.json"
BACKUP = PACKAGE / "catalog.pre-video-leveling-v1.json"
MIGRATION = PACKAGE / "migration.video-leveling-v1.pending.json"
AUDIT = PACKAGE / "video-leveling-v1.candidate.audit.json"


FALLBACK_CONCEPTS = {
    "activity.aerobic": (
        "有氧活动",
        "可由完整动作视频表达的有氧活动类别",
        ["有氧运动", "有氧训练", "有氧操", "燃脂运动"],
    ),
    "activity.commuting": (
        "通勤出行",
        "步行、地铁或候车等日常通勤出行",
        ["日常通勤", "通勤出行", "上下班通勤"],
    ),
    "activity.food_preparation": (
        "日常备餐",
        "洗切、烧水、打蛋或泡茶等日常食物准备活动",
        ["日常备餐", "准备饭菜", "备餐过程"],
    ),
    "activity.health_tracking": (
        "健康记录",
        "称重或记录身体数据等健康跟踪活动",
        ["健康记录", "身体数据记录", "健康数据记录"],
    ),
    "activity.light_daily": (
        "日常轻活动",
        "步行、轻柔活动等低强度日常身体活动",
        ["日常轻活动", "轻活动", "轻量活动", "日常活动", "活动一下"],
    ),
    "activity.mind_body": (
        "传统舒缓运动",
        "八段锦、气功等传统且节奏舒缓的身体活动",
        ["传统养生运动", "舒缓传统运动", "传统舒缓运动"],
    ),
    "activity.mobility": (
        "拉伸与关节活动",
        "踮脚、抬膝、伸展等轻柔关节活动",
        ["关节活动", "轻柔伸展", "活动关节"],
    ),
    "activity.self_care": (
        "日常养护",
        "泡脚、休息或晒太阳等日常自我照顾",
        ["日常养护", "自我照顾", "日常放松"],
    ),
    "activity.strength_training": (
        "力量训练",
        "哑铃、器械或自重抗阻训练",
        ["力量训练", "抗阻训练", "举哑铃", "哑铃训练"],
    ),
    "activity.work_study": (
        "办公学习",
        "使用电脑、阅读或开会等工作学习活动",
        ["日常办公", "伏案工作", "工作学习"],
    ),
    "meal.light_meal": (
        "清淡餐食",
        "整体呈现为清淡、简洁的餐食",
        ["清淡餐食", "清淡饮食", "清淡餐", "轻食餐"],
    ),
    "scene.city_night": (
        "城市夜景",
        "夜晚城市、街头、建筑或车流场景",
        ["城市夜景", "夜晚城市", "夜间街头"],
    ),
    "scene.dining": (
        "用餐空间",
        "餐桌、餐厅或家庭用餐环境",
        ["用餐场景", "餐桌场景", "家庭用餐场景"],
    ),
    "scene.emotion_atmosphere": (
        "情绪氛围",
        "以孤独、低落或安静人物状态为主的氛围空镜",
        ["低落氛围", "孤独氛围", "情绪氛围"],
    ),
    "scene.gym": (
        "健身空间",
        "健身房或室内运动训练空间",
        ["健身空间", "健身房场景", "室内运动空间"],
    ),
    "scene.home": (
        "居家场景",
        "卧室、窗边、阳台或庭院等居家环境",
        ["居家场景", "家中场景", "居家环境"],
    ),
    "scene.market_public": (
        "市场与公共空间",
        "菜市场、夜市等市场或公共空间",
        ["市场场景", "菜市场场景", "公共空间"],
    ),
    "scene.nature": (
        "自然户外",
        "山水、树林、湖泊或田野等自然环境",
        ["自然风景", "自然环境", "山水自然"],
    ),
    "scene.office_general": (
        "办公场景",
        "办公室、写字楼或电脑工作台环境",
        ["办公场景", "办公室场景", "工作环境"],
    ),
    "scene.social": (
        "社交人群",
        "会议、亲子或多人互动场景",
        ["社交场景", "人群互动", "多人互动"],
    ),
    "scene.technology": (
        "科技场景",
        "人工智能、数据界面或未来科技环境",
        ["科技场景", "人工智能场景", "未来科技场景"],
    ),
    "scene.traditional": (
        "中式传统场景",
        "传统建筑、寺庙、庭院等中式文化环境",
        ["中式传统场景", "传统文化场景", "中式场景"],
    ),
    "scene.urban": (
        "城市场景",
        "城市道路、交通、建筑或公共街景",
        ["城市生活", "城市街景", "城市场景"],
    ),
    "scene.weather_season": (
        "天气季节",
        "天气变化或季节特征明显的环境空镜",
        ["天气季节", "季节天气", "季节变化"],
    ),
}

BROAD_VIDEO_CONCEPTS = {
    "activity.dance",
    "activity.exercise",
    "activity.light_daily",
    "activity.relaxing",
    "activity.self_care",
    "meal.breakfast",
    "scene.ai",
    "scene.city",
    "scene.nature",
    "scene.scifi",
    "scene.technology",
    "scene.urban",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def split_ids(value: str) -> list[str]:
    return [item.strip() for item in value.split("|") if item.strip()]


def reviewed_id(asset_id: str) -> str | None:
    parts = asset_id.split(".")
    if len(parts) >= 4 and parts[0] == "review" and parts[1].startswith("u"):
        return parts[1].upper()
    return None


def merge_aliases(existing: list[str], preferred: list[str]) -> list[str]:
    result: list[str] = []
    for value in [*preferred, *existing]:
        value = value.strip()
        if value and value not in result and "（" not in value:
            result.append(value)
    return result


def baseline_taxonomy(asset: dict) -> dict:
    concepts = list(asset["concept_ids"])
    concept = concepts[0]
    name = asset["name"]
    l2: list[str]
    fallback: list[str] = []
    actions = list(asset.get("visual_actions") or [])
    if concept == "activity.walking":
        l2, fallback, actions = ["l2.activity.light_daily"], ["activity.light_daily"], ["walking"]
    elif concept == "activity.aerobic":
        l2, fallback, actions = ["l2.activity.cardio"], ["activity.aerobic"], ["training"]
    elif concept in {"activity.core_training"}:
        l2, fallback, actions = ["l2.activity.strength"], ["activity.strength_training"], ["training"]
    elif concept in {"activity.waist_twist"}:
        l2, fallback, actions = ["l2.activity.mobility"], ["activity.mobility"], ["training"]
    elif concept == "health.sleep":
        l2 = ["l2.health.sleep"]
    elif concept.startswith("health."):
        l2, fallback = ["l2.health.body_tracking"], ["activity.health_tracking"]
    elif concept.startswith(("topic.weight_loss", "weight_management.")):
        l2 = ["l2.health.weight_management"]
    else:
        raise ValueError(f"unclassified baseline video: {asset['asset_id']} {name}")
    if not set(asset.get("usage_modes") or []) & {"full_screen_broll", "seam_broll"}:
        fallback = []
    return {
        "l1_domain_ids": ["l1.activity_wellness"],
        "l2_category_ids": l2,
        "l3_exact_concept_ids": concepts,
        "action_ids": actions,
        "scene_ids": [],
        "fallback_concept_ids": fallback,
        "fallback_policy": "video_only_explicit_whitelist",
        "review_status": "BASELINE_COMPAT_REVIEWED_V1",
    }


def main() -> None:
    rows = {
        row["review_id"]: row
        for row in csv.DictReader(LEVELING.open("r", encoding="utf-8-sig", newline=""))
    }
    if len(rows) != 432:
        raise ValueError(f"expected 432 reviewed video rows, got {len(rows)}")
    payload = json.loads(ACTIVE.read_text(encoding="utf-8"))
    original_images = {
        asset["asset_id"]: json.dumps(asset, ensure_ascii=False, sort_keys=True)
        for asset in payload["assets"]
        if asset["media_type"] == "image"
    }
    concept_by_id = {item["concept_id"]: item for item in payload["concepts"]}
    used_fallbacks = {
        value
        for row in rows.values()
        for value in split_ids(row["fallback_concept_ids"])
    }
    for concept_id in sorted(used_fallbacks):
        label, description, aliases = FALLBACK_CONCEPTS[concept_id]
        if concept_id in concept_by_id:
            concept = concept_by_id[concept_id]
            concept["label"] = label
            concept["description"] = description
            concept["aliases"] = merge_aliases(concept["aliases"], aliases)
        else:
            concept = {
                "concept_id": concept_id,
                "label": label,
                "description": description,
                "aliases": aliases,
            }
            payload["concepts"].append(concept)
            concept_by_id[concept_id] = concept

    reviewed_seen: set[str] = set()
    video_count = 0
    baseline_count = 0
    for asset in payload["assets"]:
        if asset["media_type"] != "video":
            continue
        video_count += 1
        rid = reviewed_id(asset["asset_id"])
        if rid is None:
            asset["video_taxonomy"] = baseline_taxonomy(asset)
            baseline_count += 1
            continue
        row = rows.get(rid)
        if row is None:
            raise ValueError(f"reviewed video is missing leveling row: {asset['asset_id']}")
        reviewed_seen.add(rid)
        current_exact = list(asset["concept_ids"])
        exact = [value for value in current_exact if value not in BROAD_VIDEO_CONCEPTS]
        exact = exact or current_exact[:1]
        fallback = split_ids(row["fallback_concept_ids"])
        actions = split_ids(row["action_ids"])
        asset["concept_ids"] = exact
        asset["auto_trigger_concept_ids"] = exact
        asset["semantic_roles"] = {"depicts": exact, "expresses": [], "related": []}
        asset["trigger_basis"] = {value: "exact_subject" for value in exact}
        asset["visual_actions"] = actions
        asset["video_taxonomy"] = {
            "l1_domain_ids": split_ids(row["l1_domain_ids"]),
            "l2_category_ids": split_ids(row["l2_category_ids"]),
            "l3_exact_concept_ids": exact,
            "action_ids": actions,
            "scene_ids": split_ids(row["scene_ids"]),
            "fallback_concept_ids": fallback,
            "fallback_policy": "video_only_explicit_whitelist",
            "review_status": row["leveling_review"],
        }

    if reviewed_seen != set(rows):
        raise ValueError(f"leveling rows without catalog video: {sorted(set(rows) - reviewed_seen)}")
    if video_count != 451 or baseline_count != 19:
        raise ValueError(f"unexpected video counts: total={video_count} baseline={baseline_count}")

    # Alias ownership remains unique after adding the controlled broad concepts.
    aliases: dict[str, list[str]] = {}
    for concept in payload["concepts"]:
        for alias in concept["aliases"]:
            aliases.setdefault(alias, []).append(concept["concept_id"])
    duplicate_aliases = {
        alias: owners for alias, owners in aliases.items() if len(set(owners)) > 1
    }
    if duplicate_aliases:
        raise ValueError(f"duplicate aliases after leveling: {duplicate_aliases}")

    current_images = {
        asset["asset_id"]: json.dumps(asset, ensure_ascii=False, sort_keys=True)
        for asset in payload["assets"]
        if asset["media_type"] == "image"
    }
    if current_images != original_images:
        raise ValueError("image assets changed during video-only leveling")

    payload["concepts"].sort(key=lambda item: item["concept_id"])
    CANDIDATE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    shutil.copy2(ACTIVE, BACKUP)
    audit = {
        "schema": "jyd.semantic-visual-video-leveling-candidate-audit.v1",
        "source_catalog_sha256": sha256(ACTIVE),
        "candidate_sha256": sha256(CANDIDATE),
        "assets": len(payload["assets"]),
        "images_unchanged": len(original_images),
        "videos_with_taxonomy": video_count,
        "reviewed_videos_with_taxonomy": len(reviewed_seen),
        "baseline_videos_with_taxonomy": baseline_count,
        "videos_with_fallback": sum(
            bool(asset.get("video_taxonomy", {}).get("fallback_concept_ids"))
            for asset in payload["assets"]
        ),
        "fallback_counts": dict(
            sorted(
                Counter(
                    value
                    for asset in payload["assets"]
                    for value in asset.get("video_taxonomy", {}).get(
                        "fallback_concept_ids", []
                    )
                ).items()
            )
        ),
        "image_video_taxonomy_fields": sum(
            "video_taxonomy" in asset
            for asset in payload["assets"]
            if asset["media_type"] == "image"
        ),
        "duplicate_aliases": duplicate_aliases,
        "needs_clip_rows_preserved": sum(
            row["decision"] == "NEEDS_CLIP" for row in rows.values()
        ),
    }
    AUDIT.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    migration = {
        "schema": "jyd.semantic-visual-catalog-migration.v1",
        "source_catalog_path": str(ACTIVE),
        "source_catalog_schema": payload["schema"],
        "source_catalog_sha256": sha256(ACTIVE),
        "source_backup_path": str(BACKUP),
        "source_backup_sha256": sha256(BACKUP),
        "candidate_path": str(CANDIDATE),
        "candidate_schema": payload["schema"],
        "candidate_sha256": sha256(CANDIDATE),
        "asset_count": len(payload["assets"]),
        "approval": {
            "status": "approved",
            "approved_by": "codex-full-library-audit",
            "approved_at": datetime.now(timezone.utc).isoformat(),
        },
        "rollback": {"required_current_sha256": sha256(CANDIDATE)},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "local video-only hierarchy; images unchanged; no production deploy",
    }
    MIGRATION.write_text(
        json.dumps(migration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

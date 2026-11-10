"""Add the reviewed editorial B-roll pools to a v3 semantic-video catalog.

The migration is intentionally conservative: exact concepts are untouched, and
only videos whose audited scene/action taxonomy supports a broad editorial use
receive pool fallbacks. Run without ``--apply`` for a dry-run summary.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


POOL_CONCEPTS = (
    {
        "concept_id": "editorial.home_daily",
        "label": "居家日常空镜池",
        "description": "适合作为居家生活、日常习惯和普通生活叙事陪衬的室内或家庭空间空镜；不表示具体行为或功效证据。",
        "aliases": ["居家日常空镜池"],
    },
    {
        "concept_id": "editorial.meal_daily",
        "label": "三餐饮食空镜池",
        "description": "适合作为三餐、买菜、备餐、做饭和吃饭等生活语境陪衬的空镜；不得用来证明营养成分或健康功效。",
        "aliases": ["三餐饮食空镜池"],
    },
    {
        "concept_id": "editorial.leisure_daily",
        "label": "休闲生活空镜池",
        "description": "适合作为散步、公园、逛市场、休息和轻松日常等生活语境陪衬的空镜；不替代具体运动动作。",
        "aliases": ["休闲生活空镜池"],
    },
    {
        "concept_id": "editorial.family_life",
        "label": "家庭亲情空镜池",
        "description": "适合作为家庭陪伴、亲子、共同用餐和家人相处等亲情语境陪衬的空镜。",
        "aliases": ["家庭亲情空镜池"],
    },
    {
        "concept_id": "editorial.mood_atmosphere",
        "label": "状态氛围空镜池",
        "description": "适合作为坚持、时间变化、平静、希望或叙事转折陪衬的自然光影、风景和状态空镜；不表达具体事实。",
        "aliases": ["状态氛围空镜池"],
    },
)
POOL_IDS = tuple(item["concept_id"] for item in POOL_CONCEPTS)
FAMILY_PATTERN = re.compile(
    r"家庭|亲子|家人|一家|三口|母女|母子|父女|父子|祖孙|陪(?:你|伴).{0,4}长大"
)
TIME_MOOD_PATTERN = re.compile(r"时钟|钟表|日历|时间|日出|日落|夕阳|晚霞|阳光|光影")


def _pool_ids_for_asset(asset: dict[str, Any]) -> list[str]:
    taxonomy = asset.get("video_taxonomy") or {}
    if taxonomy.get("review_status") == "CODEX_REVIEWED_EDITORIAL_V1":
        return [
            pool_id
            for pool_id in POOL_IDS
            if pool_id in set(taxonomy.get("fallback_concept_ids") or ())
        ]
    l2 = set(taxonomy.get("l2_category_ids") or ())
    actions = set(asset.get("visual_actions") or ())
    text = f"{asset.get('name', '')} {asset.get('description', '')}"
    pools: list[str] = []

    home_actions_ok = not actions or bool(actions & {"reading", "resting"})
    if (
        "l2.scene.home" in l2
        and "l2.scene.emotion" not in l2
        and home_actions_ok
    ):
        pools.append("editorial.home_daily")

    if l2 & {
        "l2.meal.general",
        "l2.meal.breakfast",
        "l2.meal.light",
        "l2.scene.dining",
        "l2.activity.food_prep",
    }:
        pools.append("editorial.meal_daily")

    if (
        "l2.scene.market_public" in l2
        or "l2.activity.light_daily" in l2
        or ("l2.activity.self_care" in l2 and bool(actions & {"reading", "resting"}))
    ) and not bool(actions & {"running", "training"}):
        pools.append("editorial.leisure_daily")

    if FAMILY_PATTERN.search(text):
        pools.append("editorial.family_life")

    mood_scene = bool(
        l2
        & {
            "l2.scene.nature",
            "l2.scene.weather_season",
            "l2.scene.emotion",
            "l2.scene.traditional",
        }
    )
    if (mood_scene or TIME_MOOD_PATTERN.search(text)) and not bool(
        actions & {"running", "training"}
    ):
        pools.append("editorial.mood_atmosphere")

    return [pool_id for pool_id in POOL_IDS if pool_id in pools]


def migrate(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if payload.get("schema") != "jyd.semantic-visual-catalog.v3":
        raise ValueError("editorial B-roll pools require catalog schema v3")

    concepts = list(payload.get("concepts") or ())
    concept_index = {
        str(concept.get("concept_id") or ""): index
        for index, concept in enumerate(concepts)
    }
    for pool in POOL_CONCEPTS:
        index = concept_index.get(pool["concept_id"])
        if index is None:
            concepts.append(dict(pool))
        else:
            concepts[index] = dict(pool)
    payload["concepts"] = concepts

    counts: Counter[str] = Counter()
    assigned_assets: list[dict[str, Any]] = []
    eligible_video_count = 0
    for asset in payload.get("assets") or ():
        if asset.get("media_type") != "video":
            continue
        modes = set(asset.get("usage_modes") or ())
        if not asset.get("auto_eligible", True) or not modes.intersection(
            {"full_screen_broll", "seam_broll"}
        ):
            continue
        eligible_video_count += 1
        taxonomy = asset.get("video_taxonomy")
        if not isinstance(taxonomy, dict):
            continue
        existing = [
            str(value)
            for value in taxonomy.get("fallback_concept_ids") or ()
            if str(value) not in POOL_IDS
        ]
        assigned = _pool_ids_for_asset(asset)
        taxonomy["fallback_concept_ids"] = existing + assigned
        if assigned:
            counts.update(assigned)
            assigned_assets.append(
                {
                    "asset_id": asset.get("asset_id"),
                    "name": asset.get("name"),
                    "pools": assigned,
                }
            )

    report = {
        "eligible_video_count": eligible_video_count,
        "assigned_video_count": len(assigned_assets),
        "unassigned_video_count": eligible_video_count - len(assigned_assets),
        "pool_counts": {pool_id: counts[pool_id] for pool_id in POOL_IDS},
        "assigned_assets": assigned_assets,
    }
    return payload, report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    payload = json.loads(args.catalog.read_text(encoding="utf-8"))
    migrated, report = migrate(payload)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.apply:
        args.catalog.write_text(
            json.dumps(migrated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({key: value for key, value in report.items() if key != "assigned_assets"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

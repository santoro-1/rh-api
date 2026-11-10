"""Import the reviewed 2026-08-14 B-roll batch into the semantic library."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


NEW_CONCEPTS = (
    {
        "concept_id": "scene.family_life",
        "label": "家庭亲子生活",
        "description": "明确展示家人、亲子或一家人共同活动的生活场景。",
        "aliases": ["家庭生活", "亲子时光", "一家人", "亲子陪伴"],
    },
    {
        "concept_id": "scene.home_sunlight",
        "label": "阳光居家场景",
        "description": "阳光照进客厅、卧室、餐厅或其他居家空间的画面。",
        "aliases": ["阳光洒进家里", "阳光照进家里", "阳光洒进房间", "阳光居家"],
    },
    {
        "concept_id": "object.clock_calendar",
        "label": "时钟日历",
        "description": "明确展示时钟走动、日历翻页或时间流逝意象。",
        "aliases": ["时钟", "钟表", "日历翻页", "日历"],
    },
    {
        "concept_id": "dish.fishball_greens_soup",
        "label": "鱼丸青菜汤",
        "description": "明确展示鱼丸与青菜搭配的汤品。",
        "aliases": ["鱼丸青菜汤", "青菜鱼丸汤", "鱼丸汤"],
    },
    {
        "concept_id": "dish.chive_scrambled_egg",
        "label": "韭黄炒鸡蛋",
        "description": "明确展示韭黄炒鸡蛋。",
        "aliases": ["韭黄炒鸡蛋", "韭黄鸡蛋"],
    },
)


def rule(match: str, name: str, description: str, concepts: list[str], l2: list[str], pools: list[str], *, actions: list[str] | None = None, person: str = "unknown", brand: str = "none", health: str = "none", platform: str = "none", auto: bool = True) -> dict[str, Any]:
    return {
        "match": match,
        "name": name,
        "description": description,
        "concepts": concepts,
        "l2": l2,
        "pools": pools,
        "actions": actions or [],
        "person": person,
        "brand": brand,
        "health": health,
        "platform": platform,
        "auto": auto,
    }


RULES = (
    rule("把对生活的热爱", "家常餐食制作", "连续展示居家烹饪和家常餐食", ["activity.home_cooking", "meal.feast"], ["l2.activity.food_prep", "l2.meal.general", "l2.scene.dining"], ["editorial.home_daily", "editorial.meal_daily"], actions=["cooking", "serving"], person="identifiable"),
    rule("今日份买菜", "超市买菜", "展示超市果蔬区域和日常采购", ["scene.vegetable_market", "food.vegetable"], ["l2.scene.market_public", "l2.food.vegetable"], ["editorial.meal_daily", "editorial.leisure_daily"], person="identifiable", brand="incidental"),
    rule("认识你们之前", "亲子陪伴", "展示亲子陪伴和家庭相处画面", ["scene.family_life"], ["l2.scene.social"], ["editorial.family_life", "editorial.mood_atmosphere"], person="identifiable"),
    rule("下午两点的家", "午后阳光客厅", "展示午后阳光照进客厅的居家空间", ["scene.home_sunlight", "scene.home"], ["l2.scene.home", "l2.scene.weather_season"], ["editorial.home_daily", "editorial.mood_atmosphere"]),
    rule("干净饮食", "鱼丸青菜汤与韭黄炒鸡蛋", "展示鱼丸青菜汤和韭黄炒鸡蛋制作", ["dish.fishball_greens_soup", "dish.chive_scrambled_egg", "food.egg"], ["l2.dish.soup", "l2.dish.prepared", "l2.food.egg"], ["editorial.meal_daily"], actions=["cooking"], person="unidentifiable", health="general_wellness", platform="embedded"),
    rule("好久没来和美超市", "超市蔬菜采购", "连续展示超市蔬菜陈列和买菜过程", ["scene.vegetable_market", "food.vegetable"], ["l2.scene.market_public", "l2.food.vegetable"], ["editorial.meal_daily", "editorial.leisure_daily"], person="identifiable", brand="incidental"),
    rule("记一个晴朗", "窗边阅读休息", "展示人物在明亮窗边阅读和休息", ["activity.reading", "scene.home"], ["l2.activity.self_care", "l2.activity.work_study", "l2.scene.home"], ["editorial.home_daily", "editorial.leisure_daily", "editorial.mood_atmosphere"], actions=["reading", "resting"], person="identifiable"),
    rule("今天闺蜜来我家吃饭", "居家聚餐做饭", "展示居家做饭和朋友聚餐", ["activity.home_cooking", "meal.feast", "scene.social"], ["l2.activity.food_prep", "l2.meal.general", "l2.scene.dining", "l2.scene.social"], ["editorial.home_daily", "editorial.meal_daily"], actions=["cooking", "serving", "eating"], person="identifiable"),
    rule("看着三口之家", "三口之家公园散步", "展示一家三口牵手在公园散步", ["scene.family_life", "activity.walking"], ["l2.activity.light_daily", "l2.scene.social", "l2.scene.nature"], ["editorial.leisure_daily", "editorial.family_life", "editorial.mood_atmosphere"], actions=["walking"], person="identifiable"),
    rule("常见的蔬菜陈列", "超市蔬菜陈列", "展示超市内多种新鲜蔬菜陈列", ["scene.vegetable_market", "food.vegetable"], ["l2.scene.market_public", "l2.food.vegetable"], ["editorial.meal_daily", "editorial.leisure_daily"], brand="incidental"),
    rule("【3】_好好吃饭", "素食家常餐", "展示清淡素食餐的制作和用餐", ["meal.light_meal", "food.vegetable"], ["l2.meal.light", "l2.meal.general", "l2.food.vegetable"], ["editorial.meal_daily"], actions=["cooking", "eating"], person="identifiable", health="general_wellness", platform="embedded"),
    rule("亲子唯美", "亲子户外陪伴", "展示亲子在户外风景中的陪伴画面", ["scene.family_life", "scene.nature"], ["l2.scene.social", "l2.scene.nature"], ["editorial.family_life", "editorial.mood_atmosphere"], person="identifiable"),
    rule("喜欢每一个有阳光", "阳光居家小屋", "展示阳光照进温暖居家空间", ["scene.home_sunlight", "scene.home"], ["l2.scene.home", "l2.scene.weather_season"], ["editorial.home_daily", "editorial.mood_atmosphere"]),
    rule("不喜欢外出", "阳光居家房间", "展示阳光下的卧室和居家空间", ["scene.home_sunlight", "scene.home"], ["l2.scene.home", "l2.scene.weather_season"], ["editorial.home_daily", "editorial.mood_atmosphere"]),
    rule("生活总是来来往往", "日常早餐餐桌", "展示早餐餐桌和早餐食物", ["meal.breakfast"], ["l2.meal.breakfast", "l2.meal.general", "l2.scene.dining"], ["editorial.meal_daily"]),
    rule("喜欢这样的小路", "绿荫小路散步", "展示人物沿绿色林荫小路慢走", ["activity.walking", "scene.nature"], ["l2.activity.light_daily", "l2.scene.nature"], ["editorial.leisure_daily", "editorial.mood_atmosphere"], actions=["walking"], person="identifiable"),
    rule("亲子晚餐", "亲子家常晚餐", "展示亲子晚餐制作和家常菜", ["scene.family_life", "meal.feast", "food.fish", "food.shrimp"], ["l2.scene.social", "l2.meal.general", "l2.activity.food_prep", "l2.food.meat_seafood"], ["editorial.meal_daily", "editorial.family_life"], actions=["cooking", "serving"], person="identifiable", platform="embedded"),
    rule("外面很冷", "冬日居家卧室", "展示冬日暖色卧室和居家氛围", ["scene.home"], ["l2.scene.home", "l2.scene.weather_season"], ["editorial.home_daily", "editorial.mood_atmosphere"]),
    rule("一个温柔闲适的午后", "湖边慢走休息", "展示中年女性在湖边慢走和休息", ["activity.walking", "scene.nature"], ["l2.activity.light_daily", "l2.activity.self_care", "l2.scene.nature"], ["editorial.leisure_daily", "editorial.mood_atmosphere"], actions=["walking", "resting"], person="identifiable", platform="embedded"),
    rule("【5】_总会有很多瞬间", "家庭餐食制作低清重复版", "与同批高清家庭餐食视频内容重复，保留仅供人工使用", ["scene.family_life", "meal.feast"], ["l2.scene.social", "l2.meal.general", "l2.activity.food_prep"], [], actions=["cooking", "serving"], person="identifiable", auto=False),
    rule("健康美味的丰盛家庭餐", "丰盛家庭餐", "连续展示家庭餐食制作和多人用餐", ["scene.family_life", "meal.feast"], ["l2.scene.social", "l2.meal.general", "l2.activity.food_prep", "l2.scene.dining"], ["editorial.meal_daily", "editorial.family_life"], actions=["cooking", "serving", "eating"], person="identifiable", health="general_wellness", platform="embedded"),
    rule("很喜欢这样，不紧不慢", "湖畔慢走", "展示人物沿湖边缓慢行走", ["activity.walking", "scene.nature"], ["l2.activity.light_daily", "l2.scene.nature"], ["editorial.leisure_daily", "editorial.mood_atmosphere"], actions=["walking"], person="identifiable"),
    rule("普通人的家", "清晨阳光居家", "展示清晨阳光照进居家空间", ["scene.home_sunlight", "scene.home"], ["l2.scene.home", "l2.scene.weather_season"], ["editorial.home_daily", "editorial.mood_atmosphere"]),
    rule("我的一日三餐", "一日三餐餐盘", "展示一日三餐搭配和餐盘准备", ["meal.light_meal"], ["l2.meal.light", "l2.meal.general", "l2.activity.food_prep"], ["editorial.meal_daily"], actions=["plating"], health="specific_claim", platform="embedded"),
    rule("喜欢一个人看风景", "湖边夕阳独处", "展示人物在湖边夕阳中慢走看风景", ["activity.walking", "scene.nature"], ["l2.activity.light_daily", "l2.scene.nature", "l2.scene.emotion"], ["editorial.leisure_daily", "editorial.mood_atmosphere"], actions=["walking"], person="identifiable"),
    rule("这世上不能复制的是时间", "时钟日历翻页", "展示时钟走动和日历翻页的时间意象", ["object.clock_calendar"], ["l2.object.daily", "l2.scene.emotion"], ["editorial.mood_atmosphere"]),
    rule("【7】_总会有很多瞬间", "家庭餐食制作", "展示家庭餐食制作和温暖用餐场景", ["scene.family_life", "meal.feast"], ["l2.scene.social", "l2.meal.general", "l2.activity.food_prep", "l2.scene.dining"], ["editorial.meal_daily", "editorial.family_life"], actions=["cooking", "serving", "eating"], person="identifiable"),
    rule("春日能量碗", "虾仁粗粮能量碗", "展示虾仁、粗粮饭和鸡蛋餐碗制作", ["food.shrimp", "food.whole_grain", "food.egg", "meal.light_meal"], ["l2.food.meat_seafood", "l2.food.staple", "l2.food.egg", "l2.meal.light"], ["editorial.meal_daily"], actions=["cooking", "plating"], health="general_wellness", platform="embedded"),
    rule("清晨漫步在南郊公园", "清晨公园林荫路", "展示清晨阳光下的公园林荫小路", ["scene.nature"], ["l2.scene.nature", "l2.scene.weather_season"], ["editorial.leisure_daily", "editorial.mood_atmosphere"]),
    rule("喜欢慢悠悠的日子", "自然环境喝茶休息", "展示人物在自然环境中喝茶休息", ["drink.tea", "scene.nature"], ["l2.drink.tea", "l2.activity.self_care", "l2.scene.nature"], ["editorial.leisure_daily", "editorial.mood_atmosphere"], actions=["drinking", "resting"], person="identifiable"),
    rule("第一视角沉浸式", "虾仁牛肉滑蛋藜麦饭", "第一视角制作虾仁、牛肉、鸡蛋和藜麦餐", ["food.shrimp", "food.beef", "food.egg", "food.quinoa"], ["l2.food.meat_seafood", "l2.food.egg", "l2.food.staple", "l2.activity.food_prep"], ["editorial.meal_daily"], actions=["cooking", "plating"], health="general_wellness", platform="embedded"),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(completed.stdout)
    stream = payload["streams"][0]
    duration_us = round(float(payload["format"]["duration"]) * 1_000_000)
    return {
        "duration_us": duration_us,
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "fps": str(stream.get("r_frame_rate") or ""),
    }


def matching_rule(filename: str) -> dict[str, Any] | None:
    matched = [item for item in RULES if item["match"] in filename]
    if len(matched) > 1:
        raise ValueError(f"multiple review rules matched {filename}")
    return matched[0] if matched else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    payload = json.loads(args.catalog.read_text(encoding="utf-8"))
    existing_names: set[str] = set()
    for asset in payload.get("assets") or ():
        resource = asset.get("resource") or {}
        metadata_rel = resource.get("metadata")
        if not metadata_rel:
            continue
        metadata_path = args.catalog.parent / str(metadata_rel)
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("original_name"):
                existing_names.add(str(metadata["original_name"]))

    source_files = sorted(args.source.glob("*.mp4"), key=lambda path: path.name)
    planned = [path for path in source_files if path.name not in existing_names]
    unmatched = [path.name for path in planned if matching_rule(path.name) is None]
    if unmatched:
        raise ValueError(f"missing review rules: {unmatched}")
    if planned and len(planned) != len(RULES):
        raise ValueError(f"expected {len(RULES)} new videos, found {len(planned)}")

    print(json.dumps({"source_count": len(source_files), "already_present": len(source_files) - len(planned), "planned_imports": len(planned), "auto_eligible": sum(bool(matching_rule(path.name)["auto"]) for path in planned), "manual_only": sum(not bool(matching_rule(path.name)["auto"]) for path in planned)}, ensure_ascii=False, indent=2))
    if not args.apply:
        return 0

    known_concepts = {str(item.get("concept_id") or "") for item in payload["concepts"]}
    for concept in NEW_CONCEPTS:
        if concept["concept_id"] not in known_concepts:
            payload["concepts"].append(dict(concept))
            known_concepts.add(concept["concept_id"])

    videos_root = args.catalog.parent / "videos"
    new_assets: list[dict[str, Any]] = []
    for index, source_path in enumerate(planned, start=1):
        review = matching_rule(source_path.name)
        assert review is not None
        missing_concepts = set(review["concepts"]) - known_concepts
        if missing_concepts:
            raise ValueError(f"unknown concepts for {source_path.name}: {sorted(missing_concepts)}")
        digest = sha256(source_path)
        asset_id = f"broll.20260814.{index:03d}.video.{digest[:10]}"
        folder_name = asset_id.replace(".", "_")
        target_dir = videos_root / folder_name
        if target_dir.exists():
            raise FileExistsError(target_dir)
        media = probe(source_path)
        target_dir.mkdir(parents=True)
        target_video = target_dir / "video.mp4"
        target_poster = target_dir / "poster.png"
        shutil.copy2(source_path, target_video)
        poster_time = min(1.0, max(0.1, media["duration_us"] / 3_000_000))
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{poster_time:.3f}", "-i", str(source_path), "-frames:v", "1", "-vf", "scale=540:-2", "-y", str(target_poster)],
            check=True,
        )
        metadata = {
            "schema": "jyd.semantic-video-metadata.v1",
            "review_id": f"BROLL-20260814-{index:03d}",
            "original_name": source_path.name,
            "source_sha256": digest,
            "fps": media["fps"],
            "duration_us": media["duration_us"],
            "width": media["width"],
            "height": media["height"],
            "has_audio": True,
            "review_decision": "APPROVE_DIRECT" if review["auto"] else "MANUAL_ONLY_DUPLICATE",
            "approved_source_start_us": 0,
            "approved_source_end_us": media["duration_us"],
            "review_notes": review["description"],
        }
        (target_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        modes = ["full_screen_broll", "seam_broll"] if review["auto"] else ["manual_only"]
        fallback_ids = list(review["pools"]) if review["auto"] else []
        trigger_basis = {concept_id: "complete_scene" for concept_id in review["concepts"]}
        l1 = ["l1.daily_life_scene"]
        if any(value.startswith(("l2.food.", "l2.dish.", "l2.meal.", "l2.drink.")) for value in review["l2"]):
            l1.insert(0, "l1.food_drink")
        if any(value.startswith("l2.activity.") for value in review["l2"]):
            l1.insert(0, "l1.activity_wellness")
        resource_base = f"videos/{folder_name}"
        new_assets.append({
            "asset_id": asset_id,
            "concept_ids": list(review["concepts"]),
            "name": review["name"],
            "description": review["description"],
            "media_type": "video",
            "renderer": "video_overlay",
            "tags": ["人工逐条审片", "网络素材", "空镜专用", "视频"],
            "resource": {"video": f"{resource_base}/video.mp4", "preview": f"{resource_base}/poster.png", "metadata": f"{resource_base}/metadata.json", "duration_us": media["duration_us"], "width": media["width"], "height": media["height"], "has_audio": True},
            "defaults": {"corner": "center", "scale": 1.0, "opacity": 1.0, "duration_us": min(5_000_000, media["duration_us"]), "source_start_us": 0, "mute": True, "loop": bool(review["auto"]), "fit": "cover"},
            "semantic_roles": {"depicts": list(review["concepts"]), "expresses": [], "related": []},
            "auto_trigger_concept_ids": list(review["concepts"]),
            "trigger_basis": trigger_basis,
            "visual_actions": list(review["actions"]),
            "usage_modes": modes,
            "cleanliness_grade": "B",
            "auto_eligible": bool(review["auto"]),
            "requires_clip": False,
            "loop_allowed": bool(review["auto"]),
            "rights_status": "attributed",
            "person_status": review["person"],
            "brand_status": review["brand"],
            "health_claim_status": review["health"],
            "platform_ui_status": review["platform"],
            "video_taxonomy": {"l1_domain_ids": list(dict.fromkeys(l1)), "l2_category_ids": list(review["l2"]), "l3_exact_concept_ids": list(review["concepts"]), "action_ids": list(review["actions"]), "scene_ids": [value for value in review["l2"] if value.startswith("l2.scene.")], "fallback_concept_ids": fallback_ids, "fallback_policy": "video_only_explicit_whitelist", "review_status": "CODEX_REVIEWED_EDITORIAL_V1"},
        })

    payload["assets"].extend(new_assets)
    args.catalog.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"imported {len(new_assets)} reviewed videos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

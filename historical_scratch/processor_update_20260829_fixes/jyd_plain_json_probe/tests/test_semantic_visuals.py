from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import json
import shutil

import pytest

from jyd_probe.semantic_subtitles import SemanticSubtitleMappingError
from jyd_probe.semantic_visual_migration import (
    SemanticVisualMigrationError,
    apply_migration,
    file_sha256,
    rollback_migration,
    validate_migration,
)
from jyd_probe.semantic_visuals import (
    _assets_for_media_policy,
    CATALOG_SCHEMA_V1,
    CATALOG_SCHEMA_V2,
    CATALOG_SCHEMA_V3,
    RECIPE_SCHEMA_V2,
    build_visual_recipe,
    frozen_visual_overlays,
    load_semantic_visual_catalog,
    map_visual_candidates_to_raw_cues,
    recall_semantic_visual_candidates,
    SemanticVisualCatalog,
    SemanticVisualCatalogError,
    validate_visual_occupancy,
    visual_overlay_conflicts,
)


CATALOG_ROOT = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "libraries"
    / "semantic_visual_library"
)


@lru_cache(maxsize=1)
def _catalog():
    return load_semantic_visual_catalog(CATALOG_ROOT)


def _catalog_with_editorial_broll() -> SemanticVisualCatalog:
    catalog = _catalog()
    pool_ids = (
        "editorial.home_daily",
        "editorial.meal_daily",
        "editorial.leisure_daily",
        "editorial.family_life",
        "editorial.mood_atmosphere",
    )
    concepts = tuple(catalog.concepts) + tuple(
        {
            "concept_id": concept_id,
            "label": concept_id,
            "description": f"测试空镜池 {concept_id}",
            "aliases": [concept_id],
        }
        for concept_id in pool_ids
    )
    base_video = next(
        asset
        for asset in catalog.assets
        if asset.get("media_type") == "video"
        and "full_screen_broll" in set(asset.get("usage_modes", ()))
        and "seam_broll" in set(asset.get("usage_modes", ()))
    )
    video = json.loads(json.dumps(base_video))
    video["asset_id"] = "editorial.test.video.01"
    video.setdefault("video_taxonomy", {})["fallback_concept_ids"] = list(pool_ids)
    return SemanticVisualCatalog(
        root=catalog.root,
        schema=catalog.schema,
        library_id=catalog.library_id,
        catalog_version="editorial-test",
        concepts=concepts,
        assets=(video,),
    )


def _write_v2_catalog(root: Path) -> Path:
    bundle = root / "bundles" / "beef_image_01"
    shutil.copytree(CATALOG_ROOT / "bundles" / "egg_boiled", bundle)
    videos = root / "videos" / "beef_cooking_01"
    videos.mkdir(parents=True)
    (videos / "video.mp4").write_bytes(b"fake-mp4-for-catalog-loader")
    shutil.copy2(
        bundle / "resources" / "sticker" / "singleImage.png",
        videos / "poster.png",
    )
    (videos / "metadata.json").write_text(
        json.dumps({"source": "test"}), encoding="utf-8"
    )
    payload = {
        "schema": CATALOG_SCHEMA_V2,
        "library_id": "jyd.semantic-visual-library.test",
        "concepts": [
            {
                "concept_id": "food.beef",
                "label": "牛肉",
                "description": "作为食物或食材出现的牛肉",
                "aliases": ["瘦牛肉", "牛肉"],
            },
            {
                "concept_id": "meal.breakfast",
                "label": "早餐",
                "description": "明确作为早餐出现的餐食",
                "aliases": ["早餐"],
            },
        ],
        "assets": [
            {
                "asset_id": "beef.image.01",
                "concept_ids": ["food.beef", "meal.breakfast"],
                "name": "牛肉图片",
                "description": "一盘熟牛肉",
                "media_type": "image",
                "renderer": "jyd_sticker_bundle",
                "tags": ["熟食", "照片"],
                "resource": {
                    "bundle": "bundles/beef_image_01",
                    "preview": "bundles/beef_image_01/resources/sticker/singleImage.png",
                },
                "defaults": {
                    "corner": "top_left",
                    "scale": 0.26,
                    "opacity": 1.0,
                    "duration_us": 1_800_000,
                },
            },
            {
                "asset_id": "beef.video.01",
                "concept_ids": ["food.beef"],
                "name": "牛肉烹饪视频",
                "description": "牛肉烹饪过程近景",
                "media_type": "video",
                "renderer": "video_overlay",
                "tags": ["烹饪过程", "动态"],
                "resource": {
                    "video": "videos/beef_cooking_01/video.mp4",
                    "preview": "videos/beef_cooking_01/poster.png",
                    "metadata": "videos/beef_cooking_01/metadata.json",
                    "duration_us": 6_200_000,
                    "width": 1920,
                    "height": 1080,
                    "has_audio": True,
                },
                "defaults": {
                    "corner": "top_right",
                    "scale": 0.32,
                    "opacity": 1.0,
                    "duration_us": 3_000_000,
                    "source_start_us": 0,
                    "mute": True,
                    "loop": False,
                    "fit": "cover",
                },
            },
        ],
    }
    manifest = root / "catalog.json"
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return manifest


def _upgrade_test_catalog_to_v3(manifest: Path) -> dict:
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["schema"] = CATALOG_SCHEMA_V3
    image, video = payload["assets"]
    image.update(
        {
            "semantic_roles": {
                "depicts": ["food.beef"],
                "expresses": ["meal.breakfast"],
                "related": [],
            },
            "auto_trigger_concept_ids": ["food.beef", "meal.breakfast"],
            "trigger_basis": {
                "food.beef": "exact_subject",
                "meal.breakfast": "complete_scene",
            },
            "visual_actions": [],
            "usage_modes": ["semantic_overlay", "list_quick_cut"],
            "cleanliness_grade": "A",
            "auto_eligible": True,
            "requires_clip": False,
            "loop_allowed": False,
            "rights_status": "internal",
            "person_status": "none",
            "brand_status": "none",
            "health_claim_status": "none",
            "platform_ui_status": "none",
        }
    )
    video.update(
        {
            "semantic_roles": {
                "depicts": ["food.beef"],
                "expresses": [],
                "related": ["meal.breakfast"],
            },
            "auto_trigger_concept_ids": ["food.beef"],
            "trigger_basis": {"food.beef": "exact_subject"},
            "visual_actions": ["cooking"],
            "usage_modes": ["full_screen_broll", "seam_broll"],
            "cleanliness_grade": "A",
            "auto_eligible": True,
            "requires_clip": False,
            "loop_allowed": True,
            "rights_status": "cleared",
            "person_status": "none",
            "brand_status": "none",
            "health_claim_status": "none",
            "platform_ui_status": "none",
        }
    )
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def _add_video_taxonomy_fixture(
    manifest: Path, *, include_exact_light_activity: bool = False
) -> dict:
    payload = _upgrade_test_catalog_to_v3(manifest)
    light_concept = {
        "concept_id": "activity.light_daily",
        "label": "日常轻活动",
        "description": "步行或轻柔活动等低强度日常身体活动",
        "aliases": ["日常轻活动", "轻活动"],
    }
    payload["concepts"].append(light_concept)
    video = payload["assets"][1]
    video["video_taxonomy"] = {
        "l1_domain_ids": ["l1.food_drink", "l1.activity_wellness"],
        "l2_category_ids": ["l2.food.meat_seafood", "l2.activity.light_daily"],
        "l3_exact_concept_ids": ["food.beef"],
        "action_ids": ["cooking"],
        "scene_ids": [],
        "fallback_concept_ids": ["activity.light_daily"],
        "fallback_policy": "video_only_explicit_whitelist",
        "review_status": "TEST_REVIEWED_V1",
    }
    if include_exact_light_activity:
        exact_video = json.loads(json.dumps(video))
        exact_video.update(
            {
                "asset_id": "light.activity.video.exact.01",
                "concept_ids": ["activity.light_daily"],
                "name": "日常步行精确视频",
                "semantic_roles": {
                    "depicts": ["activity.light_daily"],
                    "expresses": [],
                    "related": [],
                },
                "auto_trigger_concept_ids": ["activity.light_daily"],
                "trigger_basis": {"activity.light_daily": "exact_subject"},
                "visual_actions": ["walking"],
                "video_taxonomy": {
                    "l1_domain_ids": ["l1.activity_wellness"],
                    "l2_category_ids": ["l2.activity.light_daily"],
                    "l3_exact_concept_ids": ["activity.light_daily"],
                    "action_ids": ["walking"],
                    "scene_ids": [],
                    "fallback_concept_ids": ["activity.light_daily"],
                    "fallback_policy": "video_only_explicit_whitelist",
                    "review_status": "TEST_REVIEWED_V1",
                },
            }
        )
        payload["assets"].append(exact_video)
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def _write_test_migration(root: Path) -> Path:
    catalog_path = _write_v2_catalog(root)
    backup_path = root / "catalog.v2.backup.json"
    backup_path.write_bytes(catalog_path.read_bytes())
    candidate_path = root / "catalog.v3.candidate.json"
    candidate_payload = _upgrade_test_catalog_to_v3(catalog_path)
    candidate_path.write_text(
        json.dumps(candidate_payload, ensure_ascii=False), encoding="utf-8"
    )
    catalog_path.write_bytes(backup_path.read_bytes())
    manifest_path = root / "migration.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "jyd.semantic-visual-catalog-migration.v1",
                "source_catalog_path": str(catalog_path),
                "source_catalog_schema": CATALOG_SCHEMA_V2,
                "source_catalog_sha256": file_sha256(catalog_path),
                "source_backup_path": str(backup_path),
                "source_backup_sha256": file_sha256(backup_path),
                "candidate_path": str(candidate_path),
                "candidate_schema": CATALOG_SCHEMA_V3,
                "candidate_sha256": file_sha256(candidate_path),
                "asset_count": 2,
                "approval": {
                    "status": "approved",
                    "approved_by": "test-reviewer",
                    "approved_at": "2026-08-13T00:00:00+08:00",
                },
                "rollback": {
                    "required_current_sha256": file_sha256(candidate_path),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return manifest_path


def _write_protein_boundary_catalog(root: Path, *, include_guide: bool) -> Path:
    manifest = _write_v2_catalog(root)
    payload = _upgrade_test_catalog_to_v3(manifest)
    payload["concepts"] = [
        {
            "concept_id": "food.fish",
            "label": "鱼",
            "description": "画面直接出现的鱼类食物",
            "aliases": ["鱼肉", "鱼"],
        },
        {
            "concept_id": "nutrition.protein",
            "label": "蛋白质",
            "description": "明确的蛋白质集合或知识表达",
            "aliases": ["优质蛋白", "蛋白质"],
        },
    ]
    fish, guide = payload["assets"]
    fish.update(
        {
            "asset_id": "fish.image.01",
            "concept_ids": ["food.fish"],
            "name": "清蒸鱼",
            "description": "单独一盘清蒸鱼",
            "semantic_roles": {
                "depicts": ["food.fish"],
                "expresses": [],
                "related": ["nutrition.protein"],
            },
            "auto_trigger_concept_ids": ["food.fish"],
            "trigger_basis": {"food.fish": "exact_subject"},
        }
    )
    guide.update(
        {
            "asset_id": "protein.guide.video.01",
            "concept_ids": ["nutrition.protein"],
            "name": "蛋白质来源指南",
            "description": "多种蛋白质来源的知识画面",
            "semantic_roles": {
                "depicts": [],
                "expresses": ["nutrition.protein"],
                "related": [],
            },
            "auto_trigger_concept_ids": ["nutrition.protein"],
            "trigger_basis": {"nutrition.protein": "infographic"},
            "visual_actions": [],
            "usage_modes": ["knowledge_card"],
            "loop_allowed": False,
        }
    )
    if not include_guide:
        payload["assets"] = [fish]
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return manifest


def test_catalog_contains_images_and_registered_activity_videos() -> None:
    catalog = _catalog()

    assert catalog.schema in {CATALOG_SCHEMA_V2, CATALOG_SCHEMA_V3}
    assert catalog.library_id == "jyd.semantic-visual-library.default"
    assert len(catalog.concepts) >= 37
    assert len(catalog.assets) >= 40
    assert len([item for item in catalog.assets if item["media_type"] == "image"]) >= 38
    assert len([item for item in catalog.assets if item["media_type"] == "video"]) >= 2
    assert len([item for item in catalog.assets if "food.egg" in item["concept_ids"]]) >= 2
    assert all(Path(item["image_path"]).is_file() for item in catalog.assets)
    if catalog.schema == CATALOG_SCHEMA_V3:
        editorial_assets = [
            item
            for item in catalog.assets
            if any(
                str(concept_id).startswith("editorial.")
                for concept_id in item.get("video_taxonomy", {}).get(
                    "fallback_concept_ids", ()
                )
            )
        ]
        assert len(editorial_assets) == 99
        assert all("seam_broll" in item["usage_modes"] for item in editorial_assets)
        assert all(
            "full_screen_broll" not in item["usage_modes"]
            for item in editorial_assets
        )

    reviewed = [item for item in catalog.assets if item["asset_id"].startswith("review.")]
    if not reviewed:
        return

    assert len(reviewed) == 1169
    assert len([item for item in reviewed if item["media_type"] == "image"]) == 737
    assert len([item for item in reviewed if item["media_type"] == "video"]) == 432
    assert [
        item["asset_id"]
        for item in _assets_for_media_policy(
            catalog, "nutrition.protein", "mixed", usage="explicit"
        )
    ] == ["protein.food_guide.01"]
    assert len(
        _assets_for_media_policy(
            catalog, "activity.light_daily", "mixed", usage="seam_broll"
        )
    ) == 14
    light_activity = recall_semantic_visual_candidates(
        "每天保持日常轻活动，比如散步或者八段锦。", catalog=catalog
    )
    assert {
        concept["concept_id"]
        for candidate in light_activity["candidates"]
        for concept in candidate["allowed_concepts"]
    } >= {
        "activity.light_daily",
        "activity.walking",
    }
    rapid_list = recall_semantic_visual_candidates(
        "第一，早餐可以吃鸡蛋、玉米、红薯。", catalog=catalog
    )
    assert {
        concept["concept_id"]
        for candidate in rapid_list["candidates"]
        for concept in candidate["allowed_concepts"]
    } >= {
        "meal.breakfast",
        "food.egg",
        "food.corn",
        "food.sweet_potato",
    }


def test_v3_video_taxonomy_is_video_only_and_l2_fallback_is_controlled(
    tmp_path: Path,
) -> None:
    root = tmp_path / "video-taxonomy"
    manifest = _write_v2_catalog(root)
    _add_video_taxonomy_fixture(manifest)
    catalog = load_semantic_visual_catalog(root)

    image = catalog.asset("beef.image.01")
    video = catalog.asset("beef.video.01")
    assert image is not None and "video_taxonomy" not in image
    assert video is not None
    assert video["video_taxonomy"]["l1_domain_ids"] == [
        "l1.food_drink",
        "l1.activity_wellness",
    ]
    assert video["video_taxonomy"]["fallback_concept_ids"] == [
        "activity.light_daily"
    ]
    assert _assets_for_media_policy(
        catalog, "activity.light_daily", "image_only", usage="explicit"
    ) == []
    assert [
        item["asset_id"]
        for item in _assets_for_media_policy(
            catalog, "activity.light_daily", "mixed", usage="explicit"
        )
    ] == ["beef.video.01"]
    assert [
        item["asset_id"]
        for item in _assets_for_media_policy(
            catalog, "activity.light_daily", "video_only", usage="seam_broll"
        )
    ] == ["beef.video.01"]


def test_v3_exact_video_precedes_video_l2_fallback(tmp_path: Path) -> None:
    root = tmp_path / "video-taxonomy-exact-first"
    manifest = _write_v2_catalog(root)
    _add_video_taxonomy_fixture(manifest, include_exact_light_activity=True)
    catalog = load_semantic_visual_catalog(root)

    assets = _assets_for_media_policy(
        catalog, "activity.light_daily", "video_only", usage="seam_broll"
    )
    assert [item["asset_id"] for item in assets] == [
        "light.activity.video.exact.01",
        "beef.video.01",
    ]
    candidate = recall_semantic_visual_candidates("保持日常轻活动。", catalog)[
        "candidates"
    ][0]
    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[
            {
                **candidate,
                "start_us": 2_000_000,
                "duration_us": 3_000_000,
                "phrase_char_start": 0,
                "phrase_char_end": 9,
                "phrase_text": "保持日常轻活动。",
                "usage": "seam_broll",
                "direct_concept_ids": ["activity.light_daily"],
                "segment_boundary_us": 2_000_000,
            }
        ],
        decisions=[
            {
                "candidate_id": candidate["candidate_id"],
                "decision": "SHOW",
                "concept_id": "activity.light_daily",
                "priority": 2,
                "usage": "seam_broll",
                "confidence": 0.95,
                "reason_code": "LITERAL_CONCRETE_OBJECT",
            }
        ],
        media_policy="video_only",
        segment_boundaries=[{"boundary_us": 2_000_000}],
        final_video_duration_us=6_000_000,
    )
    assert recipe["overlays"][0]["asset_id"] == "light.activity.video.exact.01"
    assert recipe["overlays"][0]["timing_mode"] == "seam_broll"


def test_v3_video_taxonomy_rejects_image_or_food_category_fallback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "invalid-video-taxonomy"
    manifest = _write_v2_catalog(root)
    payload = _add_video_taxonomy_fixture(manifest)
    payload["assets"][0]["video_taxonomy"] = json.loads(
        json.dumps(payload["assets"][1]["video_taxonomy"])
    )
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SemanticVisualCatalogError, match="image cannot declare"):
        load_semantic_visual_catalog(root)

    root_second = tmp_path / "invalid-food-fallback"
    manifest_second = _write_v2_catalog(root_second)
    payload = _add_video_taxonomy_fixture(manifest_second)
    payload["assets"][1]["video_taxonomy"]["fallback_concept_ids"] = [
        "food.beef"
    ]
    manifest_second.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(SemanticVisualCatalogError, match="food/drink/nutrition"):
        load_semantic_visual_catalog(root_second)


def test_generic_staple_food_does_not_recall_quality_carbohydrate() -> None:
    catalog = _catalog()
    payload = recall_semantic_visual_candidates(
        "平时吃的精米白面替换三分之一成粗粮，比如喝杂粮粥。",
        catalog,
    )

    assert [item["text"] for item in payload["candidates"]] == ["粗粮", "杂粮粥"]
    assert {
        concept["concept_id"]
        for item in payload["candidates"]
        for concept in item["allowed_concepts"]
    } == {"food.whole_grain"}
    assert recall_semantic_visual_candidates("不要完全不吃主食", catalog)["candidates"] == []
    assert all(
        item["renderer"]
        == ("jyd_sticker_bundle" if item["media_type"] == "image" else "video_overlay")
        for item in catalog.assets
    )
    if catalog.schema == CATALOG_SCHEMA_V3:
        assert all(
            item["concept_ids"] == item["auto_trigger_concept_ids"]
            for item in catalog.assets
        )
    else:
        assert all(item["concept_ids"] == [item["concept_id"]] for item in catalog.assets)
    assert catalog.asset("protein.food_guide.01") is not None
    assert catalog.asset("fruit.platter.01") is not None
    assert catalog.asset("water.warm_glass.01") is not None
    assert catalog.asset("activity.walking.01") is not None
    assert catalog.asset("activity.aerobic.crotch_clap.video.01") is not None
    assert catalog.asset("activity.aerobic.core_broll.video.01") is not None


def test_approved_v3_review_terms_stay_narrow_and_dish_specific() -> None:
    catalog = _catalog()
    if catalog.concept("dish.tofu_soup") is None:
        pytest.skip("requires the separately distributed expanded local material catalog")

    def allowed(script: str) -> set[str]:
        return {
            concept["concept_id"]
            for candidate in recall_semantic_visual_candidates(script, catalog)["candidates"]
            for concept in candidate["allowed_concepts"]
        }

    assert allowed("豆腐汤") == {"dish.tofu_soup"}
    assert allowed("西兰花汤") == {"dish.broccoli_soup"}
    assert allowed("冬瓜汤") == {"dish.winter_melon_soup"}
    assert recall_semantic_visual_candidates("喝点汤", catalog)["candidates"] == []
    assert allowed("紫薯") == {"food.purple_sweet_potato"}
    assert allowed("红薯") == {"food.sweet_potato"}
    assert allowed("小白菜") == {"food.bok_choy"}
    assert allowed("油菜") == {"food.rapeseed_greens"}
    assert allowed("麻团") == {"food.sesame_ball"}
    assert allowed("消化不好") == {"health.digestion_problem"}
    assert allowed("减脂餐") == {"meal.weight_loss"}

    tofu_soup_ids = {
        item["asset_id"]
        for item in _assets_for_media_policy(
            catalog, "dish.tofu_soup", "mixed", usage="explicit"
        )
    }
    plain_tofu_ids = {
        item["asset_id"]
        for item in _assets_for_media_policy(
            catalog, "food.tofu", "mixed", usage="explicit"
        )
    }
    assert tofu_soup_ids
    assert tofu_soup_ids.isdisjoint(plain_tofu_ids)


def test_extended_food_and_activity_aliases_recall_specific_concepts() -> None:
    catalog = load_semantic_visual_catalog(CATALOG_ROOT)
    payload = recall_semantic_visual_candidates(
        "早餐吃全麦面包和酸奶，午餐有米饭、西红柿和三文鱼，晚上快走，雨天用跑步机。",
        catalog,
    )

    concept_by_text = {
        item["text"]: item["allowed_concepts"][0]["concept_id"]
        for item in payload["candidates"]
    }
    assert concept_by_text["全麦面包"] == "food.whole_wheat_bread"
    assert concept_by_text["酸奶"] == "food.yogurt"
    assert concept_by_text["米饭"] == "food.rice"
    assert concept_by_text["西红柿"] == "food.tomato"
    assert concept_by_text["三文鱼"] == "food.salmon"
    assert concept_by_text["快走"] == "activity.walking"
    assert concept_by_text["跑步机"] == "activity.treadmill"


def test_v2_catalog_loads_unified_image_and_reserved_video_assets(tmp_path: Path) -> None:
    root = tmp_path / "catalog-v2"
    _write_v2_catalog(root)

    catalog = load_semantic_visual_catalog(root)

    assert catalog.schema == CATALOG_SCHEMA_V2
    assert catalog.library_id == "jyd.semantic-visual-library.test"
    assert len(catalog.concepts) == 2
    image = catalog.asset("beef.image.01")
    video = catalog.asset("beef.video.01")
    assert image is not None and image["concept_ids"] == ["food.beef", "meal.breakfast"]
    assert image["media_type"] == "image"
    assert image["defaults"]["corner"] == "bottom_center"
    assert image["defaults"]["scale"] == 0.56
    assert video is not None and video["media_type"] == "video"
    assert video["renderer"] == "video_overlay"
    assert video["defaults"]["corner"] == "bottom_center"
    assert video["defaults"]["scale"] == 0.615
    assert video["defaults"]["mute"] is True
    public = catalog.public_payload()
    assert public["schema"] == CATALOG_SCHEMA_V2
    assert public["library_id"] == catalog.library_id
    assert all("resource_path" not in item for item in public["assets"])
    assert all("preview_path" not in item for item in public["assets"])
    assert all("bundle_path" not in item for item in public["assets"])
    assert all("image_path" not in item for item in public["assets"])


def test_v2_catalog_rejects_path_escape_and_renderer_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "catalog-v2"
    manifest = _write_v2_catalog(root)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["assets"][1]["resource"]["video"] = "../outside.mp4"
    (tmp_path / "outside.mp4").write_bytes(b"outside")
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SemanticVisualCatalogError):
        load_semantic_visual_catalog(root)

    shutil.rmtree(root)
    manifest = _write_v2_catalog(root)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["assets"][0]["renderer"] = "video_overlay"
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SemanticVisualCatalogError):
        load_semantic_visual_catalog(root)


def test_v2_catalog_version_changes_with_video_bytes(tmp_path: Path) -> None:
    root = tmp_path / "catalog-v2"
    _write_v2_catalog(root)
    before = load_semantic_visual_catalog(root).catalog_version
    video_path = root / "videos" / "beef_cooking_01" / "video.mp4"
    video_path.write_bytes(video_path.read_bytes() + b"changed")

    assert load_semantic_visual_catalog(root).catalog_version != before


def test_v3_catalog_loads_explicit_roles_and_uses_usage_modes_for_broll(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog-v3"
    manifest = _write_v2_catalog(root)
    _upgrade_test_catalog_to_v3(manifest)

    catalog = load_semantic_visual_catalog(root)

    assert catalog.schema == CATALOG_SCHEMA_V3
    image = catalog.asset("beef.image.01")
    video = catalog.asset("beef.video.01")
    assert image is not None
    assert image["concept_ids"] == image["auto_trigger_concept_ids"]
    assert image["semantic_roles"]["expresses"] == ["meal.breakfast"]
    assert image["trigger_basis"]["meal.breakfast"] == "complete_scene"
    assert video is not None
    assert video["usage_modes"] == ["full_screen_broll", "seam_broll"]
    assert video["defaults"]["corner"] == "center"
    assert [
        item["asset_id"]
        for item in _assets_for_media_policy(
            catalog, "food.beef", "mixed", usage="enrichment"
        )
    ] == ["beef.video.01"]


def test_v3_attributed_rights_allow_auto_broll_and_emit_source_label(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog-v3-attributed"
    manifest = _write_v2_catalog(root)
    payload = _upgrade_test_catalog_to_v3(manifest)
    for asset in payload["assets"]:
        asset["rights_status"] = "attributed"
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    catalog = load_semantic_visual_catalog(root)
    candidate = recall_semantic_visual_candidates("牛肉", catalog)["candidates"][0]

    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[{**candidate, "start_us": 0, "duration_us": 3_000_000}],
        decisions=[
            {
                "candidate_id": candidate["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.beef",
                "importance": 0.9,
                "confidence": 0.95,
                "reason_code": "LITERAL_CONCRETE_OBJECT",
            }
        ],
        media_policy="image_only",
    )

    overlay = recipe["overlays"][0]
    assert overlay["rights_status"] == "attributed"
    assert overlay["attribution_text"] == "素材来源于网络"
    assert [
        item["asset_id"]
        for item in _assets_for_media_policy(
            catalog, "food.beef", "mixed", usage="explicit"
        )
    ] == ["beef.image.01"]

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["assets"][1]["usage_modes"] = ["seam_broll"]
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    seam_only = load_semantic_visual_catalog(root)
    assert _assets_for_media_policy(
        seam_only, "food.beef", "mixed", usage="enrichment"
    ) == []
    assert [
        item["asset_id"]
        for item in _assets_for_media_policy(
            seam_only, "food.beef", "mixed", usage="seam_broll"
        )
    ] == ["beef.video.01"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda asset: asset.update(auto_trigger_concept_ids=[]),
            "concept_ids must equal auto_trigger_concept_ids",
        ),
        (
            lambda asset: asset.update(
                semantic_roles={
                    "depicts": [],
                    "expresses": [],
                    "related": ["food.beef", "meal.breakfast"],
                }
            ),
            "auto triggers must come from depicts or expresses",
        ),
        (
            lambda asset: asset.update(rights_status="unknown"),
            "unknown or restricted rights cannot auto full-screen",
        ),
    ],
)
def test_v3_catalog_rejects_unsafe_or_inconsistent_auto_relations(
    tmp_path: Path, mutate, message: str
) -> None:
    root = tmp_path / "catalog-v3-invalid"
    manifest = _write_v2_catalog(root)
    payload = _upgrade_test_catalog_to_v3(manifest)
    mutate(payload["assets"][1])
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SemanticVisualCatalogError, match=message):
        load_semantic_visual_catalog(root)


def test_v3_auto_eligible_false_asset_is_never_recalled_or_selected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog-v3-disabled"
    manifest = _write_v2_catalog(root)
    payload = _upgrade_test_catalog_to_v3(manifest)
    for asset in payload["assets"]:
        asset["auto_eligible"] = False
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    catalog = load_semantic_visual_catalog(root)

    assert _assets_for_media_policy(catalog, "food.beef", "mixed") == []
    assert _assets_for_media_policy(
        catalog, "food.beef", "mixed", usage="enrichment"
    ) == []
    assert recall_semantic_visual_candidates("早餐吃牛肉", catalog)["candidates"] == []


def test_v3_manual_only_asset_can_keep_roles_with_empty_auto_concepts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog-v3-manual-only"
    manifest = _write_v2_catalog(root)
    payload = _upgrade_test_catalog_to_v3(manifest)
    manual = payload["assets"][0]
    manual["concept_ids"] = []
    manual["auto_trigger_concept_ids"] = []
    manual["trigger_basis"] = {}
    manual["usage_modes"] = ["manual_only"]
    manual["auto_eligible"] = False
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    catalog = load_semantic_visual_catalog(root)

    asset = catalog.asset("beef.image.01")
    assert asset is not None and asset["concept_ids"] == []
    assert asset["semantic_roles"]["depicts"] == ["food.beef"]
    assert catalog.concept("meal.breakfast") is not None


def test_v3_migration_apply_and_rollback_are_hash_guarded(tmp_path: Path) -> None:
    root = tmp_path / "catalog-migration"
    manifest_path = _write_test_migration(root)
    catalog_path = root / "catalog.json"

    assert validate_migration(manifest_path)["asset_count"] == 2
    apply_migration(manifest_path)
    assert load_semantic_visual_catalog(root).schema == CATALOG_SCHEMA_V3
    rollback_migration(manifest_path)
    assert load_semantic_visual_catalog(root).schema == CATALOG_SCHEMA_V2


def test_v3_migration_refuses_changed_source_and_candidate(tmp_path: Path) -> None:
    root = tmp_path / "catalog-migration-guard"
    manifest_path = _write_test_migration(root)
    catalog_path = root / "catalog.json"
    candidate_path = root / "catalog.v3.candidate.json"

    catalog_path.write_bytes(catalog_path.read_bytes() + b"\n")
    with pytest.raises(SemanticVisualMigrationError, match="current source catalog hash mismatch"):
        apply_migration(manifest_path)

    catalog_path.write_bytes((root / "catalog.v2.backup.json").read_bytes())
    candidate_path.write_bytes(candidate_path.read_bytes() + b"\n")
    with pytest.raises(SemanticVisualMigrationError, match="v3 candidate hash mismatch"):
        validate_migration(manifest_path)


def test_v3_migration_refuses_apply_without_explicit_approval(tmp_path: Path) -> None:
    root = tmp_path / "catalog-migration-pending"
    manifest_path = _write_test_migration(root)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["approval"] = {
        "status": "pending",
        "approved_by": None,
        "approved_at": None,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    assert validate_migration(manifest_path)["approval"]["status"] == "pending"
    with pytest.raises(SemanticVisualMigrationError, match="requires explicit"):
        apply_migration(manifest_path)


def test_v3_protein_concept_selects_guide_and_never_related_fish(
    tmp_path: Path,
) -> None:
    root = tmp_path / "protein-boundary"
    _write_protein_boundary_catalog(root, include_guide=True)
    catalog = load_semantic_visual_catalog(root)

    recalled = recall_semantic_visual_candidates("注意补充蛋白质", catalog)
    assert [
        concept["concept_id"]
        for candidate in recalled["candidates"]
        for concept in candidate["allowed_concepts"]
    ] == ["nutrition.protein"]
    assert [
        asset["asset_id"]
        for asset in _assets_for_media_policy(
            catalog, "nutrition.protein", "mixed", usage="explicit"
        )
    ] == ["protein.guide.video.01"]
    assert "fish.image.01" not in {
        asset["asset_id"]
        for asset in _assets_for_media_policy(
            catalog, "nutrition.protein", "mixed", usage="explicit"
        )
    }


def test_v3_protein_concept_is_not_recalled_when_only_related_fish_exists(
    tmp_path: Path,
) -> None:
    root = tmp_path / "protein-boundary-no-guide"
    _write_protein_boundary_catalog(root, include_guide=False)
    catalog = load_semantic_visual_catalog(root)

    assert recall_semantic_visual_candidates("注意补充蛋白质", catalog)[
        "candidates"
    ] == []
    assert _assets_for_media_policy(
        catalog, "nutrition.protein", "mixed", usage="explicit"
    ) == []


def test_recipe_v2_can_freeze_and_resolve_one_video_overlay(tmp_path: Path) -> None:
    root = tmp_path / "catalog-v2"
    _write_v2_catalog(root)
    catalog = load_semantic_visual_catalog(root)
    candidate = recall_semantic_visual_candidates("早餐吃牛肉", catalog)["candidates"][1]

    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[
            {**candidate, "start_us": 1_000_000, "duration_us": 3_000_000}
        ],
        decisions=[
            {
                "candidate_id": candidate["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.beef",
                "importance": 0.9,
                "confidence": 0.95,
                "reason_code": "LITERAL_CONCRETE_OBJECT",
            }
        ],
        media_policy="video_only",
    )

    assert recipe["schema"] == RECIPE_SCHEMA_V2
    assert recipe["media_policy"] == "video_only"
    assert recipe["library_id"] == catalog.library_id
    overlay = recipe["overlays"][0]
    assert overlay["asset_id"] == "beef.video.01"
    assert overlay["media_type"] == "video"
    assert overlay["renderer"] == "video_overlay"
    assert overlay["resource_path"] == "videos/beef_cooking_01/video.mp4"
    assert "video_path" not in overlay
    resolved = frozen_visual_overlays(
        {"visual_analysis": {"recipe": recipe}}, library_root=root
    )
    assert Path(resolved[0]["video_path"]).resolve() == (
        root / "videos" / "beef_cooking_01" / "video.mp4"
    ).resolve()


def test_mixed_media_prefers_images_for_objects_and_video_for_actions(tmp_path: Path) -> None:
    root = tmp_path / "library"
    _write_v2_catalog(root)
    catalog = load_semantic_visual_catalog(root)

    assert _assets_for_media_policy(catalog, "food.beef", "mixed")[0]["media_type"] == "image"
    action_catalog = SemanticVisualCatalog(
        root=catalog.root,
        schema=catalog.schema,
        library_id=catalog.library_id,
        catalog_version=catalog.catalog_version,
        concepts=catalog.concepts,
        assets=tuple(
            {**asset, "concept_ids": ["activity.running"]}
            for asset in catalog.assets
        ),
    )
    assert _assets_for_media_policy(action_catalog, "activity.running", "mixed")[0]["media_type"] == "video"


def test_explicit_activity_uses_action_asset_and_enrichment_uses_only_tagged_broll() -> None:
    catalog = _catalog()

    explicit = _assets_for_media_policy(
        catalog, "activity.aerobic", "mixed", usage="explicit"
    )
    enrichment = _assets_for_media_policy(
        catalog, "activity.aerobic", "mixed", usage="enrichment"
    )

    assert explicit[0]["asset_id"] == "activity.aerobic.crotch_clap.video.01"
    assert all(
        set(item["usage_modes"])
        & {"semantic_overlay", "action_demo", "knowledge_card"}
        for item in explicit
    )
    assert all("full_screen_broll" in item["usage_modes"] for item in enrichment)
    assert explicit[0]["defaults"]["corner"] == "bottom_center"
    assert explicit[0]["defaults"]["scale"] == 0.615
    assert enrichment[0]["defaults"]["corner"] == "center"
    assert enrichment[0]["defaults"]["scale"] == 1.0


def test_shared_occupancy_rejects_overlap_and_applies_locked_spacing() -> None:
    locked = {
        "overlay_id": "locked-video",
        "concept_id": "activity.running",
        "asset_id": "running.video.01",
        "media_type": "video",
        "enabled": True,
        "locked": True,
        "start_us": 10_000_000,
        "duration_us": 3_000_000,
    }
    image = {
        "overlay_id": "automatic-image",
        "concept_id": "food.egg",
        "asset_id": "egg.boiled.01",
        "media_type": "image",
        "enabled": True,
        "start_us": 12_000_000,
        "duration_us": 1_800_000,
    }

    assert visual_overlay_conflicts(image, [locked]) is True
    with pytest.raises(ValueError, match="同一时间只能显示一个语义视觉素材"):
        validate_visual_occupancy([locked, image])


def test_recall_adds_compact_enrichment_anchors_only_for_tagged_assets() -> None:
    catalog = _catalog()
    target_index = next(
        index for index, asset in enumerate(catalog.assets) if asset["media_type"] == "video"
    )
    target_concept = catalog.assets[target_index]["concept_ids"][0]
    target_alias = next(
        concept["aliases"][0]
        for concept in catalog.concepts
        if concept["concept_id"] == target_concept
    )
    tagged_assets = tuple(
        {
            **asset,
            "usage_modes": (
                ["full_screen_broll"] if index == target_index else ["manual_only"]
            ),
        }
        for index, asset in enumerate(catalog.assets)
    )
    tagged_catalog = SemanticVisualCatalog(
        root=catalog.root,
        schema=catalog.schema,
        library_id=catalog.library_id,
        catalog_version=catalog.catalog_version,
        concepts=catalog.concepts,
        assets=tagged_assets,
    )
    script = f"这段先说明背景，然后安排{target_alias}帮助理解。" * 12

    recalled = recall_semantic_visual_candidates(
        script, tagged_catalog, video_duration_us=60_000_000
    )
    enrichment = [
        item
        for item in recalled["candidates"]
        if str(item.get("candidate_id") or "").startswith("ve_")
    ]

    assert enrichment
    assert {item["target_start_us"] for item in enrichment} <= {
        10_000_000,
        20_000_000,
        30_000_000,
        40_000_000,
        50_000_000,
    }
    assert all(len(item["allowed_concepts"]) <= 8 for item in enrichment)
    assert all(
        item["allowed_concepts"][0]["concept_id"] == target_concept
        for item in enrichment
    )
    assert all(item["direct_concept_ids"] == [target_concept] for item in enrichment)


def test_seam_recall_rejects_full_screen_only_broll() -> None:
    catalog = _catalog()
    target_index = next(
        index
        for index, asset in enumerate(catalog.assets)
        if asset["media_type"] == "video"
        and "full_screen_broll" in set(asset.get("usage_modes", ()))
    )
    target_concept = catalog.assets[target_index]["concept_ids"][0]
    target_alias = next(
        concept["aliases"][0]
        for concept in catalog.concepts
        if concept["concept_id"] == target_concept
    )
    tagged_assets = tuple(
        {
            **asset,
            "usage_modes": (
                ["full_screen_broll"] if index == target_index else ["manual_only"]
            ),
        }
        for index, asset in enumerate(catalog.assets)
    )
    tagged_catalog = SemanticVisualCatalog(
        root=catalog.root,
        schema=catalog.schema,
        library_id=catalog.library_id,
        catalog_version=catalog.catalog_version,
        concepts=catalog.concepts,
        assets=tagged_assets,
    )
    next_segment = "接下来再说明具体应该怎样坚持。"
    script = f"前面先安排{target_alias}帮助理解。{next_segment}"

    seams = [
        item
        for item in recall_semantic_visual_candidates(
            script,
            tagged_catalog,
            video_duration_us=12_000_000,
            segment_boundaries=[
                {"boundary_us": 5_000_000, "script_text": next_segment}
            ],
        )["candidates"]
        if item.get("usage") == "seam_broll"
    ]

    assert seams == []
    assert (
        _assets_for_media_policy(
            tagged_catalog,
            target_concept,
            "video_only",
            usage="seam_broll",
        )
        == []
    )


def test_plain_sunbathing_alias_recalls_the_exact_approved_video_concept() -> None:
    candidates = recall_semantic_visual_candidates(
        "每天晒太阳，保持规律作息。", _catalog()
    )["candidates"]

    exact = next(item for item in candidates if item["text"] == "晒太阳")
    assert [item["concept_id"] for item in exact["allowed_concepts"]] == [
        "activity.sunbathing"
    ]


def test_enrichment_never_offers_unrelated_rotating_concepts() -> None:
    catalog = _catalog()
    concept_ids = [str(item["concept_id"]) for item in catalog.concepts[:10]]
    base_asset = dict(catalog.assets[0])
    assets = tuple(
        {
            **base_asset,
            "asset_id": f"enrichment.test.{index}",
            "concept_ids": [concept_id],
            "usage_modes": ["full_screen_broll"],
            "auto_eligible": True,
        }
        for index, concept_id in enumerate(concept_ids)
    )
    tagged_catalog = SemanticVisualCatalog(
        root=catalog.root,
        schema=catalog.schema,
        library_id=catalog.library_id,
        catalog_version=catalog.catalog_version,
        concepts=catalog.concepts,
        assets=assets,
    )
    script = "这是一段不直接命中素材名称的健康生活说明。" * 20

    enrichment = [
        item
        for item in recall_semantic_visual_candidates(
            script, tagged_catalog, video_duration_us=90_000_000
        )["candidates"]
        if str(item["candidate_id"]).startswith("ve_")
    ]

    assert enrichment == []


@pytest.mark.parametrize(
    ("article_type", "included", "excluded"),
    [
        ("鸡汤文", "editorial.family_life", "editorial.meal_daily"),
        ("干货类", "editorial.meal_daily", "editorial.family_life"),
        ("带人设介绍的干货类", "editorial.family_life", None),
    ],
)
def test_editorial_broll_pools_are_limited_by_article_type(
    article_type: str, included: str, excluded: str | None
) -> None:
    segment_script = "真正长期能坚持的改变，往往来自每天都做得到的小选择。"
    script = segment_script * 8
    candidates = recall_semantic_visual_candidates(
        script,
        _catalog_with_editorial_broll(),
        video_duration_us=60_000_000,
        article_type=article_type,
        segment_boundaries=[
            {"boundary_us": 30_000_000, "script_text": segment_script}
        ],
    )["candidates"]
    enrichment = [item for item in candidates if item.get("usage") == "enrichment"]
    seams = [item for item in candidates if item.get("usage") == "seam_broll"]

    assert all(
        not any(
            concept["concept_id"].startswith("editorial.")
            for concept in item["allowed_concepts"]
        )
        for item in enrichment
    )
    assert seams
    offered = {
        concept["concept_id"]
        for item in seams
        for concept in item["allowed_concepts"]
    }
    assert included in offered
    if excluded is not None:
        assert excluded not in offered


def test_editorial_pool_can_create_seam_candidate_without_literal_alias() -> None:
    script = "先把前面的道理说清楚。接下来聊聊怎样把改变放进普通生活。"
    candidate = next(
        item
        for item in recall_semantic_visual_candidates(
            script,
            _catalog_with_editorial_broll(),
            video_duration_us=12_000_000,
            segment_boundaries=[
                {"boundary_us": 5_000_000, "script_text": "接下来聊聊怎样把改变放进普通生活。"}
            ],
            article_type="鸡汤文",
        )["candidates"]
        if item.get("usage") == "seam_broll"
    )

    assert candidate["text"] == "先把前面的道理说清楚。接下来聊聊怎样把改变放进普通生活"
    assert {
        concept["concept_id"] for concept in candidate["allowed_concepts"]
    } == {
        "editorial.home_daily",
        "editorial.leisure_daily",
        "editorial.family_life",
        "editorial.mood_atmosphere",
    }


def test_enrichment_uses_video_level_asset_deduplication() -> None:
    catalog = _catalog()
    candidates = recall_semantic_visual_candidates(
        "苹果、香蕉、西红柿", catalog
    )["candidates"]
    mapped = [
        {
            **candidate,
            "start_us": start_us,
            "duration_us": 1_800_000,
            "usage": "enrichment",
            "direct_concept_ids": [
                candidate["allowed_concepts"][0]["concept_id"]
            ],
        }
        for candidate, start_us in zip(
            candidates,
            (20_000_000, 45_000_000, 70_000_000),
        )
    ]
    decisions = [
        {
            "candidate_id": candidate["candidate_id"],
            "decision": "SHOW",
            "concept_id": candidate["allowed_concepts"][0]["concept_id"],
            "usage": "enrichment",
            "confidence": 1.0,
            "importance": 0.75,
        }
        for candidate in mapped
    ]

    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=mapped,
        decisions=decisions,
        media_policy="mixed",
    )

    assert [item["start_us"] for item in recipe["overlays"]] == [
        20_000_000,
        45_000_000,
        70_000_000,
    ]
    selected_asset_ids = [item["asset_id"] for item in recipe["overlays"]]
    assert len(selected_asset_ids) == len(set(selected_asset_ids))
    assert recipe["used_asset_ids"] == sorted(selected_asset_ids)
    assert all(item["usage"] == "enrichment" for item in recipe["overlays"])

    too_early = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[{**mapped[0], "start_us": 5_999_999}],
        decisions=[decisions[0]],
        media_policy="mixed",
    )
    assert too_early["overlays"] == []


def test_enrichment_does_not_replace_editorial_choice_with_direct_apple() -> None:
    catalog = _catalog()
    candidate = {
        "candidate_id": "ve_exact_apple",
        "text": "只吃苹果",
        "char_start": 0,
        "char_end": 5,
        "phrase_char_start": 0,
        "phrase_char_end": 5,
        "phrase_text": "只吃苹果",
        "allowed_concepts": [
            {"concept_id": "food.apple", "description": "苹果"},
            {"concept_id": "editorial.home_daily", "description": "居家日常"},
        ],
        "direct_concept_ids": ["food.apple"],
        "usage": "enrichment",
        "start_us": 10_000_000,
        "duration_us": 2_000_000,
        "video_duration_us": 20_000_000,
    }

    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[candidate],
        decisions=[
            {
                "candidate_id": candidate["candidate_id"],
                "decision": "SHOW",
                "concept_id": "editorial.home_daily",
                "usage": "enrichment",
                "confidence": 1.0,
                "importance": 0.9,
            }
        ],
        media_policy="mixed",
    )

    assert recipe["overlays"] == []


def test_missing_seam_decision_keeps_original_transition_without_fallback() -> None:
    catalog = _catalog_with_editorial_broll()
    seam = {
        "candidate_id": "vs_boundary_editorial",
        "text": "忍一忍。接下来继续说",
        "char_start": 0,
        "char_end": 10,
        "phrase_char_start": 0,
        "phrase_char_end": 10,
        "phrase_text": "忍一忍。接下来继续说",
        "allowed_concepts": [
            {"concept_id": "editorial.home_daily", "description": "居家日常"},
            {"concept_id": "editorial.mood_atmosphere", "description": "情绪氛围"},
        ],
        "usage": "seam_broll",
        "segment_boundary_us": 5_000_000,
        "start_us": 4_500_000,
        "duration_us": 2_000_000,
        "video_duration_us": 10_000_000,
    }
    cake = {
        "candidate_id": "vc_cake_after_boundary",
        "text": "蛋糕",
        "char_start": 11,
        "char_end": 13,
        "phrase_char_start": 11,
        "phrase_char_end": 13,
        "phrase_text": "蛋糕",
        "allowed_concepts": [
            {"concept_id": "food.cake", "description": "蛋糕"},
        ],
        "start_us": 5_100_000,
        "duration_us": 2_000_000,
        "video_duration_us": 10_000_000,
    }

    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[seam, cake],
        decisions=[
            {
                "candidate_id": cake["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.cake",
                "confidence": 1.0,
                "importance": 1.0,
            }
        ],
        media_policy="mixed",
        segment_boundaries=[{"boundary_us": 5_000_000}],
        final_video_duration_us=10_000_000,
    )

    assert all(
        overlay.get("usage") != "seam_broll" for overlay in recipe["overlays"]
    )


def test_cake_video_is_not_eligible_for_seam_broll() -> None:
    cake = _catalog().asset("review.u0006.video.59499a298d")

    assert cake is not None
    assert "full_screen_broll" in cake["usage_modes"]
    assert "seam_broll" not in cake["usage_modes"]


def test_full_screen_broll_reserves_its_slot_before_explicit_image() -> None:
    catalog = _catalog()
    explicit = {
        "candidate_id": "vc_apple_image",
        "text": "苹果",
        "char_start": 0,
        "char_end": 2,
        "allowed_concepts": [{"concept_id": "food.apple", "description": "苹果"}],
        "start_us": 10_000_000,
        "duration_us": 2_000_000,
        "video_duration_us": 20_000_000,
    }
    enrichment = {
        **explicit,
        "candidate_id": "ve_apple_video",
        "usage": "enrichment",
        "direct_concept_ids": ["food.apple"],
        "start_us": 10_200_000,
    }
    decisions = [
        {
            "candidate_id": explicit["candidate_id"],
            "decision": "SHOW",
            "concept_id": "food.apple",
            "confidence": 1.0,
            "importance": 1.0,
        },
        {
            "candidate_id": enrichment["candidate_id"],
            "decision": "SHOW",
            "concept_id": "food.apple",
            "usage": "enrichment",
            "confidence": 1.0,
            "importance": 0.8,
        },
    ]

    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[explicit, enrichment],
        decisions=decisions,
        media_policy="image_only",
    )

    assert len(recipe["overlays"]) == 1
    assert recipe["overlays"][0]["usage"] == "enrichment"
    assert recipe["overlays"][0]["media_type"] == "video"


def test_seam_broll_does_not_reset_ordinary_broll_six_second_gap() -> None:
    catalog = _catalog()
    enrichment = {
        "candidate_id": "ve_apple_after_seam",
        "text": "苹果",
        "char_start": 0,
        "char_end": 2,
        "allowed_concepts": [{"concept_id": "food.apple", "description": "苹果"}],
        "usage": "enrichment",
        "direct_concept_ids": ["food.apple"],
        "start_us": 10_000_000,
        "target_start_us": 10_000_000,
        "duration_us": 2_000_000,
        "video_duration_us": 20_000_000,
    }
    locked_seam = {
        "overlay_id": "locked-seam",
        "asset_id": "manual-seam-video",
        "media_type": "video",
        "enabled": True,
        "locked": True,
        "selection_mode": "manual",
        "usage": "seam_broll",
        "timing_mode": "seam_broll",
        "display_role": "full_screen_broll",
        "start_us": 5_000_000,
        "duration_us": 2_000_000,
    }

    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[enrichment],
        decisions=[
            {
                "candidate_id": enrichment["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.apple",
                "usage": "enrichment",
                "confidence": 1.0,
            }
        ],
        media_policy="image_only",
        locked_overlays=[locked_seam],
    )

    assert any(item.get("usage") == "enrichment" for item in recipe["overlays"])


def test_ordinary_broll_stays_in_its_sentence_when_trimmed_around_seam() -> None:
    catalog = _catalog()
    enrichment = {
        "candidate_id": "ve_apple_overlapping_seam",
        "text": "苹果",
        "char_start": 0,
        "char_end": 2,
        "allowed_concepts": [{"concept_id": "food.apple", "description": "苹果"}],
        "usage": "enrichment",
        "direct_concept_ids": ["food.apple"],
        "start_us": 10_000_000,
        "target_start_us": 10_000_000,
        "duration_us": 5_000_000,
        "video_duration_us": 20_000_000,
    }
    locked_seam = {
        "overlay_id": "locked-overlapping-seam",
        "asset_id": "manual-overlapping-seam-video",
        "media_type": "video",
        "enabled": True,
        "locked": True,
        "selection_mode": "manual",
        "usage": "seam_broll",
        "timing_mode": "seam_broll",
        "display_role": "full_screen_broll",
        "start_us": 9_500_000,
        "duration_us": 1_000_000,
    }

    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[enrichment],
        decisions=[
            {
                "candidate_id": enrichment["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.apple",
                "usage": "enrichment",
                "confidence": 1.0,
            }
        ],
        media_policy="image_only",
        locked_overlays=[locked_seam],
    )

    ordinary = next(
        item for item in recipe["overlays"] if item.get("usage") == "enrichment"
    )
    assert ordinary["start_us"] == 10_500_000
    assert ordinary["start_us"] + ordinary["duration_us"] <= 15_000_000


def test_same_concept_can_use_image_and_later_full_screen_video() -> None:
    catalog = _catalog()
    explicit = {
        "candidate_id": "vc_apple_early",
        "text": "苹果",
        "char_start": 0,
        "char_end": 2,
        "allowed_concepts": [{"concept_id": "food.apple", "description": "苹果"}],
        "start_us": 1_000_000,
        "duration_us": 2_000_000,
        "video_duration_us": 20_000_000,
    }
    enrichment = {
        **explicit,
        "candidate_id": "ve_apple_later",
        "usage": "enrichment",
        "direct_concept_ids": ["food.apple"],
        "start_us": 10_000_000,
    }
    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[explicit, enrichment],
        decisions=[
            {
                "candidate_id": explicit["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.apple",
                "confidence": 1.0,
            },
            {
                "candidate_id": enrichment["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.apple",
                "usage": "enrichment",
                "confidence": 1.0,
            },
        ],
        media_policy="image_only",
    )

    assert [item["display_role"] for item in recipe["overlays"]] == [
        "semantic_overlay",
        "full_screen_broll",
    ]
    assert len({item["asset_id"] for item in recipe["overlays"]}) == 2


def test_untouched_auto_recipe_refreshes_current_asset_layout_and_resource() -> None:
    overlay = {
        "overlay_id": "vo-cucumber",
        "asset_id": "cucumber.salad.01",
        "enabled": True,
        "selection_mode": "auto",
        "manual": False,
        "locked": False,
        "media_type": "image",
        "renderer": "jyd_sticker_bundle",
        "resource_path": "bundles/cucumber_salad_01",
        "corner": "top_right",
        "scale": 0.25,
        "opacity": 1.0,
    }
    item = {
        "visual_analysis": {
            "recipe": {
                "schema": RECIPE_SCHEMA_V2,
                "catalog_version": "sha256:legacy",
                "overlays": [overlay],
            }
        }
    }

    refreshed = frozen_visual_overlays(item, library_root=CATALOG_ROOT)[0]

    assert refreshed["corner"] == "bottom_center"
    assert refreshed["scale"] == 0.56
    assert Path(refreshed["bundle_path"]).name == "cucumber_salad_lower_fade_02"

    overlay.update({"manual": True, "locked": True})
    manual = frozen_visual_overlays(item, library_root=CATALOG_ROOT)[0]
    assert manual["corner"] == "top_right"
    assert manual["scale"] == 0.25
    assert Path(manual["bundle_path"]).name == "cucumber_salad_01"


def test_catalog_rejects_path_escape_and_duplicate_asset_id(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    manifest_path = root / "catalog.json"
    payload = json.loads((CATALOG_ROOT / "catalog.json").read_text(encoding="utf-8"))
    payload["assets"][0]["resource"]["bundle"] = "../outside"
    (tmp_path / "outside").mkdir()
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SemanticVisualCatalogError):
        load_semantic_visual_catalog(root)

    payload = json.loads((CATALOG_ROOT / "catalog.json").read_text(encoding="utf-8"))
    payload["assets"][1]["asset_id"] = payload["assets"][0]["asset_id"]
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SemanticVisualCatalogError):
        load_semantic_visual_catalog(root)


def test_catalog_version_changes_when_image_bytes_change(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    _write_v2_catalog(root)
    before = load_semantic_visual_catalog(root).catalog_version
    image_path = Path(load_semantic_visual_catalog(root).assets[0]["image_path"])
    image_path.write_bytes(image_path.read_bytes() + b"catalog-version-test")

    assert load_semantic_visual_catalog(root).catalog_version != before


def test_recall_uses_exact_python_ranges_and_longest_alias() -> None:
    script = "早餐吃水煮蛋、玉米和豆浆。"
    payload = recall_semantic_visual_candidates(script, _catalog())

    assert [item["text"] for item in payload["candidates"]] == [
        "早餐",
        "水煮蛋",
        "玉米",
        "豆浆",
    ]
    for item in payload["candidates"]:
        assert script[item["char_start"] : item["char_end"]] == item["text"]
    assert payload == recall_semantic_visual_candidates(script, _catalog())


def test_recall_rejects_known_compounds_that_have_no_matching_asset() -> None:
    payload = recall_semantic_visual_candidates(
        "不要吃油脂很多的鸡蛋糕，也不要吃放久的蔬菜沙拉，它被叫做植物三文鱼。",
        _catalog(),
    )

    assert all(
        item["text"] not in {"鸡蛋", "蔬菜", "三文鱼"}
        for item in payload["candidates"]
    )


def test_platform_ui_quarantine_disables_assets_centrally() -> None:
    catalog = _catalog()

    assert catalog.asset("review.u0102.video.a64745b702")["auto_eligible"] is False
    assert catalog.asset("manual.048.video.10095de4fb")["auto_eligible"] is False
    assert frozen_visual_overlays(
        {
            "visual_analysis": {
                "recipe": {
                    "schema": RECIPE_SCHEMA_V2,
                    "overlays": [
                        {
                            "asset_id": "review.u0102.video.a64745b702",
                            "selection_mode": "auto",
                            "manual": False,
                            "locked": False,
                        }
                    ],
                }
            }
        },
        library_root=CATALOG_ROOT,
    ) == []


def test_recall_does_not_decide_idiom_negation_or_meta_context_locally() -> None:
    script = "每天吃一个鸡蛋。鸡蛋里挑骨头。这不是鸡蛋。讨论鸡蛋这个词。"
    payload = recall_semantic_visual_candidates(script, _catalog())

    assert [item["text"] for item in payload["candidates"]] == ["鸡蛋"] * 4


def test_minimax_mapping_uses_phrase_span_and_clamps_to_video_duration() -> None:
    script = "每天吃一个鸡蛋"
    candidates = recall_semantic_visual_candidates(script, _catalog())["candidates"]
    mapped = map_visual_candidates_to_raw_cues(
        script,
        candidates,
        [{"start_us": 1_000_000, "end_us": 3_000_000, "text": script}],
        video_duration_us=3_100_000,
        cover_offset_us=200_000,
    )

    assert mapped[0]["start_us"] == 1_200_000
    assert mapped[0]["duration_us"] == 1_900_000
    assert mapped[0]["start_us"] + mapped[0]["duration_us"] == 3_100_000


def test_mapping_covers_the_punctuation_phrase_containing_the_keyword() -> None:
    script = "早餐可以灵活安排，比如早上喝碗杂粮粥。"
    candidate = next(
        item
        for item in recall_semantic_visual_candidates(script, _catalog())["candidates"]
        if item["text"] == "杂粮粥"
    )

    mapped = map_visual_candidates_to_raw_cues(
        script,
        [candidate],
        [{"start_us": 0, "end_us": 8_000_000, "text": script}],
        video_duration_us=8_000_000,
    )[0]

    assert mapped["phrase_text"] == "比如早上喝碗杂粮粥"
    assert mapped["phrase_char_start"] == script.index("比如")
    assert mapped["phrase_char_end"] == script.index("。")
    keyword_time_us = candidate["char_start"] / len(script) * 8_000_000
    assert mapped["start_us"] < keyword_time_us
    assert mapped["start_us"] + mapped["duration_us"] > keyword_time_us
    assert mapped["timing_source"] == "minimax_raw_cue_phrase_span"


def test_mapping_prefers_funasr_phrase_timestamps_when_available() -> None:
    script = "每天吃一个鸡蛋"
    candidate = recall_semantic_visual_candidates(script, _catalog())["candidates"][0]
    raw_cues = [{"start_us": 0, "end_us": 4_000_000, "text": script}]
    alignment = {
        "status": "SUCCESS",
        "ranges": [
            {
                "token_index": index,
                "start_us": 500_000 + index * 300_000,
                "end_us": 760_000 + index * 300_000,
            }
            for index in range(7)
        ],
    }

    mapped = map_visual_candidates_to_raw_cues(
        script,
        [candidate],
        raw_cues,
        asr_alignment=alignment,
    )[0]

    assert mapped["start_us"] == 500_000
    assert mapped["duration_us"] == 2_060_000
    assert mapped["matched_end_us"] == 2_560_000
    assert mapped["phrase_text"] == script
    assert mapped["timing_source"] == "funasr_phrase_timestamps"


def test_mapping_rejects_raw_cue_text_mismatch() -> None:
    script = "每天吃一个鸡蛋"
    candidates = recall_semantic_visual_candidates(script, _catalog())["candidates"]
    with pytest.raises(SemanticSubtitleMappingError) as error:
        map_visual_candidates_to_raw_cues(
            script,
            candidates,
            [{"start_us": 0, "end_us": 1_000_000, "text": "每天吃一个玉米"}],
        )
    assert error.value.code == "RAW_CUES_TEXT_MISMATCH"


def test_recipe_applies_confidence_and_density_policy() -> None:
    catalog = _catalog()
    script = "鸡蛋鸡蛋鸡蛋"
    candidates = recall_semantic_visual_candidates(script, catalog)["candidates"]
    mapped = [
        {**candidate, "start_us": index * 7_000_000, "duration_us": 1_800_000}
        for index, candidate in enumerate(candidates)
    ]
    decisions = [
        {
            "candidate_id": candidate["candidate_id"],
            "decision": "SHOW",
            "concept_id": "food.egg",
            "confidence": 0.95,
            "reason_code": "LITERAL_CONCRETE_OBJECT",
        }
        for candidate in candidates
    ]

    recipe = build_visual_recipe(
        catalog=catalog, mapped_candidates=mapped, decisions=decisions
    )

    # Same concept/asset has a 20 second cooldown, so only the first survives.
    assert len(recipe["overlays"]) == 1
    assert recipe["overlays"][0]["asset_id"].startswith("egg.")


def test_recipe_conflict_prefers_importance_then_confidence() -> None:
    catalog = _catalog()
    candidates = recall_semantic_visual_candidates("鸡蛋和玉米", catalog)["candidates"]
    mapped = [
        {**candidates[0], "start_us": 0, "duration_us": 1_800_000},
        {**candidates[1], "start_us": 3_000_000, "duration_us": 1_800_000},
    ]
    decisions = [
        {
            "candidate_id": candidates[0]["candidate_id"],
            "decision": "SHOW",
            "concept_id": "food.egg",
            "importance": 0.7,
            "confidence": 0.99,
            "reason_code": "LITERAL_CONCRETE_OBJECT",
        },
        {
            "candidate_id": candidates[1]["candidate_id"],
            "decision": "SHOW",
            "concept_id": "food.corn",
            "importance": 0.95,
            "confidence": 0.9,
            "reason_code": "LITERAL_CONCRETE_OBJECT",
        },
    ]

    recipe = build_visual_recipe(
        catalog=catalog, mapped_candidates=mapped, decisions=decisions
    )

    assert [item["concept_id"] for item in recipe["overlays"]] == [
        "food.egg",
        "food.corn",
    ]


def test_recipe_trims_a_minor_edge_overlap_instead_of_dropping_the_item() -> None:
    catalog = _catalog()
    candidates = recall_semantic_visual_candidates("鸡蛋。玉米。", catalog)["candidates"]
    egg = next(item for item in candidates if item["text"] == "鸡蛋")
    corn = next(item for item in candidates if item["text"] == "玉米")
    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[
            {**egg, "start_us": 0, "duration_us": 2_000_000},
            {**corn, "start_us": 1_800_000, "duration_us": 2_000_000},
        ],
        decisions=[
            {
                "candidate_id": egg["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.egg",
                "importance": 1.0,
                "confidence": 1.0,
            },
            {
                "candidate_id": corn["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.corn",
                "importance": 0.9,
                "confidence": 1.0,
            },
        ],
    )

    assert len(recipe["overlays"]) == 2
    assert recipe["overlays"][1]["start_us"] == 2_000_000
    assert recipe["overlays"][1]["duration_us"] == 1_800_000


def test_punctuation_free_manual_candidates_keep_the_mapped_sentence_duration() -> None:
    catalog = _catalog()
    script = "牛肉鸡蛋豆腐苹果西红柿黄瓜杏仁"
    candidates = recall_semantic_visual_candidates(script, catalog)["candidates"]
    mapped = [
        {
            **candidate,
            "start_us": 1_200_000 + index * 2_000_000,
            "duration_us": 1_500_000,
        }
        for index, candidate in enumerate(candidates)
    ]
    decisions = [
        {
            "candidate_id": candidate["candidate_id"],
            "decision": "SHOW",
            "concept_id": candidate["allowed_concepts"][0]["concept_id"],
            "importance": 0.9,
            "confidence": 0.95,
        }
        for candidate in candidates
    ]

    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=mapped,
        decisions=decisions,
    )

    assert len(candidates) >= 6
    assert len(recipe["overlays"]) == len(candidates)
    assert all(item["duration_us"] == 1_500_000 for item in recipe["overlays"])
    assert all(
        current["start_us"] - previous["start_us"] == 2_000_000
        for previous, current in zip(recipe["overlays"], recipe["overlays"][1:])
    )


def test_recipe_allows_first_phrase_visual_to_start_at_zero() -> None:
    catalog = _catalog()
    candidate = recall_semantic_visual_candidates("早餐吃鸡蛋", catalog)["candidates"][0]
    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[
            {
                **candidate,
                "start_us": 0,
                "duration_us": 2_000_000,
                "matched_end_us": 1_500_000,
            }
        ],
        decisions=[
            {
                "candidate_id": candidate["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.egg",
                "confidence": 0.95,
            }
        ],
    )

    assert recipe["overlays"][0]["start_us"] == 0
    assert recipe["overlays"][0]["duration_us"] == 2_000_000


def test_recipe_keeps_a_short_first_phrase_without_opening_delay() -> None:
    catalog = _catalog()
    candidate = recall_semantic_visual_candidates("早餐吃鸡蛋", catalog)["candidates"][0]
    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[
            {
                **candidate,
                "start_us": 0,
                "duration_us": 1_800_000,
                "matched_end_us": 900_000,
            }
        ],
        decisions=[
            {
                "candidate_id": candidate["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.egg",
                "confidence": 0.95,
            }
        ],
    )

    assert len(recipe["overlays"]) == 1
    assert recipe["overlays"][0]["start_us"] == 0
    assert recipe["overlays"][0]["duration_us"] == 1_800_000


def test_rapid_list_covers_the_whole_sentence_and_cuts_in_speech_order() -> None:
    catalog = _catalog()
    script = "早餐可以吃鸡蛋、玉米、红薯。"
    candidates = [
        item
        for item in recall_semantic_visual_candidates(script, catalog)["candidates"]
        if item["text"] in {"鸡蛋", "玉米", "红薯"}
    ]
    mapped = map_visual_candidates_to_raw_cues(
        script,
        candidates,
        [{"start_us": 0, "end_us": 3_000_000, "text": script}],
        video_duration_us=3_000_000,
    )
    decisions = [
        {
            "candidate_id": item["candidate_id"],
            "decision": "SHOW",
            "concept_id": item["allowed_concepts"][0]["concept_id"],
            "importance": 1.0,
            "confidence": 1.0,
        }
        for item in candidates
    ]

    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=mapped,
        decisions=decisions,
        media_policy="mixed",
    )

    overlays = recipe["overlays"]
    assert len(overlays) == 3
    assert [item["timing_mode"] for item in overlays] == ["rapid_list"] * 3
    assert [item["list_index"] for item in overlays] == [0, 1, 2]
    assert overlays[0]["start_us"] == mapped[0]["start_us"]
    assert overlays[-1]["start_us"] + overlays[-1]["duration_us"] == (
        mapped[0]["start_us"] + mapped[0]["duration_us"]
    )
    assert all(
        previous["start_us"] + previous["duration_us"] == current["start_us"]
        for previous, current in zip(overlays, overlays[1:])
    )
    assert all(item["duration_us"] < 2_000_000 for item in overlays)
    assert len(recipe["used_asset_ids"]) == 3


def test_rapid_list_keeps_positive_contiguous_ranges_when_keyword_times_collapse() -> None:
    catalog = _catalog()
    script = "早餐可以吃鸡蛋、玉米、红薯。"
    candidates = [
        item
        for item in recall_semantic_visual_candidates(script, catalog)["candidates"]
        if item["text"] in {"鸡蛋", "玉米", "红薯"}
    ]
    mapped = map_visual_candidates_to_raw_cues(
        script,
        candidates,
        [{"start_us": 0, "end_us": 3_000_000, "text": script}],
        video_duration_us=3_000_000,
    )
    mapped = [
        {**item, "keyword_start_us": 1_500_000, "keyword_end_us": 1_500_000}
        for item in mapped
    ]
    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=mapped,
        decisions=[
            {
                "candidate_id": item["candidate_id"],
                "decision": "SHOW",
                "concept_id": item["allowed_concepts"][0]["concept_id"],
                "importance": 1.0,
                "confidence": 1.0,
            }
            for item in candidates
        ],
        media_policy="mixed",
    )

    overlays = recipe["overlays"]
    assert len(overlays) == 3
    assert all(item["duration_us"] > 0 for item in overlays)
    assert all(
        previous["start_us"] + previous["duration_us"] == current["start_us"]
        for previous, current in zip(overlays, overlays[1:])
    )


def test_short_normal_sentence_is_clamped_to_final_video_end() -> None:
    catalog = _catalog()
    candidate = recall_semantic_visual_candidates("鸡蛋。", catalog)["candidates"][0]
    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[
            {
                **candidate,
                "start_us": 1_500_000,
                "duration_us": 300_000,
                "video_duration_us": 2_400_000,
            }
        ],
        decisions=[
            {
                "candidate_id": candidate["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.egg",
                "confidence": 1.0,
            }
        ],
    )

    overlay = recipe["overlays"][0]
    assert overlay["start_us"] == 1_500_000
    assert overlay["duration_us"] == 300_000
    assert overlay["timing_mode"] == "sentence"


def test_locked_asset_is_reserved_and_selector_falls_back_to_next_media(
    tmp_path: Path,
) -> None:
    _write_v2_catalog(tmp_path)
    catalog = load_semantic_visual_catalog(tmp_path)
    candidate = recall_semantic_visual_candidates("牛肉。", catalog)["candidates"][0]
    locked = {
        "overlay_id": "manual-beef-image",
        "asset_id": "beef.image.01",
        "concept_id": "food.beef",
        "enabled": True,
        "manual": True,
        "locked": True,
        "start_us": 30_000_000,
        "duration_us": 2_000_000,
    }
    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[
            {
                **candidate,
                "start_us": 0,
                "duration_us": 2_000_000,
                "video_duration_us": 40_000_000,
            }
        ],
        decisions=[
            {
                "candidate_id": candidate["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.beef",
                "confidence": 1.0,
            }
        ],
        media_policy="mixed",
        locked_overlays=[locked],
    )

    automatic = next(item for item in recipe["overlays"] if item.get("manual") is False)
    assert automatic["asset_id"] == "beef.video.01"
    assert recipe["used_asset_ids"] == ["beef.image.01", "beef.video.01"]


def test_missing_first_asset_falls_back_to_the_next_candidate(tmp_path: Path) -> None:
    _write_v2_catalog(tmp_path)
    catalog = load_semantic_visual_catalog(tmp_path)
    shutil.rmtree(tmp_path / "bundles" / "beef_image_01")
    candidate = recall_semantic_visual_candidates("牛肉。", catalog)["candidates"][0]

    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[{**candidate, "start_us": 0, "duration_us": 2_000_000}],
        decisions=[
            {
                "candidate_id": candidate["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.beef",
                "confidence": 1.0,
            }
        ],
        media_policy="mixed",
    )

    assert recipe["overlays"][0]["asset_id"] == "beef.video.01"


def test_all_concept_assets_already_used_skips_automatic_overlay(tmp_path: Path) -> None:
    _write_v2_catalog(tmp_path)
    catalog = load_semantic_visual_catalog(tmp_path)
    candidate = recall_semantic_visual_candidates("牛肉。", catalog)["candidates"][0]
    locked = [
        {
            "overlay_id": f"manual-{asset_id}",
            "asset_id": asset_id,
            "concept_id": "food.beef",
            "enabled": True,
            "manual": True,
            "locked": True,
            "start_us": start_us,
            "duration_us": 2_000_000,
        }
        for asset_id, start_us in (
            ("beef.image.01", 30_000_000),
            ("beef.video.01", 34_000_000),
        )
    ]
    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[
            {
                **candidate,
                "start_us": 0,
                "duration_us": 2_000_000,
                "video_duration_us": 40_000_000,
            }
        ],
        decisions=[
            {
                "candidate_id": candidate["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.beef",
                "confidence": 1.0,
            }
        ],
        media_policy="mixed",
        locked_overlays=locked,
    )

    assert all(item.get("manual") is True for item in recipe["overlays"])
    assert recipe["used_asset_ids"] == ["beef.image.01", "beef.video.01"]


def test_video_overlay_is_clipped_when_sentence_exceeds_source(tmp_path: Path) -> None:
    _write_v2_catalog(tmp_path)
    catalog = load_semantic_visual_catalog(tmp_path)
    candidate = recall_semantic_visual_candidates("牛肉。", catalog)["candidates"][0]
    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[{**candidate, "start_us": 0, "duration_us": 8_000_000}],
        decisions=[
            {
                "candidate_id": candidate["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.beef",
                "confidence": 1.0,
            }
        ],
        media_policy="video_only",
    )

    overlay = recipe["overlays"][0]
    assert overlay["duration_us"] == 3_000_000
    assert overlay["source_duration_us"] == 3_000_000
    assert overlay["loop_to_target"] is False


def test_video_overlay_never_loops_even_when_legacy_catalog_allows_it(tmp_path: Path) -> None:
    manifest = _write_v2_catalog(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["assets"][1]["defaults"]["loop"] = True
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    catalog = load_semantic_visual_catalog(tmp_path)
    candidate = recall_semantic_visual_candidates("牛肉。", catalog)["candidates"][0]

    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[{**candidate, "start_us": 0, "duration_us": 8_000_000}],
        decisions=[
            {
                "candidate_id": candidate["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.beef",
                "confidence": 1.0,
            }
        ],
        media_policy="video_only",
    )

    overlay = recipe["overlays"][0]
    assert overlay["duration_us"] == 3_000_000
    assert overlay["source_duration_us"] == 3_000_000
    assert overlay["loop_to_target"] is False


def test_segment_boundary_uses_corresponding_unused_seam_broll(tmp_path: Path) -> None:
    manifest = _write_v2_catalog(tmp_path)
    _upgrade_test_catalog_to_v3(manifest)
    catalog = load_semantic_visual_catalog(tmp_path)
    candidate = recall_semantic_visual_candidates("牛肉。", catalog)["candidates"][0]
    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[
            {
                **candidate,
                "start_us": 1_000_000,
                "duration_us": 800_000,
                "video_duration_us": 5_000_000,
                "usage": "seam_broll",
                "direct_concept_ids": ["food.beef"],
                "segment_boundary_us": 1_000_000,
            }
        ],
        decisions=[
            {
                "candidate_id": candidate["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.beef",
                "usage": "seam_broll",
                "confidence": 1.0,
            }
        ],
        media_policy="mixed",
        segment_boundaries=[{"boundary_us": 1_000_000, "script_text": "牛肉。"}],
    )

    seam = next(item for item in recipe["overlays"] if item["timing_mode"] == "seam_broll")
    assert seam["asset_id"] == "beef.video.01"
    assert seam["start_us"] == 500_000
    assert seam["duration_us"] == 1_000_000
    assert seam["segment_boundary_us"] == 1_000_000


def test_segment_boundary_without_approved_broll_keeps_normal_recipe(tmp_path: Path) -> None:
    _write_v2_catalog(tmp_path)
    catalog = load_semantic_visual_catalog(tmp_path)
    candidate = recall_semantic_visual_candidates("牛肉。", catalog)["candidates"][0]
    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[{**candidate, "start_us": 1_000_000, "duration_us": 1_000_000}],
        decisions=[
            {
                "candidate_id": candidate["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.beef",
                "confidence": 1.0,
            }
        ],
        media_policy="mixed",
        segment_boundaries=[{"boundary_us": 1_000_000}],
    )

    assert all(item["timing_mode"] != "seam_broll" for item in recipe["overlays"])


def test_low_confidence_show_is_not_auto_enabled() -> None:
    catalog = _catalog()
    candidate = recall_semantic_visual_candidates("鸡蛋", catalog)["candidates"][0]
    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=[{**candidate, "start_us": 0, "duration_us": 1_800_000}],
        decisions=[
            {
                "candidate_id": candidate["candidate_id"],
                "decision": "SHOW",
                "concept_id": "food.egg",
                "importance": 0.9,
                "confidence": 0.84,
                "reason_code": "LITERAL_CONCRETE_OBJECT",
            }
        ],
    )

    assert recipe["overlays"] == []

from __future__ import annotations

from pathlib import Path

from jyd_probe.content_replace import (
    ContentReplaceJob,
    ImageAddition,
    StickerAddition,
    _apply_json_changes,
)
from jyd_probe.cli import import_pyjianyingdraft
from jyd_probe.image_apply import _image_transform, add_image_overlay_to_data
from jyd_probe.render_job import (
    _build_fixed_overlay_additions,
    _build_visual_overlay_additions,
)
from jyd_probe.semantic_visuals import frozen_visual_overlays
from jyd_probe.semantic_visuals import fixed_nameplate_overlay
from jyd_probe.sticker_apply import add_fullscreen_sticker_to_data


def test_visual_overlay_job_uses_independent_optional_track() -> None:
    bundle = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "libraries"
        / "semantic_visual_library"
        / "bundles"
        / "egg_boiled"
    )
    additions = _build_visual_overlay_additions(
        {
            "visual_overlays": [
                {
                    "enabled": True,
                    "bundle_path": str(bundle),
                    "start_us": 500_000,
                    "duration_us": 1_800_000,
                    "corner": "top_right",
                    "scale": 0.28,
                    "opacity": 0.9,
                }
            ]
        }
    )

    assert len(additions) == 1
    assert additions[0].track_name == "语义前景图片"
    assert additions[0].optional is True
    assert additions[0].image_path == bundle / "resources" / "sticker" / "singleImage.png"


def test_missing_optional_visual_does_not_fail_other_json_changes(tmp_path: Path) -> None:
    job = ContentReplaceJob(
        template_draft_dir=tmp_path,
        output_root=tmp_path,
        sticker_additions=[
            StickerAddition(
                sticker_json_path=tmp_path / "missing" / "sticker.json",
                start_us=0,
                duration_us=1_000_000,
                track_name="语义前景图片",
                optional=True,
            )
        ],
    )
    data = {"duration": 3_000_000, "materials": {}, "tracks": []}

    assert _apply_json_changes(object(), data, job) == 0
    assert data["tracks"] == []


def test_missing_optional_image_does_not_fail_other_json_changes(tmp_path: Path) -> None:
    job = ContentReplaceJob(
        template_draft_dir=tmp_path,
        output_root=tmp_path,
        image_additions=[
            ImageAddition(
                image_path=tmp_path / "missing.png",
                start_us=0,
                duration_us=1_000_000,
                track_name="语义前景图片",
                optional=True,
            )
        ],
    )
    data = {"duration": 3_000_000, "materials": {}, "tracks": []}

    assert _apply_json_changes(import_pyjianyingdraft(), data, job) == 0
    assert data["tracks"] == []


def test_semantic_bundle_writes_timed_safe_area_track_without_changing_duration() -> None:
    bundle = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "libraries"
        / "semantic_visual_library"
        / "bundles"
        / "egg_boiled"
    )
    data = {
        "duration": 8_000_000,
        "canvas_config": {"width": 1080, "height": 1920},
        "tracks": [{"id": "video-track", "type": "video", "segments": []}],
        "materials": {"stickers": []},
    }

    changed = add_fullscreen_sticker_to_data(
        data,
        bundle / "sticker.json",
        start_us=500_000,
        duration_us=1_800_000,
        corner="top_right",
        scale=0.28,
        opacity=0.9,
        track_name="语义前景图片",
        inside_canvas=True,
    )

    assert changed == 1
    assert data["duration"] == 8_000_000
    track = data["tracks"][-1]
    assert track["name"] == "语义前景图片"
    segment = track["segments"][0]
    assert segment["target_timerange"] == {
        "start": 500_000,
        "duration": 1_800_000,
    }
    assert segment["global_alpha"] == 0.9
    transform = segment["clip"]["transform"]
    assert -1.0 <= transform["x"] <= 1.0
    assert -1.0 <= transform["y"] <= 1.0


def test_semantic_bundle_is_inserted_above_video_and_below_caption() -> None:
    bundle = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "libraries"
        / "semantic_visual_library"
        / "bundles"
        / "egg_boiled"
    )
    data = {
        "duration": 8_000_000,
        "canvas_config": {"width": 1080, "height": 1920},
        "tracks": [
            {"id": "video", "type": "video", "segments": [{"render_index": 0}]},
            {"id": "effect", "type": "effect", "segments": [{"render_index": 11_000}]},
            {"id": "caption", "type": "text", "segments": [{"render_index": 14_000}]},
        ],
        "materials": {"stickers": []},
    }

    add_fullscreen_sticker_to_data(
        data,
        bundle / "sticker.json",
        start_us=500_000,
        duration_us=1_800_000,
        corner="bottom_left",
        scale=0.60,
        inside_canvas=True,
        render_below_text=True,
        track_name="语义前景图片",
    )

    assert [track["type"] for track in data["tracks"]] == [
        "video",
        "effect",
        "sticker",
        "text",
    ]
    render_index = data["tracks"][2]["segments"][0]["render_index"]
    assert 11_000 < render_index < 14_000


def test_fixed_nameplate_is_full_length_directly_above_video_and_below_semantic() -> None:
    library = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "libraries"
        / "semantic_visual_library"
    )
    semantic_bundle = library / "bundles" / "whole_grain_multigrain_rice_01"
    nameplate_bundle = library / "fixed" / "nameplate_zhangluo"
    data = {
        "duration": 8_000_000,
        "canvas_config": {"width": 1080, "height": 1920},
        "tracks": [
            {"id": "video", "type": "video", "segments": [{"render_index": 0}]},
            {"id": "caption", "type": "text", "segments": [{"render_index": 14_000}]},
        ],
        "materials": {"stickers": []},
    }
    semantic = _build_visual_overlay_additions(
        {
            "visual_overlays": [
                {
                    "bundle_path": str(semantic_bundle),
                    "start_us": 500_000,
                    "duration_us": 1_800_000,
                    "corner": "bottom_left",
                    "scale": 0.60,
                }
            ]
        }
    )[0]
    nameplate = _build_fixed_overlay_additions(
        {
            "fixed_overlays": [
                {
                    "bundle_path": str(nameplate_bundle),
                    "start_us": 0,
                    "duration_us": 0,
                    "corner": "center",
                    "scale": 0.60,
                    "transform_x": -0.40,
                    "transform_y": -0.26,
                }
            ]
        },
        timeline_duration_us=8_000_000,
    )[0]
    assert nameplate.duration_us == 8_000_000
    assert nameplate.layer_order == 0
    assert semantic.layer_order == 10

    draft = import_pyjianyingdraft()
    for addition in (nameplate, semantic):
        add_image_overlay_to_data(
            draft,
            data,
            addition.image_path,
            start_us=addition.start_us,
            duration_us=addition.duration_us,
            corner=addition.corner,
            scale=addition.scale,
            track_name=addition.track_name,
            render_below_text=addition.render_below_text,
            transform_x=addition.transform_x,
            transform_y=addition.transform_y,
        )

    tracks = {track["name"]: track for track in data["tracks"] if track.get("name")}
    semantic_segment = tracks["语义前景图片"]["segments"][0]
    semantic_index = semantic_segment["track_render_index"]
    nameplate_segment = tracks["固定人名牌"]["segments"][0]
    caption_index = next(
        track["segments"][0]["render_index"]
        for track in data["tracks"]
        if track["type"] == "text"
    )
    assert nameplate_segment["track_render_index"] < semantic_index < caption_index
    semantic_material = next(
        material
        for material in data["materials"]["videos"]
        if material["id"] == semantic_segment["material_id"]
    )
    assert semantic_material["type"] == "photo"
    assert semantic_material["path"].endswith("singleImage.png")
    assert nameplate_segment["target_timerange"] == {
        "start": 0,
        "duration": 8_000_000,
    }
    assert nameplate_segment["clip"]["transform"]["x"] == -0.40
    assert nameplate_segment["clip"]["transform"]["y"] == -0.26
    assert nameplate_segment["clip"]["scale"]["x"] == 0.60


def test_fixed_nameplate_recipe_uses_left_chest_preset() -> None:
    library = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "libraries"
        / "semantic_visual_library"
    )

    overlay = fixed_nameplate_overlay(library)

    assert overlay["corner"] == "center"
    assert overlay["renderer"] == "sticker"
    assert abs(overlay["scale"] - 0.8941348042237189) < 1e-12
    assert overlay["rotation"] == -90.0
    assert abs(overlay["transform_x"] - -0.22939889867171298) < 1e-12
    assert abs(overlay["transform_y"] - -0.11377708978328174) < 1e-12


def test_fixed_nameplate_renders_as_the_original_local_sticker() -> None:
    library = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "libraries"
        / "semantic_visual_library"
    )
    overlay = fixed_nameplate_overlay(library, "standing")
    additions = _build_fixed_overlay_additions(
        {"fixed_overlays": [overlay]},
        timeline_duration_us=8_000_000,
    )
    assert len(additions) == 1
    addition = additions[0]
    assert addition.duration_us == 8_000_000
    data = {
        "duration": 8_000_000,
        "canvas_config": {"width": 1080, "height": 1920},
        "tracks": [
            {"id": "video", "type": "video", "segments": [{"render_index": 0}]},
            {"id": "caption", "type": "text", "segments": [{"render_index": 14_000}]},
        ],
        "materials": {"stickers": []},
    }

    changed = add_fullscreen_sticker_to_data(
        data,
        addition.sticker_json_path,
        start_us=addition.start_us,
        duration_us=addition.duration_us,
        scale=addition.scale,
        rotation=addition.rotation,
        transform_x=addition.transform_x,
        transform_y=addition.transform_y,
        track_name=addition.track_name,
        render_below_text=addition.render_below_text,
    )

    assert changed == 1
    track = next(row for row in data["tracks"] if row.get("name") == "固定人名牌·站姿")
    assert track["type"] == "sticker"
    segment = track["segments"][0]
    assert segment["clip"]["scale"]["x"] == overlay["scale"]
    assert segment["clip"]["rotation"] == -90.0
    assert segment["clip"]["transform"]["x"] == overlay["transform_x"]
    assert segment["clip"]["transform"]["y"] == overlay["transform_y"]
    material = next(
        row for row in data["materials"]["stickers"] if row["id"] == segment["material_id"]
    )
    assert material["resource_id"] == "7533070364969078040"
    assert material["path"].endswith("nameplate_standing\\resources\\sticker")


def test_bottom_portrait_image_height_is_capped_at_thirty_percent() -> None:
    x, y, resolved_scale = _image_transform(
        corner="bottom_left",
        width_ratio=0.60,
        image_width=1080,
        image_height=1920,
        canvas_width=1080,
        canvas_height=1920,
    )

    assert resolved_scale == 0.30
    assert round(x, 2) == -0.62
    assert round(y, 2) == -0.62


def test_bottom_center_matches_manually_accepted_wide_food_layout() -> None:
    x, y, resolved_scale = _image_transform(
        corner="bottom_center",
        width_ratio=0.78,
        image_width=1254,
        image_height=1254,
        canvas_width=1080,
        canvas_height=1920,
    )

    assert resolved_scale == 0.78
    assert x == 0.0
    assert round(y, 2) == -0.70


def test_browser_and_render_consumers_read_same_frozen_recipe() -> None:
    overlay = {
        "overlay_id": "vo-1",
        "asset_id": "egg.boiled.01",
        "enabled": True,
        "start_us": 100_000,
        "duration_us": 1_800_000,
        "corner": "top_right",
        "scale": 0.28,
        "opacity": 1.0,
    }
    item = {
        "visual_analysis": {
            "recipe": {
                "schema": "jyd.semantic-visual-recipe.v1",
                "catalog_version": "sha256:test",
                "overlays": [overlay],
            }
        }
    }

    assert frozen_visual_overlays(item) == [overlay]

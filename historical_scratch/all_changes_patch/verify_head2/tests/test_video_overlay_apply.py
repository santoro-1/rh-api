from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from jyd_probe.content_replace import (
    ContentReplaceJob,
    ImageAddition,
    VideoOverlayAddition,
    _apply_json_changes,
)
from jyd_probe.render_job import _build_visual_video_additions
from jyd_probe.video_overlay_apply import _video_transform, add_video_overlay_to_data


class _Timerange:
    def __init__(self, start: int, duration: int) -> None:
        self.start = start
        self.duration = duration


class _ClipSettings:
    def __init__(self, **values) -> None:
        self.values = values


class _Speed:
    def export_json(self) -> dict[str, str]:
        return {"id": "speed"}


class _VideoMaterial:
    width = 1920
    height = 1080
    duration = 2_000_000
    material_id = "video-material"

    def __init__(self, path: str) -> None:
        self.path = path

    def export_json(self) -> dict[str, object]:
        return {"id": self.material_id, "path": self.path, "type": "video"}


class _VideoSegment:
    def __init__(
        self,
        material: _VideoMaterial,
        target_timerange: _Timerange,
        *,
        source_timerange: _Timerange | None = None,
        volume: float,
        clip_settings: _ClipSettings,
    ) -> None:
        self.material = material
        self.target_timerange = target_timerange
        self.source_timerange = source_timerange or _Timerange(0, target_timerange.duration)
        self.volume = volume
        self.clip_settings = clip_settings
        self.speed = _Speed()

    def export_json(self) -> dict[str, object]:
        values = self.clip_settings.values
        return {
            "material_id": self.material.material_id,
            "target_timerange": {
                "start": self.target_timerange.start,
                "duration": self.target_timerange.duration,
            },
            "source_timerange": {
                "start": self.source_timerange.start,
                "duration": self.source_timerange.duration,
            },
            "volume": self.volume,
            "clip": {
                "scale": {"x": values["scale_x"], "y": values["scale_y"]},
                "transform": {
                    "x": values["transform_x"],
                    "y": values["transform_y"],
                },
            },
        }


class _Draft:
    Timerange = _Timerange
    ClipSettings = _ClipSettings
    VideoMaterial = _VideoMaterial
    VideoSegment = _VideoSegment


def test_native_video_overlay_plays_once_and_covers_portrait_canvas(tmp_path: Path) -> None:
    video = tmp_path / "action.mp4"
    video.write_bytes(b"test")
    data = {
        "duration": 8_000_000,
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {"videos": [], "speeds": []},
        "tracks": [
            {"type": "video", "segments": [{"render_index": 0}]},
            {"name": "固定人名牌", "type": "video", "segments": [{"render_index": 1}]},
            {"type": "text", "segments": [{"render_index": 10}]},
        ],
    }

    changed = add_video_overlay_to_data(
        _Draft(),
        data,
        video,
        start_us=1_000_000,
        duration_us=5_000_000,
        fit="cover",
        corner="center",
        scale=1.0,
        track_name="全屏 B-roll",
    )

    assert changed == 1
    track = next(item for item in data["tracks"] if item.get("name") == "全屏 B-roll")
    assert [item["target_timerange"]["duration"] for item in track["segments"]] == [2_000_000]
    assert data["tracks"].index(track) < next(
        index for index, item in enumerate(data["tracks"]) if item["type"] == "text"
    )
    assert round(track["segments"][0]["clip"]["scale"]["x"], 2) == 3.16
    assert all(item["volume"] == 0.0 for item in track["segments"])


def test_native_video_overlay_ends_at_remaining_source_duration(
    tmp_path: Path,
) -> None:
    video = tmp_path / "short.mp4"
    video.write_bytes(b"test")
    data = {
        "duration": 6_000_000,
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {"videos": [], "speeds": []},
        "tracks": [{"type": "video", "segments": [{"render_index": 0}]}],
    }

    changed = add_video_overlay_to_data(
        _Draft(),
        data,
        video,
        start_us=0,
        duration_us=5_000_000,
        source_start_us=500_000,
    )

    assert changed == 1
    track = next(item for item in data["tracks"] if item.get("name") == "语义前景视频")
    assert [item["target_timerange"]["duration"] for item in track["segments"]] == [1_500_000]
    assert track["segments"][0]["source_timerange"] == {
        "start": 500_000,
        "duration": 1_500_000,
    }


def test_native_video_overlay_loops_only_the_approved_source_window(
    tmp_path: Path,
) -> None:
    video = tmp_path / "short.mp4"
    video.write_bytes(b"test")
    data = {
        "duration": 6_000_000,
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {"videos": [], "speeds": []},
        "tracks": [{"type": "video", "segments": [{"render_index": 0}]}],
    }

    changed = add_video_overlay_to_data(
        _Draft(),
        data,
        video,
        start_us=0,
        duration_us=5_000_000,
        source_start_us=500_000,
        source_duration_us=1_000_000,
        loop_to_target=True,
    )

    assert changed == 5
    track = next(item for item in data["tracks"] if item.get("name") == "语义前景视频")
    assert [item["target_timerange"]["duration"] for item in track["segments"]] == [
        1_000_000,
        1_000_000,
        1_000_000,
        1_000_000,
        1_000_000,
    ]
    assert {item["source_timerange"]["start"] for item in track["segments"]} == {
        500_000
    }


def test_bottom_center_action_window_matches_manually_accepted_layout() -> None:
    x, y, resolved_scale = _video_transform(
        corner="bottom_center",
        width_ratio=0.615,
        fit="contain",
        video_width=528,
        video_height=534,
        canvas_width=1080,
        canvas_height=1920,
    )

    assert resolved_scale == 0.615
    assert x == 0.0
    assert round(y, 2) == -0.64


def test_render_builder_places_window_video_below_nameplate_and_broll_above() -> None:
    additions = _build_visual_video_additions(
        {
            "visual_overlays": [
                {
                    "media_type": "video",
                    "video_path": "window.mp4",
                    "start_us": 2_000_000,
                    "duration_us": 2_000_000,
                    "corner": "bottom_left",
                    "scale": 0.4,
                },
                {
                    "media_type": "video",
                    "video_path": "broll.mp4",
                    "start_us": 8_000_000,
                    "duration_us": 3_000_000,
                    "source_duration_us": 1_500_000,
                    "loop_to_target": True,
                    "corner": "center",
                    "scale": 1.0,
                },
            ]
        }
    )

    assert [(item.track_name, item.layer_order) for item in additions] == [
        ("语义前景视频", 10),
        ("全屏 B-roll", 30),
    ]
    assert additions[1].source_duration_us == 1_500_000
    assert additions[1].loop_to_target is False


def test_visual_layer_order_is_image_or_window_then_nameplate_then_broll(tmp_path: Path) -> None:
    calls: list[str] = []
    job = ContentReplaceJob(
        template_draft_dir=tmp_path,
        output_root=tmp_path,
        image_additions=[
            ImageAddition("semantic.png", layer_order=10, track_name="semantic"),
            ImageAddition("nameplate.png", layer_order=20, track_name="nameplate"),
        ],
        video_overlay_additions=[
            VideoOverlayAddition(
                "window.mp4", 1, 1, layer_order=10, track_name="window"
            ),
            VideoOverlayAddition(
                "broll.mp4", 1, 1, layer_order=30, track_name="broll"
            ),
        ],
    )

    with (
        patch(
            "jyd_probe.content_replace.add_image_overlay_to_data",
            side_effect=lambda *_args, **kwargs: calls.append(kwargs["track_name"]) or 1,
        ),
        patch(
            "jyd_probe.content_replace.add_video_overlay_to_data",
            side_effect=lambda *_args, **kwargs: calls.append(kwargs["track_name"]) or 1,
        ),
    ):
        _apply_json_changes(_Draft(), {}, job)

    assert calls == ["semantic", "window", "nameplate", "broll"]


def test_visual_layers_make_room_below_tightly_packed_text(tmp_path: Path) -> None:
    video = tmp_path / "action.mp4"
    image = tmp_path / "semantic.png"
    video.write_bytes(b"test")
    image.write_bytes(b"test")
    data = {
        "duration": 8_000_000,
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {"videos": [], "speeds": []},
        "tracks": [
            {"type": "video", "segments": [{"render_index": 0}]},
            {"type": "text", "segments": [{"render_index": 1}]},
        ],
    }

    with patch("jyd_probe.image_apply._image_transform", return_value=(0.0, 0.0, 0.3)):
        from jyd_probe.image_apply import add_image_overlay_to_data

        add_image_overlay_to_data(
            _Draft(),
            data,
            image,
            start_us=1_000_000,
            duration_us=1_000_000,
            track_name="下方语义贴图",
        )
    add_video_overlay_to_data(
        _Draft(),
        data,
        video,
        start_us=3_000_000,
        duration_us=1_000_000,
        track_name="固定人名牌",
    )
    add_video_overlay_to_data(
        _Draft(),
        data,
        video,
        start_us=5_000_000,
        duration_us=1_000_000,
        track_name="全屏 B-roll",
    )

    layers = {
        track.get("name", "text" if track.get("type") == "text" else "base"):
        track["segments"][0].get(
            "track_render_index", track["segments"][0].get("render_index")
        )
        for track in data["tracks"]
    }
    assert layers["下方语义贴图"] < layers["固定人名牌"]
    assert layers["固定人名牌"] < layers["全屏 B-roll"]
    assert layers["全屏 B-roll"] < layers["text"]

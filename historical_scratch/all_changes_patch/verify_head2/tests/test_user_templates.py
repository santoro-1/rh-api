from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from jyd_probe.content_replace import _fit_timeline_duration
from jyd_probe.draft_transfer import build_transfer_package
from jyd_probe.project_postprocess import ProjectPostprocessCoordinator
from jyd_probe.render_job import _build_subtitle_range_replacements
from jyd_probe.user_templates import UserTemplateStore, detect_caption_track
from jyd_probe.web_api import WebApiSettings, create_app


def _draft() -> dict:
    return {
        "duration": 4_000_000,
        "materials": {
            "videos": [{"id": "video-material", "path": r"C:\missing\main.mp4"}],
            "texts": [
                {"id": "text-1", "content": json.dumps({"text": "第一句"}, ensure_ascii=False)},
                {"id": "text-2", "content": json.dumps({"text": "第二句"}, ensure_ascii=False)},
            ],
        },
        "tracks": [
            {
                "id": "video-track",
                "type": "video",
                "segments": [{
                    "id": "video-segment",
                    "material_id": "video-material",
                    "target_timerange": {"start": 0, "duration": 4_000_000},
                }],
            },
            {
                "id": "caption-track",
                "type": "text",
                "name": "自动字幕",
                "segments": [
                    {"id": "caption-1", "material_id": "text-1", "target_timerange": {"start": 0, "duration": 2_000_000}},
                    {"id": "caption-2", "material_id": "text-2", "target_timerange": {"start": 2_000_000, "duration": 2_000_000}},
                ],
            },
        ],
    }


def test_user_template_upload_analyze_and_owner_isolation(tmp_path):
    store = UserTemplateStore(tmp_path / "templates", libraries_root=tmp_path / "libraries")
    created = store.create("user-a", "上传1")
    store.upload_draft_file(
        "user-a",
        created["template_id"],
        "draft_content.json",
        json.dumps(_draft(), ensure_ascii=False).encode("utf-8"),
    )

    analyzed = store.analyze("user-a", created["template_id"])

    assert analyzed["status"] == "READY"
    assert analyzed["profile"]["caption_track"]["track_id"] == "caption-track"
    assert analyzed["profile"]["main_video"]["segment_id"] == "video-segment"
    assert store.render_binding("user-a", created["template_id"])["name"] == "上传1"
    with pytest.raises(FileNotFoundError):
        store.get("user-b", created["template_id"])


def test_user_template_without_video_track_is_ready(tmp_path):
    store = UserTemplateStore(tmp_path / "templates", libraries_root=tmp_path / "libraries")
    draft = _draft()
    draft["materials"]["videos"] = []
    draft["tracks"] = [track for track in draft["tracks"] if track.get("type") != "video"]
    created = store.create("user-a", "纯样式模板")
    store.upload_draft_file(
        "user-a",
        created["template_id"],
        "draft_content.json",
        json.dumps(draft, ensure_ascii=False).encode("utf-8"),
    )

    analyzed = store.analyze("user-a", created["template_id"])

    assert analyzed["status"] == "READY"
    assert analyzed["profile"]["main_video"] is None
    assert analyzed["profile"]["caption_track"]["track_id"] == "caption-track"


def test_collector_package_import_is_permanent_and_owner_isolated(tmp_path):
    draft_dir = tmp_path / "collector-draft"
    draft_dir.mkdir()
    sticker = tmp_path / "flower-text-resource.json"
    sticker.write_text('{"resource":"visual"}', encoding="utf-8")
    draft = _draft()
    draft["materials"]["stickers"] = [{"id": "visual-1", "path": str(sticker)}]
    (draft_dir / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False), encoding="utf-8"
    )
    package_path = tmp_path / "template-center.zip"
    build_transfer_package(
        {
            "mode": "template_center",
            "plan_id": "template-plan",
            "report_id": "template-report",
            "draft": {"name": "采集草稿", "analyzed_draft_dir": str(draft_dir)},
            "policies": {"audio": "replace"},
            "summary": {"ready_for_upload": True, "upload_count": 1},
            "dependencies": [{
                "kind": "sticker",
                "path": str(sticker),
                "original_path": str(sticker),
                "decision": "upload",
                "size_bytes": sticker.stat().st_size,
                "references": [{"material_id": "visual-1"}],
            }],
        },
        package_path,
    )
    store = UserTemplateStore(tmp_path / "templates", libraries_root=tmp_path / "libraries")

    imported = store.import_transfer_package("user-a", "账号采集模板", package_path)

    assert imported["status"] == "READY"
    assert imported["profile"]["main_video"]["material_id"] == "video-material"
    assert store.list("user-b") == []
    with pytest.raises(FileNotFoundError):
        store.get("user-b", imported["template_id"])
    binding = store.render_binding("user-a", imported["template_id"])
    stored = json.loads(
        (Path(binding["draft_dir"]) / "draft_content.json").read_text(encoding="utf-8")
    )
    rewritten_sticker = Path(stored["materials"]["stickers"][0]["path"])
    assert rewritten_sticker.is_file()
    assert rewritten_sticker != sticker


def test_user_template_ignores_jianying_builtin_system_font_path(tmp_path):
    store = UserTemplateStore(tmp_path / "templates", libraries_root=tmp_path / "libraries")
    draft = _draft()
    draft["materials"]["texts"][0]["font_path"] = (
        r"D:\JianyingPro\Resources\Font\SystemFont\zh-hans.ttf"
    )
    created = store.create("user-a", "默认字体模板")
    store.upload_draft_file(
        "user-a",
        created["template_id"],
        "draft_content.json",
        json.dumps(draft, ensure_ascii=False).encode("utf-8"),
    )

    analyzed = store.analyze("user-a", created["template_id"])

    assert analyzed["status"] == "READY"
    assert analyzed["missing_resources"] == []


def test_user_template_still_requires_missing_custom_font(tmp_path):
    store = UserTemplateStore(tmp_path / "templates", libraries_root=tmp_path / "libraries")
    draft = _draft()
    draft["materials"]["texts"][0]["content"] = json.dumps(
        {
            "text": "第一句",
            "styles": [{
                "font": {
                    "path": r"C:\Users\editor\AppData\Local\JianyingPro\User Data\Cache\effect\123\custom.otf",
                    "id": "7244518590332801592",
                }
            }],
        },
        ensure_ascii=False,
    )
    created = store.create("user-a", "自定义字体模板")
    store.upload_draft_file(
        "user-a",
        created["template_id"],
        "draft_content.json",
        json.dumps(draft, ensure_ascii=False).encode("utf-8"),
    )

    analyzed = store.analyze("user-a", created["template_id"])

    assert analyzed["status"] == "NEEDS_RESOURCES"
    assert analyzed["missing_resources"][0]["kind"] == "font"
    assert analyzed["missing_resources"][0]["candidate_cache_paths"] == [
        "font/7244518590332801592",
        "effect/7244518590332801592",
    ]


def test_existing_builtin_font_false_positive_is_migrated_to_ready(tmp_path):
    store = UserTemplateStore(tmp_path / "templates", libraries_root=tmp_path / "libraries")
    created = store.create("user-a", "旧记录")
    meta_path = next((tmp_path / "templates").rglob("template.json"))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["status"] = "NEEDS_RESOURCES"
    meta["missing_resources"] = [{
        "resource_key": "old-system-font",
        "kind": "font",
        "original_path": r"C:\JianyingPro\Resources\Font\SystemFont\zh-hans.ttf",
        "identifiers": {},
        "candidate_cache_paths": [],
    }]
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    migrated = store.get("user-a", created["template_id"])

    assert migrated["status"] == "READY"
    assert migrated["missing_resources"] == []


def test_template_upload_rejects_parent_path(tmp_path):
    store = UserTemplateStore(tmp_path / "templates", libraries_root=tmp_path / "libraries")
    created = store.create("user-a", "安全模板")
    with pytest.raises(ValueError, match="路径不合法"):
        store.upload_draft_file("user-a", created["template_id"], "../secret.txt", b"x")


def test_uploaded_cache_resource_keeps_safe_tail_below_resource_id(tmp_path):
    payload = tmp_path / "payload"
    hashed = payload / "hash-folder"
    hashed.mkdir(parents=True)
    asset = hashed / "effect.json"
    asset.write_text("{}", encoding="utf-8")
    resolved = UserTemplateStore._uploaded_resource_target(
        payload,
        [asset],
        "C:/Cache/effect/123/hash-folder",
        {"resource_id": "123"},
    )
    escaped = UserTemplateStore._uploaded_resource_target(
        payload,
        [asset],
        "C:/Cache/effect/123/../../outside",
        {"resource_id": "123"},
    )
    assert resolved == hashed.resolve()
    assert escaped == payload.resolve()


def test_caption_detection_rejects_ambiguous_tracks():
    data = _draft()
    duplicate = json.loads(json.dumps(data["tracks"][1], ensure_ascii=False))
    duplicate["id"] = "other-caption-track"
    data["tracks"].append(duplicate)
    with pytest.raises(ValueError, match="多条相似字幕轨"):
        detect_caption_track(data)


def test_caption_detection_supports_flower_text_template_materials():
    data = _draft()
    data["materials"]["text_templates"] = [{
        "id": "flower-template",
        "text_info_resources": [{"text_material_id": "text-1"}],
    }]
    data["tracks"][1]["segments"] = [{
        "id": "flower-caption",
        "material_id": "flower-template",
        "target_timerange": {"start": 0, "duration": 4_000_000},
    }]
    detected = detect_caption_track(data)
    assert detected["base_material_id"] == "flower-template"
    assert detected["sample_texts"] == ["第一句"]


def test_caption_detection_does_not_treat_one_static_title_as_speech_captions():
    data = _draft()
    data["tracks"][1]["name"] = "顶部标题"
    data["tracks"][1]["segments"] = [data["tracks"][1]["segments"][0]]
    data["tracks"][1]["segments"][0]["target_timerange"] = {
        "start": 0,
        "duration": 4_000_000,
    }
    with pytest.raises(ValueError, match="无法自动确认字幕轨"):
        detect_caption_track(data)


def test_render_job_parses_template_subtitle_range():
    replacements = _build_subtitle_range_replacements({
        "subtitle_range_replacements": [{
            "track_index": 2,
            "base_segment_index": 3,
            "start_us": 0,
            "end_us": 5_000_000,
            "subtitles": [{"start_us": 100_000, "duration_us": 900_000, "text": "新字幕"}],
        }]
    })
    assert replacements[0].track_index == 2
    assert replacements[0].base_segment_index == 3
    assert replacements[0].subtitles[0].text == "新字幕"


def test_template_timeline_follows_video_without_extending_new_captions():
    data = {
        "duration": 4_000_000,
        "tracks": [
            {"type": "text", "segments": [{"target_timerange": {"start": 0, "duration": 4_000_000}}]},
            {"type": "text", "segments": [{"target_timerange": {"start": 0, "duration": 4_000_000}}]},
            {"type": "effect", "segments": [{"target_timerange": {"start": 0, "duration": 4_000_000}}]},
        ],
    }
    changed = _fit_timeline_duration(data, 6_000_000, protected_text_track_indexes={0})
    assert changed > 0
    assert data["duration"] == 6_000_000
    assert data["tracks"][0]["segments"][0]["target_timerange"]["duration"] == 4_000_000
    assert data["tracks"][1]["segments"][0]["target_timerange"]["duration"] == 6_000_000
    assert data["tracks"][2]["segments"][0]["target_timerange"]["duration"] == 6_000_000


def test_4b_template_job_preserves_template_visuals_and_replaces_dynamic_media(tmp_path):
    draft_dir = tmp_path / "template-draft"
    draft_dir.mkdir()
    (draft_dir / "draft_content.json").write_text("{}", encoding="utf-8")
    video = tmp_path / "base.mp4"
    audio = tmp_path / "voice.mp3"
    font = tmp_path / "font.ttf"
    for path in (video, audio, font):
        path.write_bytes(b"test")
    coordinator = ProjectPostprocessCoordinator.__new__(ProjectPostprocessCoordinator)
    coordinator.draft_root = tmp_path / "draft-output"
    coordinator.fonts = {"font": {"identity": "font", "path": str(font)}}
    coordinator.bgm_assets = {}
    coordinator.semantic_visual_library_root = tmp_path
    coordinator.semantic_visual_catalog = None
    item = {
        "row_key": "1",
        "outputs": {
            "base_video": {"managed_path": str(video), "metadata": {"duration_us": 6_000_000, "segment_count": 1}},
            "audio": {"managed_path": str(audio)},
        },
        "subtitles": {
            "status": "PREVIEW_READY",
            "render_cues": [{"start_us": 0, "duration_us": 1_000_000, "text": "新字幕"}],
            "style": {"font_id": "font"},
        },
        "settings": {"postprocess": {
            "font_identity": "font",
            "jianying_template": {
                "template_id": "template-1",
                "draft_dir": str(draft_dir),
                "profile": {
                    "draft_duration_us": 4_000_000,
                    "main_video": {"typed_track_index": 1, "segment_index": 2},
                    "caption_track": {"typed_track_index": 3, "base_segment_index": 4},
                },
            },
        }},
    }

    job = coordinator._build_draft_job(item, draft_name="模板成片", skip_export=True)

    assert job["source"] == {"type": "template", "template_draft_dir": str(draft_dir.resolve())}
    assert job["timeline_duration_us"] == 6_000_000
    assert job["remove_existing_audio"] is True
    assert job["video_replacements"][0]["track_index"] == 1
    assert job["video_replacements"][0]["media_path"] == str(video.resolve())
    assert job["subtitle_range_replacements"][0]["track_index"] == 3
    assert job["subtitle_range_replacements"][0]["subtitles"][0]["text"] == "新字幕"
    assert "captions" not in job
    assert "visual_overlays" not in job


def test_template_job_adds_required_main_video_when_template_has_no_video_track(tmp_path):
    draft_dir = tmp_path / "template-draft"
    draft_dir.mkdir()
    (draft_dir / "draft_content.json").write_text("{}", encoding="utf-8")
    video = tmp_path / "base.mp4"
    audio = tmp_path / "voice.mp3"
    font = tmp_path / "font.ttf"
    for path in (video, audio, font):
        path.write_bytes(b"test")
    coordinator = ProjectPostprocessCoordinator.__new__(ProjectPostprocessCoordinator)
    coordinator.draft_root = tmp_path / "draft-output"
    coordinator.fonts = {"font": {"identity": "font", "path": str(font)}}
    coordinator.bgm_assets = {}
    coordinator.semantic_visual_library_root = tmp_path
    coordinator.semantic_visual_catalog = None
    item = {
        "outputs": {
            "base_video": {"managed_path": str(video), "metadata": {"duration_us": 6_000_000}},
            "audio": {"managed_path": str(audio)},
        },
        "subtitles": {
            "status": "PREVIEW_READY",
            "render_cues": [{"start_us": 0, "duration_us": 1_000_000, "text": "新字幕"}],
            "style": {"font_id": "font"},
        },
        "settings": {"postprocess": {
            "font_identity": "font",
            "jianying_template": {
                "template_id": "template-no-video",
                "draft_dir": str(draft_dir),
                "profile": {
                    "draft_duration_us": 4_000_000,
                    "main_video": None,
                    "caption_track": {"typed_track_index": 0, "base_segment_index": 0},
                },
            },
        }},
    }

    job = coordinator._build_draft_job(item, draft_name="无视频轨模板成片", skip_export=True)

    assert "video_replacements" not in job
    assert job["visual_overlays"][0]["track_name"] == "项目主视频"
    assert job["visual_overlays"][0]["optional"] is False
    assert job["visual_overlays"][0]["video_path"] == str(video.resolve())


def test_new_frontend_exposes_compact_template_modal():
    root = Path(__file__).resolve().parents[1]
    page = (root / "apps" / "processor" / "frontend" / "new" / "index.html").read_text(encoding="utf-8")
    manager = (root / "apps" / "processor" / "frontend" / "new" / "template-manager.js").read_text(encoding="utf-8")
    assert 'id="btn-jianying-template"' in page
    assert 'id="jianying-template-modal"' in page
    assert 'src="/app-static/new/template-manager.js"' in page
    assert "DRAFT_FILES" in manager
    assert "showDirectoryPicker" in manager
    assert "window.isSecureContext" in manager
    assert "webkitdirectory" in manager
    assert "browserDraftFiles" in manager
    assert "请通过 HTTPS" not in manager
    assert "缺少 ${template.missing_resources?.length || 0} 个花字资源" not in manager
    assert "RESOURCE_KIND_LABELS" in manager


def test_new_frontend_exposes_account_template_center():
    root = Path(__file__).resolve().parents[1]
    page = (root / "apps" / "processor" / "frontend" / "new" / "templates.html").read_text(encoding="utf-8")
    collector = (root / "apps" / "processor" / "frontend" / "new" / "template-collector.js").read_text(encoding="utf-8")
    assert "我的剪映模板" in page
    assert "/api/new/jianying-templates" in page
    assert "jyd-pending-template-id" in page
    assert "无视频轨的纯样式草稿也可以保存" in page
    assert "从本机剪映导入" in page
    assert 'src="/app-static/new/template-collector.js"' in page
    assert 'mode: "template_center"' in collector
    assert "/api/new/jianying-template-import-tickets" in collector
    assert "template_import_ticket" in collector


def test_template_api_binds_ready_template_to_current_account_project(tmp_path):
    settings = WebApiSettings(
        storage_root=tmp_path / "storage",
        template_library_root=tmp_path / "template-library",
        default_draft_root=tmp_path / "draft-output",
        audio_library_root=tmp_path / "audio-library",
        admin_password="admin-pass",
        admin_session_secret="admin-secret",
        auth_authority=False,
        auth_server_url="http://127.0.0.1:8000",
        execution_mode="agent",
    )
    for directory in (
        settings.storage_root,
        settings.template_library_root,
        settings.default_draft_root,
        settings.audio_library_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    user = {"user_id": "template-user", "username": "tester", "enabled": True}
    collector_draft = tmp_path / "collector-source"
    collector_draft.mkdir()
    (collector_draft / "draft_content.json").write_text(
        json.dumps(_draft(), ensure_ascii=False), encoding="utf-8"
    )
    collector_package = tmp_path / "collector-template.zip"
    build_transfer_package(
        {
            "mode": "template_center",
            "plan_id": "api-plan",
            "report_id": "api-report",
            "draft": {"name": "API 草稿", "analyzed_draft_dir": str(collector_draft)},
            "policies": {"audio": "replace"},
            "summary": {"ready_for_upload": True, "upload_count": 0},
            "dependencies": [],
        },
        collector_package,
    )

    def verify(_client, token):
        return user if token == "center-token" else None

    with patch(
        "jyd_probe.auth_center.AuthCenterClient.login",
        return_value={"access_token": "center-token", "user": user},
    ), patch("jyd_probe.auth_center.AuthCenterClient.verify", new=verify):
        with TestClient(create_app(settings)) as client:
            assert client.post("/api/auth/login", json={"username": "tester", "password": "pass"}).status_code == 200
            ticket_response = client.post(
                "/api/new/jianying-template-import-tickets",
                json={"name": "采集器账号模板"},
            )
            assert ticket_response.status_code == 201
            ticket = ticket_response.json()["ticket"]
            package_bytes = collector_package.read_bytes()
            client.cookies.clear()
            imported_response = client.post(
                f"/api/new/jianying-template-imports/{ticket}",
                content=package_bytes,
                headers={
                    "Content-Type": "application/zip",
                    "X-Package-SHA256": hashlib.sha256(package_bytes).hexdigest(),
                },
            )
            assert imported_response.status_code == 201, imported_response.text
            assert imported_response.json()["template"]["name"] == "采集器账号模板"
            assert client.post(
                f"/api/new/jianying-template-imports/{ticket}",
                content=package_bytes,
                headers={"Content-Type": "application/zip"},
            ).status_code == 401
            assert client.post("/api/auth/login", json={"username": "tester", "password": "pass"}).status_code == 200
            assert any(
                item["name"] == "采集器账号模板"
                for item in client.get("/api/new/jianying-templates").json()["templates"]
            )
            created = client.post("/api/new/jianying-templates", json={"name": "上传1"})
            assert created.status_code == 201
            template_id = created.json()["template_id"]
            uploaded = client.put(
                f"/api/new/jianying-templates/{template_id}/draft-files",
                params={"path": "draft_content.json"},
                content=json.dumps(_draft(), ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/octet-stream"},
            )
            assert uploaded.status_code == 200
            assert client.post(f"/api/new/jianying-templates/{template_id}/analyze").json()["status"] == "READY"
            project_response = client.post(
                "/api/new/projects",
                json={"name": "模板项目", "items": [{"row_key": "1", "script_text": "测试脚本"}]},
            )
            assert project_response.status_code == 201, project_response.text
            project = project_response.json()
            bound = client.put(
                f"/api/new/projects/{project['project_id']}/jianying-template",
                json={"template_id": template_id},
            )
            assert bound.status_code == 200
            assert bound.json()["settings"]["jianying_template"]["template_id"] == template_id
            assert client.delete(f"/api/new/jianying-templates/{template_id}").status_code == 409
            assert client.put(
                f"/api/new/projects/{project['project_id']}/jianying-template",
                json={"template_id": ""},
            ).status_code == 200
            assert client.delete(f"/api/new/jianying-templates/{template_id}").status_code == 200

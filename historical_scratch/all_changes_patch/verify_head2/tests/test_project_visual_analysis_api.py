from __future__ import annotations

import shutil
from pathlib import Path
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from jyd_probe.web_api import WebApiSettings, create_app


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_catalog_analysis_review_and_revision_api() -> None:
    root = PROJECT_ROOT / "runtime" / "test_tmp" / f"visual_api_{uuid.uuid4().hex}"
    settings = WebApiSettings(
        storage_root=root / "storage",
        template_library_root=root / "templates",
        default_draft_root=root / "drafts",
        audio_library_root=root / "audio",
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
    user = {"user_id": "visual-api-user", "username": "tester", "enabled": True}

    def verify(_client, token):
        return user if token == "center-token" else None

    def analyze(
        _client,
        _token,
        original_script,
        *,
        force_refresh=False,
        visual_context=None,
    ):
        anchor = visual_context["anchors"][0]
        return {
            "schema_version": "jyd.content-analysis.v1",
            "prompt_version": "jyd.content-analysis.prompt.v7",
            "script_sha256": __import__("hashlib").sha256(
                original_script.encode("utf-8")
            ).hexdigest(),
            "script_length": len(original_script),
            "model": "doubao-test",
            "overall_status": "SUCCESS",
            "music_analysis_status": "SUCCESS",
            "subtitle_analysis_status": "SUCCESS",
            "visual_analysis_status": "SUCCESS",
            "music_intent": {"primary_scene": "health_education"},
            "subtitle_units": [
                {
                    "start": 0,
                    "end": len(original_script),
                    "text": original_script,
                    "kind": "phrase",
                    "bind": "none",
                    "break_after": "allow",
                }
            ],
            "visual_catalog_version": visual_context["catalog_version"],
            "visual_plan": [
                {
                    "anchor_id": anchor["anchor_id"],
                    "concept_id": anchor["allowed_concepts"][0],
                    "priority": 2,
                }
            ],
            "errors": {"music": None, "subtitle": None, "visual": None},
            "provider_attempts": 1,
            "cache_hit": False,
            "cacheable": True,
            "error": None,
        }

    try:
        with (
            patch(
                "jyd_probe.auth_center.AuthCenterClient.login",
                return_value={"access_token": "center-token", "user": user},
            ),
            patch("jyd_probe.auth_center.AuthCenterClient.verify", new=verify),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.analyze_workbench_content",
                new=analyze,
            ),
            TestClient(create_app(settings)) as client,
        ):
            assert client.post(
                "/api/auth/login", json={"username": "tester", "password": "pass123"}
            ).status_code == 200
            catalog_response = client.get("/api/new/semantic-visuals/catalog")
            assert catalog_response.status_code == 200
            catalog = catalog_response.json()
            egg = next(asset for asset in catalog["assets"] if asset["asset_id"] == "egg.boiled.01")
            assert client.get(egg["preview_url"]).headers["content-type"].startswith("image/png")

            project = client.post(
                "/api/new/projects",
                json={
                    "name": "视觉 API",
                    "items": [{"row_key": "1", "script_text": "每天吃一个鸡蛋"}],
                },
            ).json()
            item_id = project["items"][0]["item_id"]
            analyzed = client.post(
                f"/api/new/projects/{project['project_id']}/visual-analysis",
                json={},
            )
            assert analyzed.status_code == 200, analyzed.text
            analyzed_project = analyzed.json()
            analysis = analyzed_project["items"][0]["visual_analysis"]
            assert analysis["analysis_status"] == "SUCCESS"
            assert analysis["mapping_status"] == "FAILED"

            saved = client.put(
                f"/api/new/projects/{project['project_id']}/items/{item_id}/visual-overlays",
                json={
                    "revision": analyzed_project["revision"],
                    "overlays": [
                        {
                            "overlay_id": "vo-manual-api",
                            "candidate_id": analysis["candidate_request"]["candidates"][0]["candidate_id"],
                            "concept_id": "food.egg",
                            "asset_id": "egg.boiled.01",
                            "enabled": True,
                            "locked": True,
                            "corner": "top_right",
                            "scale": 0.28,
                            "opacity": 1.0,
                            "start_us": 0,
                            "duration_us": 2_000_000,
                            "timing_source": "minimax_raw_cue_phrase_span",
                            "timing_mode": "sentence",
                            "sentence_char_start": 0,
                            "sentence_char_end": 8,
                            "sentence_text": "每天吃一个鸡蛋",
                            "phrase_char_start": 0,
                            "phrase_char_end": 8,
                            "phrase_text": "每天吃一个鸡蛋",
                            "list_index": None,
                            "list_size": None,
                            "segment_boundary_us": None,
                            "usage": "explicit",
                        }
                    ],
                },
            )
            assert saved.status_code == 200, saved.text
            frozen = saved.json()["items"][0]["visual_analysis"]["recipe"]["overlays"][0]
            assert frozen["manual"] is True
            assert frozen["selection_mode"] == "manual"
            assert frozen["locked"] is True
            assert frozen["timing_mode"] == "sentence"
            assert frozen["sentence_text"] == "每天吃一个鸡蛋"
            recipe = saved.json()["items"][0]["visual_analysis"]["recipe"]
            assert recipe["timing_policy_version"] == "sentence-v1"
            assert recipe["used_asset_ids"] == ["egg.boiled.01"]

            video_asset = next(
                asset for asset in catalog["assets"] if asset["media_type"] == "video"
            )
            saved_video = client.put(
                f"/api/new/projects/{project['project_id']}/items/{item_id}/visual-overlays",
                json={
                    "revision": saved.json()["revision"],
                    "overlays": [
                        {
                            "overlay_id": "vo-legacy-loop-video",
                            "candidate_id": "legacy-video-candidate",
                            "concept_id": video_asset["concept_ids"][0],
                            "asset_id": video_asset["asset_id"],
                            "enabled": True,
                            "locked": True,
                            "corner": "center",
                            "scale": 1.0,
                            "opacity": 1.0,
                            "start_us": 0,
                            "duration_us": 2_000_000,
                            "source_start_us": 0,
                            "mute": True,
                            "loop": True,
                            "fit": "cover",
                        }
                    ],
                },
            )
            assert saved_video.status_code == 200, saved_video.text
            frozen_video = saved_video.json()["items"][0]["visual_analysis"]["recipe"]["overlays"][0]
            assert frozen_video["media_type"] == "video"
            assert "loop" not in frozen_video
            assert frozen_video["loop_to_target"] is False

            conflict = client.put(
                f"/api/new/projects/{project['project_id']}/items/{item_id}/visual-overlays",
                json={"revision": project["revision"], "overlays": []},
            )
            assert conflict.status_code == 409
    finally:
        shutil.rmtree(root, ignore_errors=True)

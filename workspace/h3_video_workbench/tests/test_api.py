from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from h3_workbench.api import create_app
from h3_workbench.config import Settings
from h3_workbench.postprocess import H3JYDAssets


class FakeCloud:
    def __init__(self) -> None:
        self.uploads = 0
        self.prepared: dict | None = None
        self.prepare_calls: list[dict] = []
        self.confirms = 0
        self.regeneration_confirms = 0
        self.retry_confirms = 0
        self.cancels = 0
        self.downloads = 0
        self.audio_approvals = 0
        self.audio_reviewed = False
        self.upload_paths: list[Path] = []

    def login(self, username: str, password: str) -> dict:
        assert username == "tester"
        assert password == "password123"
        return {"access_token": "cloud-token", "user": {"id": 7, "username": username}}

    def accounts(self, token: str) -> dict:
        assert token == "cloud-token"
        return {
            "accounts": [
                {
                    "id": 3,
                    "label": "H3 一号",
                    "selectable": True,
                    "availability": "AVAILABLE",
                    "instance_type": "plus",
                    "available_slots": 2,
                    "max_concurrent_tasks": 3,
                }
            ],
            "default_selected_account_ids": [3],
            "adapter_capability": {
                "max_user_reference_images": 5,
                "continuity_modes": [
                    {"value": "fast", "label": "快速并行（默认）"},
                    {"value": "soft_chain", "label": "连续模式（尾帧串行）"},
                ],
                "aspect_ratios": [
                    {
                        "value": "9:16 (Portrait Widescreen)",
                        "label": "9:16 竖屏",
                    },
                    {
                        "value": "16:9 (Widescreen)",
                        "label": "16:9 横屏",
                    },
                ],
                "megapixels": {"minimum": 0.2, "maximum": 2, "step": 0.1},
            },
        }

    def audio_sources(self, token: str) -> dict:
        assert token == "cloud-token"
        return {
            "schema": "runninghub.h3-audio-sources.v1",
            "sources": [
                {
                    "audio_batch_id": "audio-batch-1",
                    "audio_item_id": "audio-item-1",
                    "audio_generation_version": 2,
                    "batch_name": "已生成口播",
                    "row_key": "ROW-001",
                    "script_text": "真正的优势，是长期把复杂的事情做对。",
                    "status": "SUCCESS" if self.audio_reviewed else "AWAITING_REVIEW",
                    "created_at": "2026-08-22T00:00:00+00:00",
                    "audio_download_url": (
                        "/api/workbench/audio-batches/audio-batch-1"
                        "/items/audio-item-1/audio"
                    ),
                }
            ],
        }

    def approve_audio_source(
        self,
        token: str,
        *,
        audio_batch_id: str,
        audio_item_id: str,
        audio_generation_version: int,
    ) -> dict:
        assert token == "cloud-token"
        assert audio_batch_id == "audio-batch-1"
        assert audio_item_id == "audio-item-1"
        assert audio_generation_version == 2
        self.audio_approvals += 1
        self.audio_reviewed = True
        return {
            "audio_batch_id": audio_batch_id,
            "audio_item_id": audio_item_id,
            "audio_generation_version": audio_generation_version,
            "status": "SUCCESS",
            "reviewed_at": "2026-08-23T00:00:00+00:00",
        }

    def upload_asset(self, token, kind, path, original_name, content_type):
        del content_type
        assert token == "cloud-token"
        assert kind in {"image", "video"}
        assert path.is_file()
        self.upload_paths.append(path.resolve())
        self.uploads += 1
        return {"asset_id": f"asset-{self.uploads}-{original_name}"}

    def prepare(self, token: str, payload: dict) -> dict:
        assert token == "cloud-token"
        self.prepared = payload
        self.prepare_calls.append(payload)
        return {
            "batch_id": "h3-batch-1",
            "status": "AWAITING_COST_CONFIRMATION",
            "fee_snapshot": {
                "segment_count": 2,
                "estimated_paid_calls": 2,
                "reusable_result_count": 0,
            },
            "items": [],
        }

    def confirm(self, token: str, batch_id: str) -> dict:
        assert token == "cloud-token"
        assert batch_id == "h3-batch-1"
        self.confirms += 1
        return {
            "batch_id": batch_id,
            "status": "ACTIVE",
            "fee_snapshot": {"segment_count": 2, "estimated_paid_calls": 2},
            "items": [],
        }

    def batch(self, token: str, batch_id: str) -> dict:
        assert token == "cloud-token"
        return {"batch_id": batch_id, "status": "ACTIVE", "items": []}

    def prepare_regeneration(self, token: str, segment_id: str) -> dict:
        assert token == "cloud-token"
        return {
            "target_segment_id": segment_id,
            "affected_segment_indexes": [0, 1],
            "estimated_paid_calls": 2,
            "cascade_required": True,
            "historical_files_will_be_retained": True,
            "quote_token": "regen-quote-1",
        }

    def confirm_regeneration(
        self,
        token: str,
        segment_id: str,
        *,
        request_key: str,
        quote_token: str,
    ) -> dict:
        assert token == "cloud-token"
        assert segment_id == "segment-1"
        assert request_key == "regen-request-1"
        assert quote_token == "regen-quote-1"
        self.regeneration_confirms += 1
        return {
            "batch_id": "h3-batch-1",
            "status": "ACTIVE",
            "regeneration": {"request_key": request_key},
            "items": [
                {
                    "row_id": "ROW-001",
                    "segments": [
                        {"segment_id": "segment-1", "status": "PENDING"},
                        {
                            "segment_id": "segment-2",
                            "status": "WAITING_REGENERATION_DEPENDENCY",
                        },
                    ],
                }
            ],
        }

    def prepare_retry(self, token: str, segment_id: str) -> dict:
        assert token == "cloud-token"
        return {
            "segment_id": segment_id,
            "retry_scope": "provider_resubmit",
            "estimated_paid_calls": 1,
            "quote_token": "retry-quote-1",
        }

    def confirm_retry(
        self,
        token: str,
        segment_id: str,
        *,
        request_key: str,
        quote_token: str,
        cost_confirmed: bool,
    ) -> dict:
        assert token == "cloud-token"
        assert segment_id == "segment-1"
        assert request_key == "retry-request-1"
        assert quote_token == "retry-quote-1"
        assert cost_confirmed is True
        self.retry_confirms += 1
        return {
            "batch_id": "h3-batch-1",
            "status": "ACTIVE",
            "retry": {"request_key": request_key},
            "items": [
                {
                    "row_id": "ROW-001",
                    "segments": [{"segment_id": "segment-1", "status": "PENDING"}],
                }
            ],
        }

    def cancel_segment(
        self,
        token: str,
        segment_id: str,
        *,
        request_key: str,
    ) -> dict:
        assert token == "cloud-token"
        assert segment_id == "segment-1"
        assert request_key == "cancel-request-1"
        self.cancels += 1
        return {
            "batch_id": "h3-batch-1",
            "status": "FAILED",
            "cancellation": {"request_key": request_key},
            "items": [
                {
                    "row_id": "ROW-001",
                    "segments": [
                        {
                            "segment_id": "segment-1",
                            "status": "CANCELLED",
                            "can_retry": True,
                        }
                    ],
                }
            ],
        }

    def download(self, token: str, relative_url: str, destination: Path) -> Path:
        assert token == "cloud-token"
        self.downloads += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative_url.endswith("raw-cues"):
            destination.write_text(
                '[{"text":"台词。","start_seconds":0,"end_seconds":5}]',
                encoding="utf-8",
            )
        else:
            destination.write_bytes(b"downloaded-media")
        return destination


class FakeJYD:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def import_handoff(self, **kwargs):
        self.calls.append(kwargs)
        return {"project_id": "jyd-project-1", "status": "BASE_VIDEO_READY"}


def _client(tmp_path: Path) -> tuple[TestClient, FakeCloud, FakeJYD, Settings]:
    settings = Settings(
        cloud_base_url="https://cloud.example",
        runtime_dir=tmp_path,
        upload_dir=tmp_path / "uploads",
        result_dir=tmp_path / "results",
        database_path=tmp_path / "h3.sqlite3",
        jyd_root=tmp_path / "jyd",
        jyd_database_path=tmp_path / "jyd" / "control.db",
        jyd_python="python",
    )
    cloud = FakeCloud()
    jyd = FakeJYD()
    return TestClient(create_app(settings, cloud, jyd)), cloud, jyd, settings


def test_frontend_serves_adapter_driven_row_overrides(tmp_path: Path) -> None:
    client, _, _, _ = _client(tmp_path)

    index = client.get("/")
    script = client.get("/assets/app.js")
    override_styles = client.get("/assets/overrides.css")

    assert index.status_code == 200
    assert script.status_code == 200
    assert override_styles.status_code == 200
    assert "adapter_capability" in script.text
    assert "state.capability?.generation_tail_seconds" in script.text
    assert "data-f=\"rowContinuity\"" in script.text
    assert "data-f=\"rowAspect\"" in script.text
    assert "data-f=\"rowMegapixels\"" in script.text
    assert "h3-audio-sources" not in script.text
    assert "/api/audio-sources" in script.text
    assert "data-f=\"audioSource\"" in script.text
    assert "function contractLocked()" in script.text
    assert "feeSummary(snap.fee_snapshot)" in script.text
    assert "workflowId" not in script.text
    assert "最终保留模型返回的完整同步音画" in index.text
    assert "最终保留 H3 返回的完整同步音画" in script.text
    assert "下载验收视频" in script.text
    assert "/segments/${encodeURIComponent(s.segment_id)}/video" in script.text
    assert "裁回真实音频时长" not in index.text
    assert "余量只会从每段尾部裁掉" not in script.text


def test_local_ui_contract_upload_prepare_and_explicit_confirm(tmp_path: Path) -> None:
    client, cloud, _, settings = _client(tmp_path)
    login = client.post(
        "/api/session/login",
        json={"username": "tester", "password": "password123"},
    )
    assert login.status_code == 200
    project = client.post("/api/projects", json={"name": "H3 本地项目"}).json()
    project_id = project["id"]
    sources = client.get("/api/audio-sources")
    assert sources.status_code == 200
    assert sources.json()["sources"][0]["audio_item_id"] == "audio-item-1"
    preview = client.get(
        "/api/audio-sources/audio-batch-1/audio-item-1/audio"
    )
    assert preview.status_code == 200
    assert preview.content == b"downloaded-media"
    assert not any(path.is_file() for path in settings.upload_dir.rglob("*"))

    for index in range(5):
        uploaded = client.post(
            f"/api/projects/{project_id}/identity-images",
            files={"file": (f"person-{index}.png", b"png", "image/png")},
        )
        assert uploaded.status_code == 200, uploaded.text
    sixth = client.post(
        f"/api/projects/{project_id}/identity-images",
        files={"file": ("sixth.png", b"png", "image/png")},
    )
    assert sixth.status_code == 409
    assert not any(path.is_file() for path in settings.upload_dir.rglob("*"))

    row = {
        "row_id": "ROW-001",
        "script_text": "真正的优势，是长期把复杂的事情做对。",
        "video_asset_id": "",
        "audio_batch_id": "audio-batch-1",
        "audio_item_id": "audio-item-1",
        "audio_generation_version": 2,
        "overrides": {
            "user_direction": "本行语气更坚定",
            "continuity_mode": "soft_chain",
            "aspect_ratio": "16:9 (Widescreen)",
            "megapixels": 0.8,
        },
    }
    saved = client.put(
        f"/api/projects/{project_id}",
        json={
            "name": "H3 本地项目",
            "defaults": project["defaults"],
            "rows": [row],
            "selected_account_ids": [3],
        },
    )
    assert saved.status_code == 200
    video = client.post(
        f"/api/projects/{project_id}/rows/ROW-001/reference-video",
        files={"file": ("reference.mp4", b"video", "video/mp4")},
    )
    assert video.status_code == 200, video.text
    assert not any(path.is_file() for path in settings.upload_dir.rglob("*"))

    reviewed = client.post(f"/api/projects/{project_id}/review-audio")
    assert reviewed.status_code == 200, reviewed.text
    assert cloud.audio_approvals == 1
    assert reviewed.json()["reviewed"][0]["status"] == "SUCCESS"

    quoted = client.post(f"/api/projects/{project_id}/prepare")
    assert quoted.status_code == 200, quoted.text
    assert cloud.confirms == 0
    assert cloud.prepared["reference_image_asset_ids"] == [
        f"asset-{index}-person-{index - 1}.png" for index in range(1, 6)
    ]
    assert cloud.prepared["rows"][0]["video_asset_id"].endswith("reference.mp4")
    assert cloud.prepared["rows"][0]["overrides"] == {
        "user_direction": "本行语气更坚定",
        "continuity_mode": "soft_chain",
        "aspect_ratio": "16:9 (Widescreen)",
        "megapixels": 0.8,
    }
    assert cloud.prepared["defaults"]["generation_tail_seconds"] == 0.5
    first_request_key = cloud.prepared["request_key"]
    quoted_project = quoted.json()
    no_op_save = client.put(
        f"/api/projects/{project_id}",
        json={
            "name": quoted_project["name"],
            "defaults": quoted_project["defaults"],
            "rows": quoted_project["rows"],
            "selected_account_ids": quoted_project["selected_account_ids"],
        },
    )
    assert no_op_save.status_code == 200
    assert no_op_save.json()["remote_batch_id"] == "h3-batch-1"
    repeated = client.post(f"/api/projects/{project_id}/prepare")
    assert repeated.status_code == 200
    assert cloud.prepared["request_key"] == first_request_key

    edited = client.get(f"/api/projects/{project_id}").json()
    edited["rows"][0]["script_text"] += "继续向前。"
    edited_save = client.put(
        f"/api/projects/{project_id}",
        json={
            "name": edited["name"],
            "defaults": edited["defaults"],
            "rows": edited["rows"],
            "selected_account_ids": edited["selected_account_ids"],
        },
    )
    assert edited_save.status_code == 200
    assert edited_save.json()["remote_batch_id"] is None
    assert edited_save.json()["snapshot"] is None
    stale_confirm = client.post(
        f"/api/projects/{project_id}/confirm",
        json={"cost_confirmed": True},
    )
    assert stale_confirm.status_code == 409
    assert cloud.confirms == 0
    assert client.post(f"/api/projects/{project_id}/prepare").status_code == 200
    assert cloud.prepared["request_key"] != first_request_key

    rejected = client.post(
        f"/api/projects/{project_id}/confirm",
        json={"cost_confirmed": False},
    )
    assert rejected.status_code == 409
    confirmed = client.post(
        f"/api/projects/{project_id}/confirm",
        json={"cost_confirmed": True},
    )
    assert confirmed.status_code == 200
    assert cloud.confirms == 1

    submitted = client.get(f"/api/projects/{project_id}").json()
    submitted["defaults"]["generation_tail_seconds"] = 0.25
    locked = client.put(
        f"/api/projects/{project_id}",
        json={
            "name": submitted["name"],
            "defaults": submitted["defaults"],
            "rows": submitted["rows"],
            "selected_account_ids": submitted["selected_account_ids"],
        },
    )
    assert locked.status_code == 409
    assert "已锁定" in locked.json()["detail"]

    database_bytes = settings.database_path.read_bytes()
    assert b"password123" not in database_bytes
    assert b"cloud-token" not in database_bytes


def test_reference_video_upload_hashes_untrusted_row_id_directory(tmp_path: Path) -> None:
    client, cloud, _, settings = _client(tmp_path)
    client.post(
        "/api/session/login",
        json={"username": "tester", "password": "password123"},
    )
    project = client.post("/api/projects", json={"name": "路径安全"}).json()
    row_id = r"..\..\outside"
    saved = client.put(
        f"/api/projects/{project['id']}",
        json={
            "name": project["name"],
            "defaults": project["defaults"],
            "rows": [
                {
                    "row_id": row_id,
                    "script_text": "安全路径测试。",
                    "video_asset_id": "",
                    "audio_batch_id": "audio-batch-1",
                    "audio_item_id": "audio-item-1",
                    "audio_generation_version": 2,
                }
            ],
            "selected_account_ids": [3],
        },
    )
    assert saved.status_code == 200, saved.text

    uploaded = client.post(
        f"/api/projects/{project['id']}/rows/{quote(row_id, safe='')}"
        "/reference-video",
        files={"file": ("reference.mp4", b"video", "video/mp4")},
    )

    assert uploaded.status_code == 200, uploaded.text
    assert cloud.upload_paths[-1].is_relative_to(settings.upload_dir.resolve())
    assert not any(path.is_file() for path in settings.upload_dir.rglob("*"))
    assert not (settings.upload_dir.parent / "outside").exists()


def test_completed_segment_can_be_downloaded_for_review_without_persisting_temp_file(
    tmp_path: Path,
) -> None:
    client, cloud, _, settings = _client(tmp_path)
    client.post(
        "/api/session/login",
        json={"username": "tester", "password": "password123"},
    )
    project = client.post("/api/projects", json={"name": "成片验收"}).json()
    client.app.state.store.update(
        "7",
        project["id"],
        snapshot={
            "status": "SUCCESS",
            "items": [
                {
                    "row_id": "ROW-001",
                    "segments": [
                        {
                            "segment_id": "segment-1",
                            "index": 0,
                            "status": "SUCCESS",
                            "normalized_video_download_url": "/segment-1/video",
                        }
                    ],
                }
            ],
        },
    )

    response = client.get(
        f"/api/projects/{project['id']}/segments/segment-1/video"
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("video/mp4")
    assert response.content == b"downloaded-media"
    assert cloud.downloads == 1
    assert not any(path.is_file() for path in settings.upload_dir.rglob("*"))


def test_completed_row_preserves_h3_audio_builds_split_assets_and_enters_jyd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, _, jyd, _ = _client(tmp_path)
    assert client.post(
        "/api/session/login",
        json={"username": "tester", "password": "password123"},
    ).status_code == 200
    project = client.post("/api/projects", json={"name": "H3 交接"}).json()
    row = {
        "row_id": "ROW-001",
        "script_text": "台词。",
        "video_asset_id": "video-1",
        "audio_batch_id": "audio-batch",
        "audio_item_id": "audio-item",
        "audio_generation_version": 1,
        "overrides": {},
    }
    assert client.put(
        f"/api/projects/{project['id']}",
        json={
            "name": project["name"],
            "defaults": project["defaults"],
            "rows": [row],
            "selected_account_ids": [3],
        },
    ).status_code == 200
    client.app.state.store.update(
        "7",
        project["id"],
        snapshot={
            "status": "SUCCESS",
            "items": [
                {
                    "row_id": "ROW-001",
                    "authoritative_audio_download_url": "/audio",
                    "raw_cues_download_url": "/raw-cues",
                    "segments": [
                        {
                            "segment_id": "segment-1",
                            "index": 0,
                            "script_text": "台词。",
                            "status": "SUCCESS",
                            "normalized_video_download_url": "/segment-1/video",
                        }
                    ],
                }
            ],
        },
    )

    def fake_prepare(*, segment_paths, segment_texts, script_text, target_dir):
        assert len(segment_paths) == 1
        assert segment_texts == ["台词。"]
        assert script_text == "台词。"
        target_dir.mkdir(parents=True, exist_ok=True)
        master = target_dir / "h3-master-av.mp4"
        silent = target_dir / "h3-base-video-silent.mp4"
        audio = target_dir / "h3-authoritative-full.wav"
        cues = target_dir / "h3-segment-cue-windows.json"
        master.write_bytes(b"h3-av")
        silent.write_bytes(b"silent-base")
        audio.write_bytes(b"h3-audio")
        cues.write_text(
            '[{"text":"台词。","start_us":0,"end_us":5000000}]',
            encoding="utf-8",
        )
        return H3JYDAssets(master, silent, audio, cues, (5.0,))

    monkeypatch.setattr("h3_workbench.api.prepare_h3_jyd_assets", fake_prepare)
    response = client.post(
        f"/api/projects/{project['id']}/rows/ROW-001/jyd-handoff"
    )
    assert response.status_code == 200, response.text
    assert response.json()["jyd_import"]["project_id"] == "jyd-project-1"
    assert len(jyd.calls) == 1
    manifest = json.loads(Path(jyd.calls[0]["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "h3.jyd_handoff.v2"
    assert manifest["base_video"]["audio_policy"] == "separate_h3_generated_audio"
    assert manifest["authoritative_audio"]["source"] == "h3_generated_audio"
    assert manifest["authoritative_audio"]["reuse_once"] is True


def test_jyd_handoff_rejects_non_contiguous_or_duplicate_cloud_segments(
    tmp_path: Path,
) -> None:
    client, cloud, _, _ = _client(tmp_path)
    client.post(
        "/api/session/login",
        json={"username": "tester", "password": "password123"},
    )
    project = client.post("/api/projects", json={"name": "损坏分段"}).json()
    row = {
        "row_id": "ROW-001",
        "script_text": "台词。",
        "video_asset_id": "video-1",
        "audio_batch_id": "audio-batch",
        "audio_item_id": "audio-item",
        "audio_generation_version": 1,
        "overrides": {},
    }
    client.app.state.store.update("7", project["id"], rows=[row])
    client.app.state.store.update(
        "7",
        project["id"],
        snapshot={
            "status": "SUCCESS",
            "items": [
                {
                    "row_id": "ROW-001",
                    "authoritative_audio_download_url": "/audio",
                    "raw_cues_download_url": "/raw-cues",
                    "segments": [
                        {
                            "segment_id": "segment-1",
                            "index": 0,
                            "status": "SUCCESS",
                            "normalized_video_download_url": "/segment-1/video",
                        },
                        {
                            "segment_id": "segment-2",
                            "index": 2,
                            "status": "SUCCESS",
                            "normalized_video_download_url": "/segment-2/video",
                        },
                    ],
                }
            ],
        },
    )

    response = client.post(
        f"/api/projects/{project['id']}/rows/ROW-001/jyd-handoff"
    )

    assert response.status_code == 409
    assert "不连续" in response.json()["detail"]
    assert cloud.downloads == 0


def test_local_regeneration_proxy_keeps_preview_and_paid_confirm_separate(
    tmp_path: Path,
) -> None:
    client, cloud, _, _ = _client(tmp_path)
    client.post(
        "/api/session/login",
        json={"username": "tester", "password": "password123"},
    )
    project = client.post("/api/projects", json={"name": "级联重生成"}).json()
    project_id = project["id"]
    client.app.state.store.update(
        "7",
        project_id,
        remote_batch_id="h3-batch-1",
        snapshot={
            "batch_id": "h3-batch-1",
            "status": "SUCCESS",
            "items": [
                {
                    "row_id": "ROW-001",
                    "segments": [
                        {"segment_id": "segment-1", "status": "SUCCESS"},
                        {"segment_id": "segment-2", "status": "SUCCESS"},
                    ],
                }
            ],
        },
    )

    preview = client.post(
        f"/api/projects/{project_id}/segments/segment-1/regeneration/prepare"
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["estimated_paid_calls"] == 2
    assert cloud.regeneration_confirms == 0

    rejected = client.post(
        f"/api/projects/{project_id}/segments/segment-1/regeneration/confirm",
        json={
            "request_key": "regen-request-1",
            "quote_token": "regen-quote-1",
            "cost_confirmed": False,
        },
    )
    assert rejected.status_code == 409
    assert cloud.regeneration_confirms == 0

    confirmed = client.post(
        f"/api/projects/{project_id}/segments/segment-1/regeneration/confirm",
        json={
            "request_key": "regen-request-1",
            "quote_token": "regen-quote-1",
            "cost_confirmed": True,
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert cloud.regeneration_confirms == 1
    assert confirmed.json()["snapshot"]["items"][0]["segments"][1]["status"] == (
        "WAITING_REGENERATION_DEPENDENCY"
    )


def test_local_retry_proxy_requires_preview_before_paid_confirm(tmp_path: Path) -> None:
    client, cloud, _, _ = _client(tmp_path)
    client.post(
        "/api/session/login",
        json={"username": "tester", "password": "password123"},
    )
    project = client.post("/api/projects", json={"name": "失败重试"}).json()
    project_id = project["id"]
    client.app.state.store.update(
        "7",
        project_id,
        remote_batch_id="h3-batch-1",
        snapshot={
            "batch_id": "h3-batch-1",
            "status": "FAILED",
            "items": [
                {
                    "row_id": "ROW-001",
                    "segments": [
                        {
                            "segment_id": "segment-1",
                            "status": "FAILED",
                            "can_retry": True,
                        }
                    ],
                }
            ],
        },
    )

    preview = client.post(
        f"/api/projects/{project_id}/segments/segment-1/retry/prepare"
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["estimated_paid_calls"] == 1
    assert cloud.retry_confirms == 0

    confirmed = client.post(
        f"/api/projects/{project_id}/segments/segment-1/retry/confirm",
        json={
            "request_key": "retry-request-1",
            "quote_token": "retry-quote-1",
            "cost_confirmed": True,
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert cloud.retry_confirms == 1
    assert confirmed.json()["snapshot"]["items"][0]["segments"][0]["status"] == (
        "PENDING"
    )


def test_local_cancel_proxy_requires_explicit_idempotency_key(tmp_path: Path) -> None:
    client, cloud, _, _ = _client(tmp_path)
    client.post(
        "/api/session/login",
        json={"username": "tester", "password": "password123"},
    )
    project = client.post("/api/projects", json={"name": "取消任务"}).json()
    project_id = project["id"]
    client.app.state.store.update(
        "7",
        project_id,
        remote_batch_id="h3-batch-1",
        snapshot={
            "batch_id": "h3-batch-1",
            "status": "ACTIVE",
            "items": [
                {
                    "row_id": "ROW-001",
                    "segments": [
                        {
                            "segment_id": "segment-1",
                            "status": "RUNNING",
                            "can_cancel": True,
                        }
                    ],
                }
            ],
        },
    )

    rejected = client.post(
        f"/api/projects/{project_id}/segments/segment-1/cancel",
        json={},
    )
    assert rejected.status_code == 422
    assert cloud.cancels == 0

    cancelled = client.post(
        f"/api/projects/{project_id}/segments/segment-1/cancel",
        json={"request_key": "cancel-request-1"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cloud.cancels == 1
    assert cancelled.json()["snapshot"]["items"][0]["segments"][0]["status"] == (
        "CANCELLED"
    )

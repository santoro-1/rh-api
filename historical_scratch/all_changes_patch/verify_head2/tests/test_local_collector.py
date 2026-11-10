from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.local_collector import (  # noqa: E402
    DEFAULT_RENDER_SERVER_URL,
    LocalCollectorService,
    LocalCollectorSettings,
)
from jyd_probe.local_collector_api import create_local_collector_app  # noqa: E402


class LocalCollectorServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_root = PROJECT_ROOT / "runtime" / "test_tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = temp_root / f"local_collector_{uuid.uuid4().hex}"
        self.temp.mkdir()
        self.draft_root = self.temp / "drafts"
        self.draft_root.mkdir()
        self.workspace = self.temp / "workspace"
        self.workspace.mkdir()
        self.state_root = self.temp / "state"
        self.settings = LocalCollectorSettings(
            draft_root=self.draft_root,
            state_root=self.state_root,
            workspace_root=self.workspace,
            decrypt_work_root=self.state_root / "decrypted",
            font_library_root=self.workspace / "font_library",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_default_server_is_the_local_standalone_processor(self) -> None:
        self.assertEqual(DEFAULT_RENDER_SERVER_URL, "http://127.0.0.1:8010")
        self.assertEqual(self.settings.render_server_url, DEFAULT_RENDER_SERVER_URL)

    def test_double_click_does_not_open_obsolete_local_workbench_url(self) -> None:
        source = (
            PROJECT_ROOT / "apps" / "collector" / "run_local_collector.py"
        ).read_text(encoding="utf-8")
        self.assertIn("if args.open_browser and not args.no_browser:", source)
        self.assertIn('collector_url = f"http://{args.host}:{args.port}/"', source)
        self.assertNotIn("settings.render_server_url.rstrip('/')}/app", source)

    def test_lists_plain_and_encrypted_drafts_and_analyzes_plain_draft(self) -> None:
        source_video = self.temp / "source.mp4"
        source_video.write_bytes(b"video")
        plain_dir = self.draft_root / "成片草稿"
        plain_dir.mkdir()
        draft = {
            "duration": 8_000_000,
            "canvas_config": {"width": 1080, "height": 1920, "ratio": "9:16"},
            "tracks": [
                {
                    "id": "video-track",
                    "type": "video",
                    "segments": [
                        {
                            "id": "video-segment",
                            "material_id": "video-material",
                            "target_timerange": {"start": 0, "duration": 8_000_000},
                            "source_timerange": {"start": 0, "duration": 8_000_000},
                        }
                    ],
                }
            ],
            "materials": {
                "videos": [
                    {
                        "id": "video-material",
                        "path": str(source_video),
                        "material_name": "主视频",
                    }
                ]
            },
        }
        (plain_dir / "draft_content.json").write_text(
            json.dumps(draft, ensure_ascii=False),
            encoding="utf-8",
        )

        encrypted_dir = self.draft_root / "高版本草稿"
        encrypted_dir.mkdir()
        (encrypted_dir / "draft_content.json").write_bytes(b"not-plain-json")
        ignored_dir = self.draft_root / "普通文件夹"
        ignored_dir.mkdir()

        service = LocalCollectorService(self.settings)
        listed = service.list_drafts()
        by_name = {item["name"]: item for item in listed}

        self.assertEqual(set(by_name), {"成片草稿", "高版本草稿"})
        self.assertEqual(by_name["成片草稿"]["encryption_status"], "plain")
        self.assertEqual(by_name["成片草稿"]["duration_us"], 8_000_000)
        self.assertEqual(by_name["高版本草稿"]["encryption_status"], "encrypted")

        report = service.analyze_draft(plain_dir, hash_limit_bytes=-1)
        self.assertTrue(report["report_id"])
        self.assertEqual(report["draft"]["name"], "成片草稿")
        self.assertEqual(report["draft"]["main_video"]["material_id"], "video-material")
        self.assertEqual(report["summary"]["upload_required_count"], 1)
        self.assertEqual(service.get_report(report["report_id"])["report_id"], report["report_id"])
        plan = service.create_upload_plan(
            report["report_id"],
            {
                "audio": "replace",
                "video_effects": "replace",
                "text_style": "replace",
                "text_effects": "keep",
                "text_templates": "keep",
            },
        )
        self.assertTrue(plan["plan_id"])
        self.assertEqual(plan["summary"]["upload_count"], 1)
        self.assertTrue(plan["summary"]["ready_for_upload"])
        self.assertEqual(service.get_upload_plan(plan["plan_id"])["plan_id"], plan["plan_id"])

        template_plan = service.create_upload_plan(
            report["report_id"],
            {
                "audio": "replace",
                "video_effects": "keep",
                "text_style": "keep",
                "text_effects": "keep",
                "text_templates": "keep",
            },
            mode="template_center",
        )
        self.assertEqual(template_plan["mode"], "template_center")
        self.assertEqual(template_plan["summary"]["upload_count"], 0)
        with patch.object(service, "_post_package", return_value={"template": {"name": "账号模板"}}) as posted:
            uploaded = service.upload_plan(
                template_plan["plan_id"],
                template_name="账号模板",
                server_url="http://192.168.1.20:8010",
                template_import_ticket="one-time-ticket",
            )
        self.assertEqual(uploaded["server_result"]["template"]["name"], "账号模板")
        self.assertEqual(posted.call_args.kwargs["template_import_ticket"], "one-time-ticket")

    def test_persists_root_and_rejects_draft_outside_configured_root(self) -> None:
        alternative_root = self.temp / "alternative"
        alternative_root.mkdir()
        service = LocalCollectorService(self.settings)
        config = service.set_draft_root(alternative_root)
        self.assertEqual(Path(config["draft_root"]), alternative_root.resolve())

        restored_settings = LocalCollectorSettings(
            draft_root=self.draft_root,
            state_root=self.state_root,
            workspace_root=self.workspace,
            decrypt_work_root=self.state_root / "decrypted",
            font_library_root=self.workspace / "font_library",
        )
        restored = LocalCollectorService(restored_settings)
        self.assertEqual(restored.settings.draft_root, alternative_root.resolve())

        outside = self.temp / "outside"
        outside.mkdir()
        (outside / "draft_content.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "只能分析"):
            restored.analyze_draft(outside)

    def test_unified_website_origin_can_call_local_collector(self) -> None:
        self.settings.render_server_url = "https://video.example.internal"
        app = create_local_collector_app(self.settings)
        with TestClient(app) as client:
            response = client.options(
                "/api/drafts",
                headers={
                    "Origin": "https://video.example.internal",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Private-Network": "true",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "https://video.example.internal",
        )
        self.assertEqual(
            response.headers.get("access-control-allow-private-network"),
            "true",
        )

    def test_lan_website_origin_can_pair_without_prior_configuration(self) -> None:
        app = create_local_collector_app(self.settings)
        with TestClient(app) as client:
            response = client.options(
                "/api/config",
                headers={
                    "Origin": "http://192.168.1.20:8000",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Private-Network": "true",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "http://192.168.1.20:8000",
        )

    def test_default_collector_access_token_is_ready_for_internal_site(self) -> None:
        self.assertEqual(self.settings.access_token, "operator123")

    def test_native_pickers_return_paths_without_copying_files(self) -> None:
        source = self.temp / "本机视频.mp4"
        source.write_bytes(b"video-data")
        output = self.temp / "exports"
        output.mkdir()
        service = LocalCollectorService(self.settings)

        with patch.object(service, "_ask_open_filename", return_value=str(source)):
            selected = service.select_media_file("video")
        with patch.object(service, "_ask_directory", return_value=str(output)):
            folder = service.select_output_folder()

        self.assertEqual(Path(selected["path"]), source.resolve())
        self.assertEqual(selected["size"], len(b"video-data"))
        self.assertEqual(Path(folder["path"]), output.resolve())
        self.assertEqual(list(output.iterdir()), [])

    def test_personal_library_root_can_be_synced_from_standalone_processor(self) -> None:
        shared_personal = self.temp / "processor-data" / "personal_libraries"
        service = LocalCollectorService(self.settings)
        configured = service.set_personal_library_root(shared_personal)
        self.assertEqual(Path(configured["personal_library_root"]), shared_personal.resolve())

        restored = LocalCollectorService(self.settings)
        self.assertEqual(restored.settings.personal_library_root, shared_personal.resolve())

    def test_personal_library_starts_separate_and_collects_empty_draft_safely(self) -> None:
        draft_dir = self.draft_root / "素材采集"
        draft_dir.mkdir()
        (draft_dir / "draft_content.json").write_text(
            json.dumps({"tracks": [], "materials": {}}, ensure_ascii=False),
            encoding="utf-8",
        )
        service = LocalCollectorService(self.settings)

        result = service.collect_personal_assets(draft_dir)

        self.assertTrue(result["ok"])
        personal_root = Path(result["personal_library_root"])
        self.assertEqual(personal_root, (self.state_root / "personal_libraries").resolve())
        self.assertEqual(set(result["results"]), {
            "audio", "effects", "fonts", "stickers", "text_effects", "text_templates"
        })
        self.assertTrue(all("ok" in item for item in result["results"].values()))


if __name__ == "__main__":
    unittest.main()

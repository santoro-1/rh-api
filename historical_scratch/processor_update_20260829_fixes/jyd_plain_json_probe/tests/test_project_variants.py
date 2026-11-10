from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.project_store import ProjectStore  # noqa: E402
from jyd_probe.project_variants import (  # noqa: E402
    ProjectVariantCoordinator,
    select_maximum_difference,
    signature_distance,
)


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.outputs: dict[str, Path] = {}
        self.fail_ids: set[str] = set()

    def submit_batch(self, jobs, variants):
        start = len(self.jobs)
        self.jobs.extend(jobs)
        ids = []
        for offset, job in enumerate(jobs):
            job_id = f"job-{start + offset}"
            output = Path(job["output"]["mp4_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(job_id.encode("ascii"))
            self.outputs[job_id] = output
            ids.append(job_id)
        return {"batch_id": f"batch-{start}", "job_ids": ids}

    def get_status(self, job_id):
        if job_id in self.fail_ids:
            return {"job_id": job_id, "status": "failed", "error": "fake failure"}
        return {
            "job_id": job_id,
            "status": "completed",
            "result": {"output_mp4": str(self.outputs[job_id])},
        }


class ProjectVariantTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (
            PROJECT_ROOT / "runtime" / "test_tmp" / f"variants_{uuid.uuid4().hex}"
        )
        self.root.mkdir(parents=True)
        self.store = ProjectStore(self.root / "control.db")
        self.queue = FakeQueue()
        self.effects = [self._asset("effect", index) for index in range(3)]
        self.stickers = [self._asset("sticker", index) for index in range(3)]
        self.corners = [self._asset("corner", index) for index in range(4)]
        font = self.root / "font.ttf"
        font.write_bytes(b"font")
        self.coordinator = ProjectVariantCoordinator(
            self.store,
            self.queue,
            storage_root=self.root,
            draft_root=self.root / "drafts",
            fonts=[
                {
                    "identity": "font-1",
                    "resource_id": "font-rid",
                    "name": "固定字体",
                    "path": str(font),
                },
                {
                    "identity": "resource_id:6807742980271641102",
                    "resource_id": "6807742980271641102",
                    "name": "SourceHanSerifCN-Heavy",
                    "path": str(font),
                },
            ],
            bgm_assets=[{"identity": "bgm-1", "path": str(self.root / "bgm.mp3")}],
            effects=self.effects,
            fullscreen_stickers=self.stickers,
            corner_stickers=self.corners,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _asset(self, kind: str, index: int) -> dict:
        path = self.root / f"{kind}-{index}.json"
        path.write_text("{}", encoding="utf-8")
        return {
            "identity": f"{kind}-{index}",
            "name": f"{kind} {index}",
            "path": str(path),
            "enabled": True,
        }

    def _project(self, item_count: int = 1):
        project = self.store.create_project(
            owner_user_id="user",
            owner_username="tester",
            name="模块 6",
            items=[
                {"row_key": str(index), "script_text": f"固定字幕{index}"}
                for index in range(1, item_count + 1)
            ],
        )
        script_source = self.root / "原始脚本.xlsx"
        script_source.write_bytes(b"original-xlsx")
        self.store.add_script_source(
            "user",
            project["project_id"],
            filename=script_source.name,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=script_source.stat().st_size,
            sha256="test-sha256",
            managed_path=str(script_source),
        )
        for item_index, item in enumerate(project["items"], start=1):
            image = self.root / f"image-{item_index}.png"
            image.write_bytes(f"image-{item_index}".encode("ascii"))
            registered_image = self.store.register_input_image(
                owner_user_id="user",
                project_id=project["project_id"],
                filename=image.name,
                content_type="image/png",
                size_bytes=image.stat().st_size,
                sha256=f"image-{item_index}",
                managed_path=str(image),
            )
            self.store.replace_item_image(
                "user", project["project_id"], item["item_id"], registered_image["image_id"]
            )
            audio = self.root / f"audio-{item_index}.mp3"
            audio.write_bytes(f"audio-{item_index}".encode("ascii"))
            self.store.add_asset(
                owner_user_id="user",
                project_id=project["project_id"],
                item_id=item["item_id"],
                asset_type="audio",
                source_type="minimax",
                status="READY",
                filename=audio.name,
                managed_path=str(audio),
                make_current=True,
            )
            base = self.root / f"base-{item_index}.mp4"
            base.write_bytes(f"base-{item_index}".encode("ascii"))
            self.store.add_asset(
                owner_user_id="user",
                project_id=project["project_id"],
                item_id=item["item_id"],
                asset_type="base_video",
                source_type="runninghub_merge",
                status="READY",
                filename=base.name,
                managed_path=str(base),
                metadata={"segment_count": 2},
                make_current=True,
            )
            for segment_index in (1, 2):
                segment = self.root / f"segment-{item_index}-{segment_index}.mp4"
                segment.write_bytes(f"segment-{item_index}-{segment_index}".encode("ascii"))
                self.store.add_asset(
                    owner_user_id="user",
                    project_id=project["project_id"],
                    item_id=item["item_id"],
                    asset_type="original_video_segment",
                    source_type="runninghub",
                    status="READY",
                    filename=segment.name,
                    managed_path=str(segment),
                    external_ref={"video_index": segment_index},
                    metadata={
                        "start_seconds": segment_index - 1,
                        "end_seconds": segment_index,
                        "actual_duration_us": 1_000_000,
                    },
                )
            self.store.configure_item_postprocess(
                "user", project["project_id"], item["item_id"],
                font_identity="font-1", bgm_identity="bgm-1", text_color="#FFFFFF",
                top_title={
                    "label": "\u51cf\u80a5\u5927\u5b9e\u8bdd",
                    "headline": "\u53ea\u6709\u575a\u6301\u624d\u80fd\u8fbe\u6210\u76ee\u6807",
                },
                cover_title={"line_1": "健康真相", "line_2": "别再踩坑"},
            )
            self.store.set_item_subtitles(
                "user", project["project_id"], item["item_id"],
                {
                    "source": "minimax_timestamps", "raw_cues": [],
                    "render_cues": [{"start_us": 0, "end_us": 1_000_000, "text": item["script_text"]}],
                    "style": {"font_id": "font-1", "font_size": 15, "text_color": "#FFFFFF", "transform_y": -0.6},
                    "status": "PREVIEW_READY", "overflow_risk": False,
                },
            )
            self.store.create_operation(
                owner_user_id="user", project_id=project["project_id"], item_id=item["item_id"],
                operation_type="POSTPROCESS_GENERATE", idempotency_key=f"preview-{item_index}", payload={},
            )
            self.store.transition_operation(
                "user", project["project_id"], item["item_id"], operation_type="POSTPROCESS_GENERATE",
                status="SUCCEEDED", item_status="COMPOSITION_READY", result={"preview_mode": "browser"},
            )
        return self.store.get_project("user", project["project_id"])

    def test_initial_generation_can_target_one_project_row(self) -> None:
        project = self._project(item_count=2)
        target = project["items"][1]
        generated = self.coordinator.start(
            "user",
            project["project_id"],
            idempotency_key="variants-single-row",
            settings=None,
            items=[{
                "item_id": target["item_id"],
                "count": 1,
                "cover": {"enabled": True, "frame_time_seconds": 0},
            }],
        )
        by_id = {item["item_id"]: item for item in generated["items"]}
        self.assertEqual(by_id[project["items"][0]["item_id"]]["outputs"]["variants"], [])
        self.assertEqual(len(by_id[target["item_id"]]["outputs"]["variants"]), 1)

    def test_maximum_difference_selection_is_unique_and_spread(self) -> None:
        candidates = list(
            (ratio, color, effect, sticker, corner)
            for ratio in ("1:1", "3:4")
            for color in ("black", "white", "blue", "pink")
            for effect in ("e0", "e1", "e2")
            for sticker in ("s0", "s1", "s2")
            for corner in ("c0", "c1", "c2", "c3")
        )
        selected = select_maximum_difference(candidates, 30, seed="stable")
        self.assertEqual(len(selected), 30)
        self.assertEqual(len(set(selected)), 30)
        self.assertGreaterEqual(
            min(
                signature_distance(left, right)
                for index, left in enumerate(selected)
                for right in selected[index + 1 :]
            ),
            5,
        )
        self.assertEqual(
            selected, select_maximum_difference(candidates, 30, seed="stable")
        )

    def test_module_6_inherits_project_cover_and_real_assets(self) -> None:
        project = self._project()
        item = project["items"][0]
        generated = self.coordinator.start(
            "user",
            project["project_id"],
            idempotency_key="variants-1",
            settings=None,
            items=[
                {
                    "item_id": item["item_id"],
                    "count": 30,
                }
            ],
        )
        row = generated["items"][0]
        self.assertEqual(row["status"], "VARIANT_READY")
        self.assertEqual(generated["settings"]["variants"]["mode"], "recommended")
        self.assertEqual(row["settings"]["variants"]["count"], 30)
        self.assertNotIn("cover", row["settings"]["variants"])
        self.assertIsNotNone(row["outputs"]["base_video"])
        self.assertEqual(len(row["outputs"]["variants"]), 30)
        self.assertEqual(
            len(
                {
                    tuple(asset["metadata"]["signature"])
                    for asset in row["outputs"]["variants"]
                }
            ),
            30,
        )
        self.assertEqual(len(self.queue.jobs), 30)
        for job in self.queue.jobs:
            self.assertEqual(job["source"]["type"], "video_sequence")
            self.assertEqual(
                [entry["video_index"] for entry in job["source"]["items"]],
                [1, 2],
            )
            self.assertEqual(
                [entry["volume"] for entry in job["source"]["items"]],
                [0.0, 0.0],
            )
            self.assertEqual(job["original_video_volume"], 0.0)
            self.assertEqual(job["captions"]["size"], 11.0)
            self.assertAlmostEqual(job["captions"]["clip_scale"], 1.32)
            self.assertEqual(job["captions"]["stroke_color"], "")
            self.assertEqual(job["captions"]["font_title"], "固定字体")
            self.assertEqual(
                job["audios"][0]["media_path"],
                str((self.root / "audio-1.mp3").resolve()),
            )
            self.assertEqual(job["audios"][1]["library_identity"], "bgm-1")
            self.assertTrue(job["audios"][1]["align_to_end"])
            self.assertEqual(job["audios"][1]["crossfade_us"], 200_000)
            self.assertEqual(job["audios"][1]["fade_in_us"], 5_000_000)
            self.assertEqual(job["source"]["fade_out_us"], 0)
            self.assertEqual(
                [text["text"] for text in job["texts"]],
                [
                    "世界冠军带你自律",
                    "非医疗保健科普：仅供参考，个人经验分享，不代表普遍性\n如有不适请线下就医",
                    "张雒",
                    "世界蹦床冠军",
                    "专注35+女性身材管理",
                ],
            )
            self.assertEqual(job["fixed_overlays"][0]["rotation"], -90.0)
            self.assertEqual(job["fixed_overlays"][0]["renderer"], "sticker")
            self.assertAlmostEqual(job["fixed_overlays"][0]["scale"], 0.8941348042237189)
            self.assertEqual(job["cover"]["frame_count"], 3)
            self.assertEqual(job["cover"]["frame_source"], "input_image")
            self.assertAlmostEqual(job["cover"]["text_scale"], 1.1045453049181124)
            self.assertFalse(job["cover"]["auto_wrapping"])
            self.assertEqual(job["cover"]["image_path"], str((self.root / "image-1.png").resolve()))
            self.assertEqual(job["cover"]["text_line_1"], "健康真相")
            self.assertEqual(job["cover"]["text_line_2"], "别再踩坑")
            self.assertEqual(job["cover"]["font"]["font_id"], "6807742980271641102")
            self.assertEqual(job["cover"]["line_1_size"], 30.0)
            self.assertEqual(job["cover"]["line_2_size"], 22.0)
            self.assertAlmostEqual(job["cover"]["line_1_y"], -160 / 1920)
            self.assertAlmostEqual(job["cover"]["line_2_y"], -655 / 1920)
            self.assertEqual(job["cover"]["overlay_y_ratio"], 0.615)
            self.assertEqual(job["cover"]["overlay_height_ratio"], 0.28)
            self.assertEqual(len(job["effects"]), 1)
            self.assertGreaterEqual(len(job["stickers"]), 1)
        operation = next(
            value
            for value in generated["operations"]
            if value["operation_type"] == "VARIANT_GENERATE"
        )
        self.assertNotIn("use_subtitles", operation["payload"]["settings"])
        self.assertEqual(
            operation["payload"]["settings"]["use_fullscreen_stickers"], True
        )
        archive = operation["payload"]["archive"]
        archive_path = Path(archive["export_path"])
        self.assertEqual(archive_path.parent.name, archive["date_label"])
        self.assertEqual(archive_path.name, "1")
        self.assertTrue((archive_path / "原始脚本.xlsx").is_file())
        self.assertTrue(all(Path(job["output"]["mp4_path"]).parent == archive_path for job in self.queue.jobs))
        self.assertEqual(
            [job["output"]["draft_name"] for job in self.queue.jobs[:3]],
            ["任务-1-变体-001", "任务-1-变体-002", "任务-1-变体-003"],
        )
        gallery = self.coordinator.result_library.list_results("user")
        self.assertEqual(gallery["total_batches"], 1)
        self.assertEqual(gallery["total_videos"], 30)

        repeated = self.coordinator.start(
            "user",
            project["project_id"],
            idempotency_key="variants-1",
            settings=None,
            items=[
                {"item_id": item["item_id"], "count": 30, "cover": {"enabled": True}}
            ],
        )
        self.assertEqual(len(self.queue.jobs), 30)
        self.assertEqual(len(repeated["items"][0]["outputs"]["variants"]), 30)

        first_asset = row["outputs"]["variants"][0]
        removed_path = Path(first_asset["managed_path"])
        self.store.delete_variant_asset(
            "user", project["project_id"], item["item_id"], first_asset["asset_id"]
        )
        remaining = self.store.get_project("user", project["project_id"])["items"][0][
            "outputs"
        ]["variants"]
        self.assertEqual(len(remaining), 29)
        self.assertTrue(removed_path.is_file())  # the API layer owns safe file deletion

    def test_partial_failure_retries_only_failed_signature_and_supplement_is_new(
        self,
    ) -> None:
        project = self._project()
        item = project["items"][0]
        self.queue.fail_ids.add("job-1")
        failed = self.coordinator.start(
            "user",
            project["project_id"],
            idempotency_key="partial-1",
            settings=None,
            items=[
                {
                    "item_id": item["item_id"],
                    "count": 5,
                    "cover": {"enabled": True, "frame_time_seconds": 0},
                }
            ],
        )
        self.assertEqual(failed["items"][0]["status"], "VARIANT_FAILED")
        self.assertEqual(len(failed["items"][0]["outputs"]["variants"]), 4)
        failed_signature = next(
            job["signature"]
            for operation in failed["operations"]
            if operation["operation_type"] == "VARIANT_GENERATE"
            for job in operation["result"]["jobs"]
            if job["status"] == "failed"
        )

        self.queue.fail_ids.clear()
        retried = self.coordinator.retry(
            "user", project["project_id"], item["item_id"], idempotency_key="retry-1"
        )
        signatures = [
            asset["metadata"]["signature"]
            for asset in retried["items"][0]["outputs"]["variants"]
        ]
        self.assertEqual(retried["items"][0]["status"], "VARIANT_READY")
        self.assertEqual(len(signatures), 5)
        self.assertIn(failed_signature, signatures)

        supplemented = self.coordinator.supplement(
            "user",
            project["project_id"],
            item["item_id"],
            idempotency_key="supplement-1",
            count=3,
        )
        all_signatures = [
            tuple(asset["metadata"]["signature"])
            for asset in supplemented["items"][0]["outputs"]["variants"]
        ]
        self.assertEqual(len(all_signatures), 8)
        self.assertEqual(len(set(all_signatures)), 8)

    def test_uploaded_video_is_used_directly_without_reapplying_4b_recipe(self) -> None:
        project = self.store.create_project(
            owner_user_id="user",
            owner_username="tester",
            name="上传视频变体",
            items=[{"row_key": "1", "script_text": "已经人工处理"}],
        )
        item = project["items"][0]
        uploaded = self.root / "uploaded.mp4"
        uploaded.write_bytes(b"uploaded")
        self.store.add_asset(
            owner_user_id="user",
            project_id=project["project_id"],
            item_id=item["item_id"],
            asset_type="composition_video",
            source_type="user_upload",
            status="READY",
            filename="uploaded.mp4",
            managed_path=str(uploaded),
            make_current=True,
        )
        generated = self.coordinator.start(
            "user",
            project["project_id"],
            idempotency_key="upload-variants",
            settings=None,
            items=[
                {
                    "item_id": item["item_id"],
                    "count": 2,
                    "cover": {"enabled": True, "frame_time_seconds": 0},
                }
            ],
        )
        self.assertEqual(generated["items"][0]["status"], "VARIANT_READY")
        for job in self.queue.jobs:
            self.assertEqual(job["source"]["media_path"], str(uploaded.resolve()))
            self.assertNotIn("captions", job)
            self.assertNotIn("audios", job)


if __name__ == "__main__":
    unittest.main()

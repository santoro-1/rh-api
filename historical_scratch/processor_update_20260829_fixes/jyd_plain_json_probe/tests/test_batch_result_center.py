from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import shutil
import sys
import threading
import unittest
import uuid
import zipfile
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.web_api import (  # noqa: E402
    RenderJobQueue,
    StorageLifecycleManager,
    WebApiSettings,
    _batch_dir,
    _estimate_batch_timing,
    _job_dir,
    _media_meta_path,
    _prepare_render_job_payload,
    _read_json,
    _write_json,
)
from jyd_probe.audio_catalog import AudioCatalog  # noqa: E402
from jyd_probe.template_library import TemplateLibrary  # noqa: E402


class BatchResultCenterTest(unittest.TestCase):
    def setUp(self) -> None:
        test_root = PROJECT_ROOT / "runtime" / "test_tmp"
        test_root.mkdir(parents=True, exist_ok=True)
        self.root = test_root / f"batch_result_center_{uuid.uuid4().hex}"
        self.root.mkdir()
        self.settings = WebApiSettings(
            storage_root=self.root / "storage",
            template_library_root=self.root / "templates",
            default_draft_root=self.root / "drafts",
            audio_library_root=self.root / "audio",
        )
        self.queue = RenderJobQueue.__new__(RenderJobQueue)
        self.queue.settings = self.settings
        self.queue._pending = []
        self.queue._lock = threading.Lock()

    def tearDown(self) -> None:
        if self.root.exists() and self.root.parent == PROJECT_ROOT / "runtime" / "test_tmp":
            shutil.rmtree(self.root)

    def test_estimates_remaining_time_from_finished_jobs(self) -> None:
        timing = _estimate_batch_timing(
            [
                {
                    "status": "completed",
                    "started_at": "2026-07-14T10:00:00",
                    "finished_at": "2026-07-14T10:00:10",
                },
                {
                    "status": "failed",
                    "started_at": "2026-07-14T10:01:00",
                    "finished_at": "2026-07-14T10:01:20",
                },
                {"status": "pending"},
                {"status": "pending"},
            ]
        )
        self.assertEqual(timing["average_job_seconds"], 15)
        self.assertEqual(timing["estimated_remaining_seconds"], 30)

    def test_standalone_job_references_source_and_writes_to_selected_folder(self) -> None:
        source = self.root / "source.mp4"
        source.write_bytes(b"source-video")
        output_dir = self.root / "selected-output"
        output_dir.mkdir()
        settings = WebApiSettings(
            storage_root=self.root / "local-storage",
            template_library_root=self.root / "local-templates",
            default_draft_root=self.root / "local-drafts",
            audio_library_root=self.root / "local-audio",
            allow_local_file_access=True,
            execution_mode="embedded",
        )
        media_id = "local_test_video"
        _write_json(
            _media_meta_path(settings, media_id),
            {
                "media_id": media_id,
                "kind": "video",
                "path": str(source),
                "storage_mode": "local_reference",
            },
        )

        job = _prepare_render_job_payload(
            settings,
            AudioCatalog(settings.audio_library_root),
            {
                "source": {"type": "video", "media_id": media_id},
                "output": {"draft_name": "本机结果", "output_dir": str(output_dir)},
            },
            "job-local",
        )

        self.assertEqual(Path(job["source"]["media_path"]), source.resolve())
        self.assertEqual(Path(job["output"]["mp4_path"]), output_dir / "本机结果.mp4")
        self.assertTrue(job["output"]["external_output"])
        self.assertFalse((settings.storage_root / "media" / "video").exists())

    def test_run_job_records_success_with_current_result_fields(self) -> None:
        job_id = "successful-job"
        job_dir = _job_dir(self.settings, job_id)
        job_dir.mkdir(parents=True)
        mp4 = self.settings.storage_root / "outputs" / f"{job_id}.mp4"
        draft = self.settings.default_draft_root / "successful-draft"
        _write_json(job_dir / "job.json", {"source": {"type": "template"}, "output": {}})
        _write_json(job_dir / "status.json", {"job_id": job_id, "status": "pending"})
        result_dict = {
            "exported": True,
            "output_mp4": str(mp4),
            "output_draft_dir": str(draft),
        }
        result = SimpleNamespace(
            exported=True,
            output_mp4=mp4,
            output_draft_dir=draft,
            as_dict=lambda: result_dict,
        )

        with patch("jyd_probe.web_api.run_render_job", return_value=result):
            self.queue._run_job(job_id)

        status = _read_json(job_dir / "status.json")
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["result"], result_dict)
        self.assertIn("draft_expires_at", status)

    def test_get_status_recovers_exported_mp4_from_old_false_failure(self) -> None:
        job_id = "recover-export"
        job_dir = _job_dir(self.settings, job_id)
        job_dir.mkdir(parents=True)
        mp4 = self.settings.storage_root / "outputs" / f"{job_id}.mp4"
        mp4.parent.mkdir(parents=True)
        mp4.write_bytes(b"exported-video")
        _write_json(
            job_dir / "job.json",
            {
                "source": {"type": "template"},
                "output": {
                    "mp4_path": str(mp4),
                    "draft_root": str(self.settings.default_draft_root),
                    "draft_name": "recovered-draft",
                },
            },
        )
        _write_json(
            job_dir / "status.json",
            {
                "job_id": job_id,
                "status": "failed",
                "error": "'RenderJobResult' object has no attribute 'output_mp4_path'",
            },
        )

        status = self.queue.get_status(job_id)

        self.assertEqual(status["status"], "completed")
        self.assertTrue(status["result"]["exported"])
        self.assertEqual(status["result"]["output_mp4"], str(mp4.resolve()))

    def test_cancels_only_pending_jobs(self) -> None:
        batch_id = "batch-cancel"
        self._write_batch(batch_id, [("pending-job", {}), ("running-job", {})])
        self._write_status("pending-job", "pending", batch_id)
        self._write_status("running-job", "running", batch_id)
        self.queue._pending = ["pending-job"]

        result = self.queue.cancel_batch(batch_id)

        self.assertEqual(result["cancelled_now"], 1)
        self.assertEqual(result["counts"]["cancelled"], 1)
        self.assertEqual(_read_json(_job_dir(self.settings, "pending-job") / "status.json")["status"], "cancelled")
        self.assertEqual(_read_json(_job_dir(self.settings, "running-job") / "status.json")["status"], "running")

    def test_lists_recent_batches_with_result_counts(self) -> None:
        self._write_batch("older-batch", [("older-job", {})])
        self._write_status(
            "older-job",
            "completed",
            "older-batch",
            result={"exported": True, "output_mp4": "older.mp4"},
        )
        older = _read_json(_batch_dir(self.settings, "older-batch") / "batch.json")
        older["created_at"] = "2026-07-14T10:00:00"
        _write_json(_batch_dir(self.settings, "older-batch") / "batch.json", older)

        self._write_batch("newer-batch", [("newer-job", {})])
        self._write_status("newer-job", "failed", "newer-batch")
        newer = _read_json(_batch_dir(self.settings, "newer-batch") / "batch.json")
        newer["created_at"] = "2026-07-14T11:00:00"
        _write_json(_batch_dir(self.settings, "newer-batch") / "batch.json", newer)

        batches = self.queue.list_recent_batches()

        self.assertEqual([item["batch_id"] for item in batches], ["newer-batch", "older-batch"])
        self.assertEqual(batches[0]["counts"]["failed"], 1)
        self.assertEqual(batches[1]["available_outputs"], 1)

    def test_creates_zip_with_short_unique_names(self) -> None:
        batch_id = "batch-download"
        jobs = [("job-a", {"display_name": "起风+浪漫"}), ("job-b", {"display_name": "起风+浪漫"})]
        self._write_batch(batch_id, jobs)
        output_root = self.settings.storage_root / "outputs"
        output_root.mkdir(parents=True)
        for job_id, variant in jobs:
            mp4 = output_root / f"{job_id}.mp4"
            mp4.write_bytes(f"fake-{job_id}".encode("ascii"))
            self._write_status(
                job_id,
                "completed",
                batch_id,
                variant=variant,
                result={"exported": True, "output_mp4": str(mp4)},
            )

        result = self.queue.create_batch_download(batch_id, ["job-a", "job-b"])
        archive_path = self.settings.storage_root / "batch_downloads" / f"{result['download_id']}.zip"
        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(archive.namelist(), ["起风+浪漫.mp4", "起风+浪漫-02.mp4"])
        self.assertEqual(result["count"], 2)

    def test_deletes_only_managed_outputs(self) -> None:
        batch_id = "batch-delete"
        job_id = "job-delete"
        self._write_batch(batch_id, [(job_id, {})])
        mp4 = self.settings.storage_root / "outputs" / f"{job_id}.mp4"
        draft = self.settings.default_draft_root / "generated-draft"
        mp4.parent.mkdir(parents=True)
        draft.mkdir(parents=True)
        mp4.write_bytes(b"fake-mp4")
        (draft / "draft_content.json").write_text("{}", encoding="utf-8")
        self._write_status(
            job_id,
            "completed",
            batch_id,
            result={"exported": True, "output_mp4": str(mp4), "output_draft_dir": str(draft)},
        )

        result = self.queue.delete_batch_outputs(batch_id, [job_id])

        self.assertEqual(result["deleted"], [job_id])
        self.assertFalse(mp4.exists())
        self.assertFalse(draft.exists())
        self.assertTrue(_read_json(_job_dir(self.settings, job_id) / "status.json")["output_deleted"])

    def test_admin_deletes_finished_batch_record_and_all_managed_artifacts(self) -> None:
        batch_id = "test-batch-delete-record"
        job_id = "test-job-delete-record"
        self._write_batch(batch_id, [(job_id, {"display_name": "测试任务"})])
        mp4 = self.settings.storage_root / "outputs" / f"{job_id}.mp4"
        draft = self.settings.default_draft_root / "test-generated-draft"
        mp4.parent.mkdir(parents=True)
        draft.mkdir(parents=True)
        mp4.write_bytes(b"test-output")
        (draft / "draft_content.json").write_text("{}", encoding="utf-8")
        self._write_status(
            job_id,
            "completed",
            batch_id,
            result={
                "exported": True,
                "output_mp4": str(mp4),
                "output_draft_dir": str(draft),
            },
        )
        _write_json(
            _job_dir(self.settings, job_id) / "job.json",
            {"source": {"type": "template"}, "output": {}},
        )

        result = self.queue.delete_batch_record(batch_id)

        self.assertEqual(result["deleted_jobs"], 1)
        self.assertFalse(mp4.exists())
        self.assertFalse(draft.exists())
        self.assertFalse(_job_dir(self.settings, job_id).exists())
        self.assertFalse(_batch_dir(self.settings, batch_id).exists())

    def test_cannot_delete_batch_while_a_job_is_active(self) -> None:
        batch_id = "active-batch-delete-record"
        job_id = "active-job-delete-record"
        self._write_batch(batch_id, [(job_id, {})])
        self._write_status(job_id, "running", batch_id)

        with self.assertRaisesRegex(Exception, "仍有 1 个"):
            self.queue.delete_batch_record(batch_id)

        self.assertTrue(_job_dir(self.settings, job_id).exists())
        self.assertTrue(_batch_dir(self.settings, batch_id).exists())

    def test_retry_failed_clears_old_output_names(self) -> None:
        batch_id = "batch-retry"
        job_id = "failed-job"
        variant = {"display_name": "起风+浪漫"}
        self._write_batch(batch_id, [(job_id, variant)])
        self._write_status(job_id, "failed", batch_id, variant=variant)
        _write_json(
            _job_dir(self.settings, job_id) / "job.json",
            {
                "source": {"type": "video", "media_path": "input.mp4"},
                "batch": {"batch_id": batch_id},
                "output_mp4": "old.mp4",
                "output": {
                    "mp4_path": "old.mp4",
                    "draft_name": "old-draft",
                    "draft_root": str(self.settings.default_draft_root),
                },
            },
        )
        captured: dict[str, object] = {}

        def fake_submit(payloads, variants):
            captured["payloads"] = payloads
            captured["variants"] = variants
            return {"batch_id": "new-batch", "total": len(payloads)}

        self.queue.submit_batch = fake_submit
        result = self.queue.retry_failed_batch(batch_id)

        retried = captured["payloads"][0]
        self.assertNotIn("batch", retried)
        self.assertNotIn("output_mp4", retried)
        self.assertNotIn("mp4_path", retried["output"])
        self.assertNotIn("draft_name", retried["output"])
        self.assertEqual(captured["variants"], [variant])
        self.assertEqual(result["retried_from_batch_id"], batch_id)

    def test_first_cleanup_gives_legacy_outputs_a_full_grace_period(self) -> None:
        job_id = "legacy-job"
        mp4 = self.settings.storage_root / "outputs" / f"{job_id}.mp4"
        mp4.parent.mkdir(parents=True)
        mp4.write_bytes(b"legacy-output")
        self._write_status(
            job_id,
            "completed",
            "legacy-batch",
            result={"exported": True, "output_mp4": str(mp4)},
        )
        manager = StorageLifecycleManager(self.settings)

        report = manager.cleanup(now=datetime.fromisoformat("2026-07-14T12:00:00"))

        status = _read_json(_job_dir(self.settings, job_id) / "status.json")
        self.assertEqual(report["initialized_job_expirations"], 1)
        self.assertEqual(status["expires_at"], "2026-07-17T12:00:00")
        self.assertTrue(mp4.exists())

    def test_expired_cleanup_removes_temporary_files_but_preserves_libraries(self) -> None:
        job_id = "expired-job"
        mp4 = self.settings.storage_root / "outputs" / f"{job_id}.mp4"
        generated = self.settings.storage_root / "generated_video_drafts" / "source-draft"
        output_draft = self.settings.default_draft_root / "output-draft"
        permanent = self.settings.template_library_root / "keep.json"
        for directory in (mp4.parent, generated, output_draft, permanent.parent):
            directory.mkdir(parents=True, exist_ok=True)
        mp4.write_bytes(b"expired-output")
        (generated / "draft_content.json").write_text("{}", encoding="utf-8")
        (output_draft / "draft_content.json").write_text("{}", encoding="utf-8")
        permanent.write_text("{}", encoding="utf-8")
        self._write_status(
            job_id,
            "completed",
            "expired-batch",
            result={
                "exported": True,
                "output_mp4": str(mp4),
                "output_draft_dir": str(output_draft),
                "source_draft_dir": str(generated),
                "working_template_dir": str(generated),
            },
        )
        status_path = _job_dir(self.settings, job_id) / "status.json"
        status = _read_json(status_path)
        status["expires_at"] = "2026-07-14T10:00:00"
        status["draft_expires_at"] = "2026-07-14T10:00:00"
        _write_json(status_path, status)
        manager = StorageLifecycleManager(self.settings)

        report = manager.cleanup(now=datetime.fromisoformat("2026-07-14T12:00:00"))

        self.assertEqual(report["expired_jobs"], 1)
        self.assertFalse(mp4.exists())
        self.assertFalse(generated.exists())
        self.assertFalse(output_draft.exists())
        self.assertTrue(permanent.exists())
        self.assertEqual(report["expired_drafts"], 1)
        self.assertEqual(_read_json(status_path)["output_delete_reason"], "retention_expired")

    def test_failed_job_draft_is_removed_after_48_hours_without_result(self) -> None:
        job_id = "failed-draft-cleanup"
        draft_name = "known-failed-draft"
        output_draft = self.settings.default_draft_root / draft_name
        output_draft.mkdir(parents=True)
        (output_draft / "draft_content.json").write_text("{}", encoding="utf-8")
        self._write_status(job_id, "failed", "failed-draft-batch")
        job_dir = _job_dir(self.settings, job_id)
        _write_json(
            job_dir / "job.json",
            {
                "source": {"type": "template"},
                "output": {
                    "draft_root": str(self.settings.default_draft_root),
                    "draft_name": draft_name,
                },
            },
        )
        status_path = job_dir / "status.json"
        status = _read_json(status_path)
        status["expires_at"] = "2026-07-15T12:00:00"
        status["draft_expires_at"] = "2026-07-14T10:00:00"
        _write_json(status_path, status)
        manager = StorageLifecycleManager(self.settings)

        report = manager.cleanup(now=datetime.fromisoformat("2026-07-14T12:00:00"))

        self.assertEqual(report["expired_drafts"], 1)
        self.assertFalse(output_draft.exists())
        refreshed = _read_json(status_path)
        self.assertTrue(refreshed["draft_deleted"])
        self.assertFalse(refreshed.get("output_deleted", False))

    def test_expired_media_is_kept_while_referenced_by_pending_job(self) -> None:
        media_id = "active-media"
        media_path = self.settings.storage_root / "media" / "video" / "active.mp4"
        media_path.parent.mkdir(parents=True)
        media_path.write_bytes(b"active-media")
        _write_json(
            self.settings.storage_root / "media" / "records" / f"{media_id}.json",
            {
                "media_id": media_id,
                "path": str(media_path),
                "expires_at": "2026-07-14T10:00:00",
            },
        )
        self._write_status("pending-media-job", "pending", "active-batch")
        _write_json(
            _job_dir(self.settings, "pending-media-job") / "job.json",
            {"source": {"type": "video", "media_id": media_id, "media_path": str(media_path)}},
        )
        manager = StorageLifecycleManager(self.settings)

        report = manager.cleanup(now=datetime.fromisoformat("2026-07-14T12:00:00"))

        self.assertEqual(report["deleted_media"], 0)
        self.assertTrue(media_path.exists())

    def test_legacy_uploaded_template_gets_full_48_hour_grace_period(self) -> None:
        record = self._create_uploaded_template("legacy-template", expires_at="")
        manager = StorageLifecycleManager(self.settings)

        report = manager.cleanup(now=datetime.fromisoformat("2026-07-14T12:00:00"))

        refreshed = TemplateLibrary(self.settings.template_library_root).get(record.template_id)
        self.assertEqual(report["initialized_template_expirations"], 1)
        self.assertEqual(refreshed.expires_at, "2026-07-16T12:00:00")
        self.assertTrue(record.root_dir.exists())

    def test_expired_uploaded_template_and_import_record_are_removed(self) -> None:
        record = self._create_uploaded_template(
            "expired-template",
            expires_at="2026-07-14T10:00:00",
            import_id="expiredimport",
        )
        import_record = self.settings.storage_root / "draft_imports" / "records" / "expiredimport"
        import_record.mkdir(parents=True)
        (import_record / "import_result.json").write_text("{}", encoding="utf-8")
        manager = StorageLifecycleManager(self.settings)

        report = manager.cleanup(now=datetime.fromisoformat("2026-07-14T12:00:00"))

        self.assertEqual(report["deleted_templates"], 1)
        self.assertFalse(record.root_dir.exists())
        self.assertFalse(import_record.exists())

    def test_expired_uploaded_template_is_kept_for_active_job(self) -> None:
        record = self._create_uploaded_template(
            "active-template",
            expires_at="2026-07-14T10:00:00",
        )
        self._write_status("active-template-job", "running", "active-template-batch")
        _write_json(
            _job_dir(self.settings, "active-template-job") / "job.json",
            {"source": {"type": "template", "template_id": record.template_id}},
        )
        manager = StorageLifecycleManager(self.settings)

        report = manager.cleanup(now=datetime.fromisoformat("2026-07-14T12:00:00"))

        self.assertEqual(report["deleted_templates"], 0)
        self.assertTrue(record.root_dir.exists())

    def test_excel_batch_once_template_is_retained_for_24_hours_after_success(self) -> None:
        batch_id = "excel-once-success"
        job_id = "excel-once-job"
        incoming = self.settings.storage_root / "draft_imports" / "incoming" / "once.zip"
        incoming.parent.mkdir(parents=True)
        incoming.write_bytes(b"temporary-package")
        record = self._create_uploaded_template(
            "excel-once-template",
            expires_at="2026-07-15T12:00:00",
            import_id="excelonceimport",
            lifecycle="excel_batch_once",
            batch_id=batch_id,
            incoming_package_path=str(incoming),
        )
        import_record = self.settings.storage_root / "draft_imports" / "records" / "excelonceimport"
        import_record.mkdir(parents=True)
        (import_record / "import_result.json").write_text("{}", encoding="utf-8")
        self._write_batch(batch_id, [(job_id, {})], temporary_template_ids=[record.template_id])
        self._write_status(job_id, "completed", batch_id, result={"exported": True})

        finished_at = datetime.fromisoformat("2026-07-18T12:00:00")
        retained = self.queue._cleanup_batch_once_templates(batch_id, now=finished_at)

        self.assertEqual(retained["status"], "retained_for_24_hours")
        self.assertEqual(retained["retained_until"], "2026-07-19T12:00:00")
        self.assertTrue(record.root_dir.exists())
        self.assertTrue(import_record.exists())
        self.assertTrue(incoming.exists())

        cleanup = self.queue._cleanup_batch_once_templates(
            batch_id, now=datetime.fromisoformat("2026-07-19T12:00:01")
        )

        self.assertEqual(cleanup["status"], "deleted")
        self.assertEqual(cleanup["deleted_templates"], 1)
        self.assertFalse(record.root_dir.exists())
        self.assertFalse(import_record.exists())
        self.assertFalse(incoming.exists())
        batch = _read_json(_batch_dir(self.settings, batch_id) / "batch.json")
        self.assertEqual(batch["temporary_templates_deleted"], [record.template_id])

    def test_excel_batch_once_template_is_kept_when_job_failed(self) -> None:
        batch_id = "excel-once-failed"
        job_id = "excel-once-failed-job"
        record = self._create_uploaded_template(
            "excel-once-failed-template",
            expires_at="2026-07-15T12:00:00",
            lifecycle="excel_batch_once",
            batch_id=batch_id,
        )
        self._write_batch(batch_id, [(job_id, {})], temporary_template_ids=[record.template_id])
        self._write_status(job_id, "failed", batch_id)

        cleanup = self.queue._cleanup_batch_once_templates(batch_id)

        self.assertEqual(cleanup["status"], "kept_for_failed_retry")
        self.assertTrue(record.root_dir.exists())

    def test_batch_metadata_gets_grace_period_then_is_removed(self) -> None:
        batch_id = "metadata-batch"
        job_id = "metadata-job"
        self._write_batch(batch_id, [(job_id, {})])
        self._write_status(job_id, "completed", batch_id, result={"exported": False})
        manager = StorageLifecycleManager(self.settings)
        now = datetime.fromisoformat("2026-07-14T12:00:00")

        first = manager.cleanup(now=now)

        batch_path = _batch_dir(self.settings, batch_id) / "batch.json"
        batch = _read_json(batch_path)
        self.assertEqual(first["initialized_metadata_expirations"], 1)
        self.assertEqual(batch["metadata_expires_at"], "2026-08-13T12:00:00")
        self.assertTrue(_job_dir(self.settings, job_id).exists())

        batch["metadata_expires_at"] = "2026-07-14T11:00:00"
        _write_json(batch_path, batch)
        second = manager.cleanup(now=now)

        self.assertEqual(second["deleted_batch_metadata"], 1)
        self.assertEqual(second["deleted_job_metadata"], 1)
        self.assertFalse(_batch_dir(self.settings, batch_id).exists())
        self.assertFalse(_job_dir(self.settings, job_id).exists())

    def _create_uploaded_template(
        self,
        template_id: str,
        *,
        expires_at: str,
        import_id: str = "",
        lifecycle: str = "",
        batch_id: str = "",
        incoming_package_path: str = "",
    ):
        source = self.root / f"source-{template_id}"
        source.mkdir()
        (source / "draft_content.json").write_text(
            json.dumps({"duration": 1_000_000, "tracks": [], "materials": {}}),
            encoding="utf-8",
        )
        return TemplateLibrary(self.settings.template_library_root).import_template(
            source,
            template_id=template_id,
            auto_decrypt=False,
            import_info={
                "source": "local_collector",
                "import_id": import_id,
                "lifecycle": lifecycle,
                "batch_id": batch_id,
                "incoming_package_path": incoming_package_path,
            },
            expires_at=expires_at,
        )

    def _write_batch(
        self,
        batch_id: str,
        jobs: list[tuple[str, dict]],
        *,
        temporary_template_ids: list[str] | None = None,
    ) -> None:
        _write_json(
            _batch_dir(self.settings, batch_id) / "batch.json",
            {
                "batch_id": batch_id,
                "status": "pending",
                "created_at": "2026-07-14T10:00:00",
                "total": len(jobs),
                "temporary_template_ids": temporary_template_ids or [],
                "jobs": [
                    {"job_id": job_id, "index": index, "variant": variant}
                    for index, (job_id, variant) in enumerate(jobs, start=1)
                ],
            },
        )

    def _write_status(
        self,
        job_id: str,
        status: str,
        batch_id: str,
        *,
        variant: dict | None = None,
        result: dict | None = None,
    ) -> None:
        data = {
            "job_id": job_id,
            "batch_id": batch_id,
            "status": status,
            "created_at": "2026-07-14T10:00:00",
            "variant": variant or {},
        }
        if result is not None:
            data["result"] = result
        _write_json(_job_dir(self.settings, job_id) / "status.json", data)


if __name__ == "__main__":
    unittest.main()

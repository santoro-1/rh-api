from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.task_store import SQLiteTaskStore  # noqa: E402


class SQLiteTaskStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp"
        root.mkdir(parents=True, exist_ok=True)
        self.root = root / f"task_store_{uuid.uuid4().hex}"
        self.root.mkdir()
        self.store = SQLiteTaskStore(self.root / "control.db")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_two_agents_claim_different_jobs_atomically(self) -> None:
        self.store.register_agent("agent-1", {"name": "一号机"})
        self.store.register_agent("agent-2", {"name": "二号机"})
        for job_id in ("job-1", "job-2"):
            self.store.add_job(
                job_id,
                {"schema": "jyd.render_job.v1", "job": job_id},
                {"job_id": job_id, "status": "pending", "created_at": "2026-07-15T10:00:00"},
            )

        first = self.store.claim_job("agent-1")
        second = self.store.claim_job("agent-2")

        self.assertEqual(first["job_id"], "job-1")
        self.assertEqual(second["job_id"], "job-2")
        self.assertNotEqual(first["job_id"], second["job_id"])

    def test_finish_job_releases_agent(self) -> None:
        self.store.register_agent("agent-1", {"name": "一号机"})
        self.store.add_job("job-1", {"output": {}}, {"job_id": "job-1", "status": "pending"})
        self.store.claim_job("agent-1")

        status = self.store.finish_job(
            "agent-1", "job-1", result={"exported": True, "output_mp4": "result.mp4"}
        )

        self.assertEqual(status["status"], "completed")
        self.assertEqual(self.store.get_agent("agent-1")["status"], "idle")
        self.assertIsNone(self.store.get_agent("agent-1")["current_job_id"])

    def test_cancel_batch_cancels_pending_and_marks_running(self) -> None:
        self.store.add_batch({"batch_id": "batch-1", "created_at": "2026-07-15T10:00:00"})
        self.store.register_agent("agent-1", {"name": "一号机"})
        for job_id in ("job-1", "job-2"):
            self.store.add_job(
                job_id,
                {},
                {"job_id": job_id, "batch_id": "batch-1", "status": "pending"},
            )
        self.store.claim_job("agent-1")

        cancelled = self.store.cancel_batch("batch-1")

        self.assertEqual(cancelled, ["job-2"])
        self.assertTrue(self.store.get_status("job-1")["cancel_requested"])
        self.assertEqual(self.store.get_status("job-2")["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()

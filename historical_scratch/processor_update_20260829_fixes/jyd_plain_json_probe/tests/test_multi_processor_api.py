from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest
import uuid

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.web_api import WebApiSettings, create_app  # noqa: E402


class MultiProcessorApiTest(unittest.TestCase):
    def setUp(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp"
        root.mkdir(parents=True, exist_ok=True)
        self.root = root / f"multi_processor_api_{uuid.uuid4().hex}"
        self.root.mkdir()
        settings = WebApiSettings(
            storage_root=self.root / "storage",
            template_library_root=self.root / "templates",
            default_draft_root=self.root / "drafts",
            audio_library_root=self.root / "audio",
            admin_password="internal-password",
            admin_session_secret="test-session-secret",
            site_password="operator-password",
            site_session_secret="test-site-session-secret",
            execution_mode="agent",
            agent_token="test-agent-token",
            database_path=self.root / "control.db",
            max_video_upload_bytes=4,
            auth_authority=True,
        )
        for directory in (
            settings.storage_root,
            settings.template_library_root,
            settings.default_draft_root,
            settings.audio_library_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.app = create_app(settings)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def login(self) -> None:
        response = self.client.post(
            "/api/admin/login",
            json={"username": "admin", "password": "internal-password", "next": "/app"},
        )
        self.assertEqual(response.status_code, 200)

    def login_operator(self) -> None:
        response = self.client.post(
            "/api/auth/login",
            json={"username": "operator", "password": "operator-password", "next": "/app"},
        )
        self.assertEqual(response.status_code, 200)

    @property
    def agent_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-agent-token"}

    def test_login_is_required_and_agent_completes_a_job(self) -> None:
        empty_health = self.client.get("/api/health")
        self.assertEqual(empty_health.status_code, 200)
        self.assertEqual(empty_health.json()["workspace_status"], "idle")
        self.assertEqual(empty_health.json()["active_jobs"], 0)
        unauthenticated = self.client.post(
            "/api/render", json={"source": {"type": "video"}, "output": {"skip_export": True}}
        )
        self.assertEqual(unauthenticated.status_code, 401)
        self.login()

        submitted = self.client.post(
            "/api/render",
            json={"source": {"type": "video"}, "output": {"skip_export": True}},
        )
        self.assertEqual(submitted.status_code, 200)
        job_id = submitted.json()["job_id"]
        busy_health = self.client.get("/api/health").json()
        self.assertEqual(busy_health["workspace_status"], "busy")
        self.assertEqual(busy_health["pending_jobs"], 1)
        self.assertEqual(busy_health["active_jobs"], 1)

        registered = self.client.post(
            "/api/agents/register",
            headers=self.agent_headers,
            json={"agent_id": "processor-01", "name": "一号机"},
        )
        self.assertEqual(registered.status_code, 200)
        claimed = self.client.post(
            "/api/agents/processor-01/claim", headers=self.agent_headers, json={}
        )
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual(claimed.json()["job"]["job_id"], job_id)

        completed = self.client.post(
            f"/api/agents/processor-01/jobs/{job_id}/complete",
            headers=self.agent_headers,
            json={"result": {"exported": False, "output_draft_dir": "draft"}},
        )
        self.assertEqual(completed.status_code, 200)
        status = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(status.json()["status"], "completed")
        self.assertEqual(status.json()["assigned_agent_id"], "processor-01")

    def test_upload_limit_stops_stream(self) -> None:
        self.login()
        response = self.client.post(
            "/api/media/video?filename=large.mp4",
            content=b"12345",
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 413)

    def test_lan_workspace_can_read_public_health_for_load_balancing(self) -> None:
        response = self.client.options(
            "/api/health",
            headers={
                "Origin": "http://192.168.1.55:8000",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "http://192.168.1.55:8000",
        )

    def test_operator_cannot_open_admin_api(self) -> None:
        self.login_operator()
        self.assertEqual(self.client.get("/api/storage").status_code, 401)
        self.assertEqual(
            self.client.delete("/api/admin/batches/missing-batch").status_code,
            401,
        )
        self.assertEqual(self.client.get("/api/templates").status_code, 200)

    def test_workspace_logout_clears_admin_session_too(self) -> None:
        self.login()
        self.assertTrue(self.client.get("/api/auth/session").json()["authenticated"])

        response = self.client.post("/api/auth/logout")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.get("/api/auth/session").json()["authenticated"])
        self.assertEqual(
            self.client.get("/login", follow_redirects=False).status_code,
            200,
        )

    def test_admin_manages_test_users_and_disabling_revokes_session(self) -> None:
        self.login()
        created = self.client.post(
            "/api/admin/users",
            json={"username": "tester", "display_name": "测试员工", "password": "tester123"},
        )
        self.assertEqual(created.status_code, 200)
        user_id = created.json()["user_id"]
        listed = self.client.get("/api/admin/users")
        self.assertTrue(any(item["username"] == "tester" for item in listed.json()["users"]))

        with TestClient(self.app) as user_client:
            logged_in = user_client.post(
                "/api/auth/login",
                json={"username": "tester", "password": "tester123", "next": "/app"},
            )
            self.assertEqual(logged_in.status_code, 200)
            self.assertEqual(user_client.get("/api/templates").status_code, 200)

            disabled = self.client.patch(
                f"/api/admin/users/{user_id}", json={"enabled": False}
            )
            self.assertEqual(disabled.status_code, 200)
            self.assertEqual(user_client.get("/api/templates").status_code, 401)
            rejected = user_client.post(
                "/api/auth/login",
                json={"username": "tester", "password": "tester123", "next": "/app"},
            )
            self.assertEqual(rejected.status_code, 401)

    def test_authority_issues_and_revokes_center_tokens(self) -> None:
        self.login()
        created = self.client.post(
            "/api/admin/users",
            json={"username": "centeruser", "password": "center123"},
        ).json()
        logged_in = self.client.post(
            "/api/auth/center/login",
            json={"username": "centeruser", "password": "center123"},
        )
        self.assertEqual(logged_in.status_code, 200)
        token = logged_in.json()["access_token"]
        verified = self.client.post(
            "/api/auth/center/verify", json={"access_token": token}
        )
        self.assertEqual(verified.status_code, 200)

        self.client.patch(
            f"/api/admin/users/{created['user_id']}", json={"enabled": False}
        )
        revoked = self.client.post(
            "/api/auth/center/verify", json={"access_token": token}
        )
        self.assertEqual(revoked.status_code, 401)

    def test_browser_handoff_is_one_time_and_sets_center_cookie(self) -> None:
        self.login()
        self.client.post(
            "/api/admin/users",
            json={"username": "handoffuser", "password": "handoff123"},
        )
        token = self.client.post(
            "/api/auth/center/login",
            json={"username": "handoffuser", "password": "handoff123"},
        ).json()["access_token"]
        code = self.client.post(
            "/api/auth/center/handoff", json={"access_token": token}
        ).json()["handoff_code"]

        with TestClient(self.app) as browser_client:
            accepted = browser_client.get(
                f"/api/auth/handoff?code={code}&next=/app", follow_redirects=False
            )
            self.assertEqual(accepted.status_code, 303)
            self.assertEqual(accepted.headers["location"], "/app")
            self.assertTrue(browser_client.get("/api/auth/session").json()["authenticated"])

            replayed = browser_client.get(
                f"/api/auth/handoff?code={code}&next=/app", follow_redirects=False
            )
            self.assertEqual(replayed.status_code, 303)
            self.assertEqual(replayed.headers["location"], "/login?next=/app")


if __name__ == "__main__":
    unittest.main()

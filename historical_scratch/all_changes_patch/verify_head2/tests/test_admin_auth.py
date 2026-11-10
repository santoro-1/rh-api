from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.admin_auth import AdminAuth
from jyd_probe.web_api import _is_admin_protected_path, _is_site_protected_path, _safe_admin_next


class AdminAuthTest(unittest.TestCase):
    def test_authenticates_and_verifies_signed_session(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp" / f"admin_auth_{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        try:
            auth = AdminAuth(
                root,
                username="operator",
                password="correct-password",
                session_secret="test-session-secret",
            )
            self.assertTrue(auth.authenticate("operator", "correct-password"))
            self.assertFalse(auth.authenticate("operator", "wrong-password"))
            token = auth.issue_token()
            self.assertTrue(auth.verify_token(token))
            self.assertFalse(auth.verify_token(token + "changed"))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_generates_persistent_initial_credentials(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp" / f"admin_auth_{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        try:
            first = AdminAuth(root)
            second = AdminAuth(root)
            self.assertTrue(first.generated_password)
            self.assertFalse(second.generated_password)
            self.assertEqual(first.password, second.password)
            self.assertTrue((root / "admin_password.txt").is_file())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_configured_password_is_written_to_the_password_file(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp" / f"admin_auth_{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        try:
            (root / "admin_password.txt").write_text("old-password\n", encoding="utf-8")
            auth = AdminAuth(root, password="admin123")
            self.assertTrue(auth.authenticate("admin", "admin123"))
            self.assertEqual(
                (root / "admin_password.txt").read_text(encoding="utf-8").strip(),
                "admin123",
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_protects_admin_pages_and_mutating_admin_apis(self) -> None:
        self.assertTrue(_is_admin_protected_path("/app/assets"))
        self.assertTrue(_is_admin_protected_path("/app/advanced"))
        self.assertTrue(_is_admin_protected_path("/api/admin/assets"))
        self.assertTrue(_is_admin_protected_path("/api/storage"))
        self.assertTrue(_is_admin_protected_path("/api/batches/demo/delete-outputs"))
        self.assertFalse(_is_admin_protected_path("/app"))
        self.assertTrue(_is_site_protected_path("/app"))
        self.assertTrue(_is_site_protected_path("/api/render"))
        self.assertFalse(_is_site_protected_path("/api/agents/processor-01/claim"))
        self.assertFalse(_is_admin_protected_path("/api/admin/login"))
        self.assertEqual(_safe_admin_next("/app/advanced"), "/app/advanced")
        self.assertEqual(_safe_admin_next("https://example.com"), "/app/assets")


if __name__ == "__main__":
    unittest.main()

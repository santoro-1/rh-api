from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.user_auth import UserAuth  # noqa: E402


class UserAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp"
        root.mkdir(parents=True, exist_ok=True)
        self.root = root / f"user_auth_{uuid.uuid4().hex}"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_creates_initial_user_and_never_stores_plain_password(self) -> None:
        auth = UserAuth(
            self.root,
            initial_username="operator",
            initial_password="secret123",
            session_secret="test-secret",
        )
        self.assertTrue(auth.initial_user_created)
        self.assertIsNotNone(auth.authenticate("operator", "secret123"))
        self.assertIsNone(auth.authenticate("operator", "wrong-password"))
        self.assertNotIn("secret123", (self.root / "users.json").read_text(encoding="utf-8"))

    def test_disable_and_password_reset_invalidate_existing_sessions(self) -> None:
        auth = UserAuth(self.root, initial_password="operator123", session_secret="test-secret")
        created = auth.create_user("tester", "first-pass", display_name="测试员")
        authenticated = auth.authenticate("tester", "first-pass")
        token = auth.issue_token(authenticated)
        self.assertIsNotNone(auth.verify_token(token))

        auth.update_user(created["user_id"], enabled=False)
        self.assertIsNone(auth.verify_token(token))
        self.assertIsNone(auth.authenticate("tester", "first-pass"))

        enabled = auth.update_user(created["user_id"], enabled=True, password="second-pass")
        self.assertTrue(enabled["enabled"])
        self.assertIsNone(auth.authenticate("tester", "first-pass"))
        self.assertIsNotNone(auth.authenticate("tester", "second-pass"))

    def test_rejects_duplicate_accounts_and_short_passwords(self) -> None:
        auth = UserAuth(self.root, session_secret="test-secret")
        auth.create_user("tester", "123456")
        with self.assertRaisesRegex(ValueError, "已存在"):
            auth.create_user("TESTER", "abcdef")
        with self.assertRaisesRegex(ValueError, "至少"):
            auth.create_user("short", "123")

    def test_fresh_install_can_require_admin_to_create_first_user(self) -> None:
        auth = UserAuth(self.root, session_secret="test-secret", create_initial=False)
        self.assertEqual(auth.list_users(), [])
        created = auth.create_user("internal", "internal123")
        self.assertEqual(created["username"], "internal")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.draft_crypto import detect_jianying_install_dir, ensure_jy_draftc_env


class DraftCryptoEnvironmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = PROJECT_ROOT / "runtime" / "test_tmp" / f"draft_crypto_env_{uuid.uuid4().hex}"
        self.temp.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_detects_explicit_jianying_install_directory(self) -> None:
        install_dir = self.temp / "JianyingPro"
        install_dir.mkdir()
        (install_dir / "videoeditor.dll").write_bytes(b"test")
        with patch.dict(os.environ, {"JY_INSTALL_DIR": str(install_dir)}, clear=False):
            self.assertEqual(detect_jianying_install_dir(), install_dir.resolve())

    def test_creates_draftc_env_from_detected_installation(self) -> None:
        tool_dir = self.temp / "tool"
        install_dir = self.temp / "JianyingPro"
        tool_dir.mkdir()
        install_dir.mkdir()
        exe = tool_dir / "jy-draftc.exe"
        exe.write_bytes(b"test")
        (install_dir / "videoeditor.dll").write_bytes(b"test")
        with patch.dict(os.environ, {"JY_INSTALL_DIR": str(install_dir)}, clear=False):
            ensure_jy_draftc_env(exe, None)
        self.assertIn(str(install_dir.resolve()), (tool_dir / ".env").read_text("utf-8"))

    def test_detects_custom_installation_on_fixed_drive(self) -> None:
        drive_root = self.temp / "drive_d"
        install_dir = drive_root / "软件" / "JianyingPro" / "11.0.0.14274"
        install_dir.mkdir(parents=True)
        (install_dir / "videoeditor.dll").write_bytes(b"test")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "jyd_probe.draft_crypto._windows_fixed_drive_roots",
                return_value=[drive_root],
            ),
        ):
            self.assertEqual(detect_jianying_install_dir(), install_dir.resolve())

    def test_ignores_update_staging_dll_suffix(self) -> None:
        drive_root = self.temp / "drive_d"
        install_dir = drive_root / "软件" / "JianyingPro" / "11.0.0.14274"
        install_dir.mkdir(parents=True)
        (install_dir / "videoeditor.dll_d_1234567").write_bytes(b"test")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "jyd_probe.draft_crypto._windows_fixed_drive_roots",
                return_value=[drive_root],
            ),
        ):
            self.assertIsNone(detect_jianying_install_dir())


if __name__ == "__main__":
    unittest.main()

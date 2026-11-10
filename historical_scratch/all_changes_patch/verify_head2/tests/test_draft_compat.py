from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.draft_compat import normalize_draft_for_legacy_editor  # noqa: E402


class DraftCompatibilityTest(unittest.TestCase):
    def test_converts_native_newer_draft_envelope_to_legacy_target(self) -> None:
        data = {
            "version": 360000,
            "new_version": "112.0.0",
            "platform": {"app_version": "6.0.1", "os": "windows"},
            "last_modified_platform": {"app_version": "6.0.1", "os": "windows"},
            "materials": {},
        }

        result = normalize_draft_for_legacy_editor(data)

        self.assertTrue(result.changed)
        self.assertEqual(result.changed_contexts, 1)
        self.assertEqual(data["platform"]["app_version"], "5.9.0")
        self.assertEqual(data["last_modified_platform"]["app_version"], "5.9.0")
        self.assertEqual(data["new_version"], "110.0.0")
        self.assertEqual(data["version"], 360000)

    def test_preserves_known_good_draft_created_by_legacy_editor(self) -> None:
        data = {
            "version": 360000,
            "new_version": "142.0.0",
            "platform": {"app_version": "5.9.0"},
            "last_modified_platform": {"app_version": "8.9.0"},
            "materials": {},
        }

        result = normalize_draft_for_legacy_editor(data)

        self.assertFalse(result.changed)
        self.assertEqual(data["platform"]["app_version"], "5.9.0")
        self.assertEqual(data["last_modified_platform"]["app_version"], "8.9.0")
        self.assertEqual(data["new_version"], "142.0.0")

    def test_converts_embedded_native_newer_drafts(self) -> None:
        nested = {
            "version": 370000,
            "new_version": "142.0.0",
            "platform": {"app_version": "8.9.0"},
            "last_modified_platform": {"app_version": "8.9.0"},
            "materials": {},
        }
        data = {
            "version": 360000,
            "new_version": "110.0.0",
            "platform": {"app_version": "5.9.0"},
            "last_modified_platform": {"app_version": "5.9.0"},
            "materials": {"drafts": [{"draft": nested}]},
        }

        result = normalize_draft_for_legacy_editor(data)

        self.assertEqual(result.changed_contexts, 1)
        self.assertEqual(nested["platform"]["app_version"], "5.9.0")
        self.assertEqual(nested["last_modified_platform"]["app_version"], "5.9.0")
        self.assertEqual(nested["new_version"], "110.0.0")
        self.assertEqual(nested["version"], 360000)


if __name__ == "__main__":
    unittest.main()

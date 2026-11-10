from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class UpdatePackageContractTest(unittest.TestCase):
    def test_update_build_excludes_all_data(self) -> None:
        script = (
            PROJECT_ROOT / "scripts" / "build" / "build_processor.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("Remove-Item -LiteralPath $ResolvedDataDir -Recurse -Force", script)
        self.assertIn("UpdateOnly package must not contain a data directory", script)
        self.assertNotIn("$SemanticVisualSource", script)
        self.assertNotIn("$MusicProfileSource", script)
        self.assertNotIn("$LayoutFontSource", script)

    def test_update_instructions_state_code_only_contract(self) -> None:
        instructions = (PROJECT_ROOT / "docs" / "PROCESSOR_UPDATE.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("包内不会出现 `data` 目录", instructions)
        self.assertIn("只包含重新构建的 Processor 程序", instructions)
        self.assertIn("独立素材包", instructions)


if __name__ == "__main__":
    unittest.main()

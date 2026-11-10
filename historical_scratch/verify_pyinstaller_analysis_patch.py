from __future__ import annotations

import sys
from types import CodeType
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader


def code_constants(code: CodeType):
    for value in code.co_consts:
        yield value
        if isinstance(value, CodeType):
            yield from code_constants(value)


def main() -> None:
    executable_path = Path(sys.argv[1]).resolve()
    html_path = Path(sys.argv[2]).resolve()
    executable = CArchiveReader(str(executable_path))
    archive = executable.open_embedded_archive("PYZ.pyz")
    code = archive.extract("jyd_probe.auth_center")
    if code is None:
        raise SystemExit("jyd_probe.auth_center is missing from PYZ")
    if "WORKBENCH_ANALYSIS_TIMEOUT_SECONDS" not in code.co_names:
        raise SystemExit("analysis timeout constant is missing from packaged module")
    if 900.0 not in code.co_consts:
        raise SystemExit("packaged analysis timeout is not 900 seconds")

    postprocess = archive.extract("jyd_probe.project_postprocess")
    if postprocess is None:
        raise SystemExit("jyd_probe.project_postprocess is missing from PYZ")
    if "ASR_RAW_CUE_PREVIEW_FALLBACK_CODES" not in postprocess.co_names:
        raise SystemExit("ASR low-match preview fallback is missing from packaged module")
    constants = set(
        value for value in code_constants(postprocess) if isinstance(value, str)
    )
    required_asr_constants = {
        "ASR_SCRIPT_MATCH_TOO_LOW",
        "ASR_RAW_CUE_MATCH_TOO_LOW",
        "；已改用 MiniMax 原始时间轴生成预览字幕",
    }
    if not required_asr_constants.issubset(constants):
        raise SystemExit("packaged ASR low-match preview fallback is incomplete")

    html = html_path.read_text(encoding="utf-8")
    required = (
        "CONTENT_ANALYSIS_STATUS_POLL_INTERVAL_MS = 5000",
        "scheduleContentAnalysisStatusPoll(projectId, itemIds)",
        "projectHasPendingContentAnalysis(project, itemIds)",
    )
    missing = [value for value in required if value not in html]
    if missing:
        raise SystemExit(f"packaged status watchdog is incomplete: {missing}")
    print("PACKAGED_AI_ANALYSIS_AND_ASR_PATCH=PASS")


if __name__ == "__main__":
    main()

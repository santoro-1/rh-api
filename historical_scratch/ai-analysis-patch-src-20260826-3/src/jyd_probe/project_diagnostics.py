from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import uuid
import zipfile
from typing import Any

from .logging_config import redact_text


DIAGNOSTIC_SCHEMA = "jyd.project-diagnostics.v1"
LOG_FILENAMES = ("workbench.log", "render.log", "collector.log")
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_LOG_LINES = 5000
_SENSITIVE_CONTENT_FIELD = re.compile(
    r'(?i)"(?:script_text|original_script|prompt|payload|result)"\s*:'
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r'(?i)\b[A-Z]:[\\/][^"\r\n,}]*')
_UNC_ABSOLUTE_PATH = re.compile(r'\\\\[^"\r\n,}]+')


def build_project_diagnostic_archive(
    project: dict[str, Any],
    *,
    logs_root: Path,
    output_root: Path,
) -> Path:
    """Create a redacted, project-scoped archive for support diagnostics."""

    output_root.mkdir(parents=True, exist_ok=True)
    archive_path = output_root / f"project-diagnostics-{uuid.uuid4().hex}.zip"
    summary = _safe_project_summary(project)
    matched_logs = _matching_project_logs(project, logs_root)
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr(
            "项目诊断摘要.json",
            json.dumps(summary, ensure_ascii=False, indent=2),
        )
        archive.writestr(
            "项目相关日志.txt",
            matched_logs or "未在本机保留期内找到与当前项目直接关联的日志。\n",
        )
        archive.writestr(
            "说明.txt",
            "此诊断包只包含当前项目的结构化摘要和可关联日志，"
            "不包含脚本文本、素材文件、素材路径、操作负载或登录凭据。\n"
            "独立 Agent 运行在其他电脑时，其 agent.log 保存在该电脑的 logs 文件夹，"
            "不会被本机诊断包自动收集。\n",
        )
    return archive_path


def _safe_project_summary(project: dict[str, Any]) -> dict[str, Any]:
    items = project.get("items") if isinstance(project.get("items"), list) else []
    operations = (
        project.get("operations")
        if isinstance(project.get("operations"), list)
        else []
    )
    links = project.get("links") if isinstance(project.get("links"), list) else []
    return {
        "schema": DIAGNOSTIC_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": {
            "project_id": project.get("project_id"),
            "project_no": project.get("project_no"),
            "name": project.get("name"),
            "status": project.get("status"),
            "revision": project.get("revision"),
            "created_at": project.get("created_at"),
            "updated_at": project.get("updated_at"),
        },
        "items": [
            {
                "item_id": item.get("item_id"),
                "row_key": item.get("row_key"),
                "status": item.get("status"),
                "outputs_available": {
                    key: bool((item.get("outputs") or {}).get(key))
                    for key in (
                        "audio",
                        "base_video",
                        "composition_video",
                        "original_video_segments",
                        "variants",
                    )
                },
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
            for item in items
            if isinstance(item, dict)
        ],
        "operations": [
            {
                key: operation.get(key)
                for key in (
                    "operation_id",
                    "correlation_id",
                    "item_id",
                    "operation_type",
                    "status",
                    "attempt_count",
                    "error_code",
                    "created_at",
                    "updated_at",
                    "started_at",
                    "finished_at",
                )
            }
            for operation in operations
            if isinstance(operation, dict)
        ],
        "links": [
            {
                key: link.get(key)
                for key in (
                    "link_id",
                    "item_id",
                    "system",
                    "relation",
                    "external_id",
                    "created_at",
                )
            }
            for link in links
            if isinstance(link, dict)
        ],
    }


def _matching_project_logs(project: dict[str, Any], logs_root: Path) -> str:
    reference_fields: list[tuple[str, str]] = []
    project_id = str(project.get("project_id") or "").strip()
    if project_id:
        reference_fields.append(("project_id", project_id))
    for operation in project.get("operations") or []:
        if not isinstance(operation, dict):
            continue
        for key in ("operation_id", "correlation_id"):
            value = str(operation.get(key) or "").strip()
            if value:
                reference_fields.append((key, value))
    if not reference_fields or not logs_root.is_dir():
        return ""

    patterns = tuple(
        re.compile(
            rf'"{re.escape(key)}"\s*:\s*"{re.escape(value)}"'
        )
        for key, value in dict.fromkeys(reference_fields)
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    candidates: list[Path] = []
    for filename in LOG_FILENAMES:
        candidates.extend(logs_root.glob(filename))
        candidates.extend(logs_root.glob(f"{filename}.*"))
    eligible: list[Path] = []
    for path in candidates:
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        if path.is_file() and not path.is_symlink() and modified >= cutoff:
            eligible.append(path)

    sections: list[str] = []
    byte_count = 0
    line_count = 0
    for path in sorted(eligible, key=lambda item: item.stat().st_mtime):
        matches: list[str] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for raw_line in stream:
                    if not any(pattern.search(raw_line) for pattern in patterns):
                        continue
                    if _SENSITIVE_CONTENT_FIELD.search(raw_line):
                        continue
                    line = redact_text(raw_line.rstrip("\r\n"))
                    line = _WINDOWS_ABSOLUTE_PATH.sub("<redacted-path>", line)
                    line = _UNC_ABSOLUTE_PATH.sub("<redacted-path>", line)[:32768]
                    encoded_size = len(line.encode("utf-8", errors="replace")) + 1
                    if (
                        line_count >= MAX_LOG_LINES
                        or byte_count + encoded_size > MAX_LOG_BYTES
                    ):
                        break
                    matches.append(line)
                    line_count += 1
                    byte_count += encoded_size
        except OSError:
            continue
        if matches:
            sections.append(f"===== {path.name} =====\n" + "\n".join(matches))
        if line_count >= MAX_LOG_LINES or byte_count >= MAX_LOG_BYTES:
            break
    return "\n\n".join(sections) + ("\n" if sections else "")

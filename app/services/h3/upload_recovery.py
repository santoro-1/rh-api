"""H3 input-object recovery, separate from frozen creative input and paid retries."""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path

from app.models import GenerationTask
from app.workflows.base import WorkflowAsset


RECEIPT_KEY = "_h3_upload_receipt"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_REMOTE_PNG = re.compile(r"/input/(openapi/[A-Za-z0-9_./-]+\.png)(?=['\"\s]|$)", re.I)
_REMOTE_NAME = re.compile(r"[A-Za-z0-9_./\-\u0080-\uffff]{1,1000}")
_IMAGE_SLOT = re.compile(r"identity_image_[1-6]|continuity_anchor")


class H3UploadRecoveryError(ValueError):
    """A pre-submit failure: stop without scheduling another paid generation."""


def _object(value: object) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _missing_png(task: GenerationTask) -> tuple[str, dict] | None:
    if task.workflow_type != "minimax_h3_ref2va":
        return None
    try:
        history = json.loads(task.runninghub_attempt_history or "[]")
    except (ValueError, TypeError):
        return None
    if (
        not isinstance(history, list)
        or not history
        or not isinstance(history[-1], dict)
    ):
        return None
    last = history[-1]
    reason = _object(last.get("failedReason"))
    if (
        last.get("status") != "FAILED"
        or reason.get("node_name") != "LoadImage"
        or str(reason.get("exception_type", "")).split(".")[-1] != "FileNotFoundError"
        or "no such file or directory"
        not in str(reason.get("exception_message", "")).lower()
    ):
        return None
    matches = _REMOTE_PNG.findall(str(reason.get("exception_message", "")))
    if len(matches) != 1 or ".." in matches[0].split("/"):
        return None
    return matches[0], last


def _matches_missing_asset(
    task: GenerationTask, asset: WorkflowAsset, source: Path
) -> bool:
    missing = _missing_png(task)
    if missing is None or asset.kind != "image" or source.suffix.lower() != ".png":
        return False
    filename, failure = missing
    source_sha = sha256_file(source)
    audit = _object(failure.get("uploadReceipt"))
    uploads = (
        audit.get("assets", {})
        if audit.get("remote_task_id") == failure.get("taskId")
        else {}
    )
    if isinstance(uploads, dict):
        matched = [
            (slot, record)
            for slot, record in uploads.items()
            if isinstance(record, dict) and record.get("remote_filename") == filename
        ]
        if matched:
            return any(
                slot == asset.name and r.get("source_sha256") == source_sha
                for slot, r in matched
            )
    # Historical tasks predate upload receipts. Only an exact content-key match
    # is safe; never guess the first image from an arbitrary remote filename.
    return filename.lower() == f"openapi/{source_sha}.png"


def _copy_png_with_marker(source: Path, destination: Path, marker: str) -> None:
    """Stream-copy every original chunk (including IDAT/colour/EXIF/APNG).

    Check the complete chunk envelope and CRCs, then insert one ancillary tEXt
    chunk before IEND. No decoder, re-encoding, colour conversion or Pillow is
    needed in production. The encoded pixel stream stays byte-for-byte intact.
    """
    if source.resolve() == destination.resolve():
        raise H3UploadRecoveryError("不能覆盖 H3 原始图片")
    total = 0
    saw_header = saw_pixels = False
    with source.open("rb") as src, destination.open("xb") as dst:
        if src.read(8) != PNG_SIGNATURE:
            raise H3UploadRecoveryError("H3 原图不是有效 PNG，已停止重试上传")
        dst.write(PNG_SIGNATURE)
        while True:
            header = src.read(8)
            if len(header) != 8:
                raise H3UploadRecoveryError("H3 PNG 数据不完整，已停止重试上传")
            size, kind = struct.unpack(">I4s", header)
            total += size + 12
            if total > 200 * 1024 * 1024 or not re.fullmatch(rb"[A-Za-z]{4}", kind):
                raise H3UploadRecoveryError("H3 PNG 数据块不合法或超过恢复大小上限")
            if not saw_header and (kind != b"IHDR" or size != 13):
                raise H3UploadRecoveryError("H3 PNG 缺少有效图片头")
            if kind == b"IHDR":
                if saw_header or size != 13:
                    raise H3UploadRecoveryError("H3 PNG 图片头重复或不合法")
                saw_header = True
            if kind == b"IEND":
                if size != 0 or not saw_pixels:
                    raise H3UploadRecoveryError("H3 PNG 缺少像素数据或结束标记异常")
                payload = b"RunningHub-Retry\0" + marker.encode("ascii")
                text_chunk = b"tEXt" + payload
                dst.write(
                    struct.pack(">I", len(payload))
                    + text_chunk
                    + struct.pack(">I", zlib.crc32(text_chunk))
                )
            dst.write(header)
            checksum = zlib.crc32(kind)
            remaining = size
            while remaining:
                block = src.read(min(remaining, 1024 * 1024))
                if not block:
                    raise H3UploadRecoveryError("H3 PNG 数据被截断")
                checksum = zlib.crc32(block, checksum)
                dst.write(block)
                remaining -= len(block)
            crc = src.read(4)
            if len(crc) != 4 or struct.unpack(">I", crc)[0] != checksum:
                raise H3UploadRecoveryError("H3 PNG 完整性校验失败，原图未被修改")
            dst.write(crc)
            saw_pixels = saw_pixels or kind == b"IDAT"
            if kind == b"IEND":
                if src.read(1):
                    raise H3UploadRecoveryError("H3 PNG 结束标记后有异常数据")
                return


def prepare_png_retry(
    task: GenerationTask, asset: WorkflowAsset, source: Path, work_dir: Path
) -> Path:
    if not _matches_missing_asset(task, asset, source):
        return source
    if not _IMAGE_SLOT.fullmatch(asset.name):
        raise H3UploadRecoveryError("无法确认丢失图片对应的 H3 素材槽位")
    missing = _missing_png(task)
    assert missing is not None
    # Stable across capacity waits / worker restarts for this logical retry,
    # different after each explicitly retried remote failure. Not a new asset.
    marker = hashlib.sha256(
        json.dumps(
            [
                "h3.png-object-recovery.v1",
                task.id,
                missing[1].get("taskId"),
                missing[0],
                asset.name,
                str(task.created_at),
            ],
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    destination = Path(work_dir) / f"h3-retry-{asset.name}-{marker[:16]}.png"
    _copy_png_with_marker(source, destination, marker)
    return destination


def start_upload_receipt(task: GenerationTask, attempt_id: str) -> None:
    payload = _object(task.input_payload)
    payload[RECEIPT_KEY] = {
        "schema": "h3.upload-receipt.v1",
        "attempt_id": attempt_id,
        "execution_account_id": task.execution_account_id,
        "assets": {},
    }
    task.input_payload = json.dumps(payload, ensure_ascii=False)


def record_upload(
    task: GenerationTask,
    asset: WorkflowAsset,
    source: Path,
    uploaded: Path,
    remote_name: str,
) -> dict:
    # A malformed provider response must not smuggle URLs/credentials into logs.
    if (
        not isinstance(remote_name, str)
        or not _REMOTE_NAME.fullmatch(remote_name)
        or remote_name.startswith("/")
        or ".." in remote_name.split("/")
    ):
        raise H3UploadRecoveryError("RunningHub 返回的素材文件名不合法，已停止生成提交")
    source_sha = sha256_file(source)
    receipt = {
        "source_sha256": source_sha,
        "upload_sha256": source_sha if uploaded == source else sha256_file(uploaded),
        "source_size": source.stat().st_size,
        "upload_size": uploaded.stat().st_size,
        "original_name": Path(asset.original_name.replace("\\", "/")).name[:255],
        "remote_filename": remote_name,
        "refreshed": uploaded != source,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    payload = _object(task.input_payload)
    audit = _object(payload.get(RECEIPT_KEY))
    audit.setdefault("assets", {})[asset.name] = receipt
    payload[RECEIPT_KEY] = audit
    task.input_payload = json.dumps(payload, ensure_ascii=False)
    return receipt


def ensure_recovered_uploads(task: GenerationTask) -> None:
    missing = _missing_png(task)
    if missing is None:
        return
    receipt = _object(_object(task.input_payload).get(RECEIPT_KEY))
    assets = receipt.get("assets", {})
    refreshed = [r for r in assets.values() if r.get("refreshed")]
    if not refreshed:
        raise H3UploadRecoveryError(
            "无法安全定位远端丢失的 PNG，已停止生成提交，请检查素材上传记录"
        )
    if any(r.get("remote_filename") == missing[0] for r in assets.values()):
        raise H3UploadRecoveryError(
            "RunningHub 仍返回已失效的图片文件名，已停止生成提交；请稍后检查或联系平台"
        )


def bind_upload_receipt(task: GenerationTask, remote_id: str) -> None:
    payload = _object(task.input_payload)
    audit = _object(payload.get(RECEIPT_KEY))
    audit["remote_task_id"] = remote_id
    payload[RECEIPT_KEY] = audit
    task.input_payload = json.dumps(payload, ensure_ascii=False)


def failure_upload_receipt(task: GenerationTask) -> dict | None:
    audit = _object(_object(task.input_payload).get(RECEIPT_KEY))
    return (
        audit
        if audit.get("remote_task_id") == task.runninghub_task_id
        and task.runninghub_task_id
        else None
    )

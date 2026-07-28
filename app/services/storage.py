from __future__ import annotations

import mimetypes
import os
import shutil
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.config import Settings


class UploadValidationError(ValueError):
    """The uploaded file is invalid for the requested slot."""


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}
IMAGE_MIME_PREFIX = "image/"
VIDEO_MIME_PREFIX = "video/"
AUDIO_MIME_TYPES = {
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
    "audio/mp4",
    "audio/x-m4a",
    "audio/aac",
    "audio/flac",
    "audio/x-flac",
}


def safe_relative_path(path: str, data_dir: Path) -> Path:
    root = data_dir.resolve()
    resolved = (root / path).resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError("非法文件路径")
    return resolved


def _validate_content_signature(path: Path, kind: str) -> None:
    header = path.read_bytes()[:32]
    if kind == "image":
        valid = (
            header.startswith(b"\xff\xd8\xff")
            or header.startswith(b"\x89PNG\r\n\x1a\n")
            or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
        )
    elif kind == "audio":
        valid = (
            header.startswith(b"ID3")
            or header[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf1"}
            or (header.startswith(b"RIFF") and header[8:12] == b"WAVE")
            or header.startswith(b"fLaC")
            or header[4:8] == b"ftyp"
        )
    else:
        valid = (
            header[4:8] == b"ftyp"
            or header.startswith(b"\x1a\x45\xdf\xa3")
        )
    if not valid:
        raise UploadValidationError("文件内容与声明的格式不匹配")


def _validate_mime(upload: UploadFile, kind: str) -> None:
    content_type = (upload.content_type or "").lower()
    if not content_type:
        return
    if kind == "image" and content_type.startswith(IMAGE_MIME_PREFIX):
        return
    if kind == "audio" and content_type in AUDIO_MIME_TYPES:
        return
    if kind == "video" and content_type.startswith(VIDEO_MIME_PREFIX):
        return
    raise UploadValidationError("文件 MIME 类型不受支持")


def save_upload(
    upload: UploadFile,
    destination_dir: Path,
    kind: str,
    settings: Settings,
) -> tuple[Path, str]:
    original_name = Path(upload.filename or "").name
    if not original_name:
        raise UploadValidationError("请选择文件")
    extension = Path(original_name).suffix.lower()
    allowed_extensions = {
        "image": IMAGE_EXTENSIONS,
        "audio": AUDIO_EXTENSIONS,
        "video": VIDEO_EXTENSIONS,
    }.get(kind)
    if allowed_extensions is None:
        raise UploadValidationError("未知的上传素材类型")
    if extension not in allowed_extensions:
        raise UploadValidationError("文件扩展名不受支持")
    _validate_mime(upload, kind)

    max_megabytes = {
        "image": settings.max_image_size_mb,
        "audio": settings.max_audio_size_mb,
        "video": settings.max_video_size_mb,
    }[kind]
    max_bytes = max_megabytes * 1024 * 1024
    destination_dir.mkdir(parents=True, exist_ok=True)
    output_path = destination_dir / f"{kind}-{uuid.uuid4().hex}{extension}"
    written = 0
    try:
        with output_path.open("wb") as output:
            while chunk := upload.file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise UploadValidationError("上传文件超过大小限制")
                output.write(chunk)
        if written == 0:
            raise UploadValidationError("上传文件不能为空")
        _validate_content_signature(output_path, kind)
        guessed_type, _ = mimetypes.guess_type(output_path.name)
        if kind == "image" and guessed_type and not guessed_type.startswith("image/"):
            raise UploadValidationError("图片格式不正确")
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    finally:
        upload.file.seek(0)
    return output_path, original_name


def remove_directory(path: Path) -> None:
    """Remove a known task directory; callers must construct this from trusted IDs."""
    if path.exists():
        shutil.rmtree(path)


def task_upload_dir(settings: Settings, user_id: int, task_id: str) -> Path:
    return settings.uploads_dir / str(user_id) / task_id


def task_output_dir(settings: Settings, user_id: int, task_id: str) -> Path:
    return settings.outputs_dir / str(user_id) / task_id


def staged_asset_dir(settings: Settings, user_id: int, asset_id: str) -> Path:
    return settings.staged_assets_dir / str(user_id) / asset_id


def voice_source_dir(settings: Settings, user_id: int, voice_asset_id: str) -> Path:
    return settings.voice_sources_dir / str(user_id) / voice_asset_id


def voice_creation_dir(settings: Settings, user_id: int, task_id: str) -> Path:
    return settings.voice_creations_dir / str(user_id) / task_id


def materialize_staged_asset(
    source: Path,
    destination_dir: Path,
    *,
    kind: str,
) -> Path:
    """Give each task its own path while allowing efficient same-disk reuse."""

    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / f"{kind}-{uuid.uuid4().hex}{source.suffix.lower()}"
    try:
        os.link(source, target)
    except OSError:
        # Hard links keep repeated large videos cheap, but copying remains a
        # portable fallback for filesystems or deployments that disallow them.
        shutil.copy2(source, target)
    return target


def to_relative_data_path(path: Path, settings: Settings) -> str:
    return str(path.resolve().relative_to(settings.data_dir.resolve())).replace("\\", "/")


def create_download_target(
    settings: Settings, user_id: int, task_id: str, extension: str
) -> Path:
    extension = extension.lower().lstrip(".")
    if extension not in {"mp4", "webm", "mov"}:
        extension = "mp4"
    directory = task_output_dir(settings, user_id, task_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"result.{extension}"

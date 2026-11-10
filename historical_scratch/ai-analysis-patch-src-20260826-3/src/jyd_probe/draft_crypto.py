from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import uuid

from .runtime_paths import jy_draftc_exe_path


@dataclass(frozen=True)
class PreparedDraftDir:
    source_dir: Path
    draft_dir: Path
    was_decrypted: bool


def is_plain_json_file(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as f:
            json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def default_jy_draftc_exe() -> Path:
    return jy_draftc_exe_path()


def detect_jianying_install_dir() -> Path | None:
    configured = os.environ.get("JY_INSTALL_DIR", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        apps_root = Path(local_app_data) / "JianyingPro" / "Apps"
        if apps_root.is_dir():
            candidates.extend(
                sorted(
                    (item for item in apps_root.iterdir() if item.is_dir()),
                    key=lambda item: _version_key(item.name),
                    reverse=True,
                )
            )

    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name, "").strip()
        if base:
            candidates.extend(
                [
                    Path(base) / "JianyingPro",
                    Path(base) / "JianyingPro" / "Apps",
                ]
            )

    detected = _first_valid_install_dir(candidates)
    if detected is not None:
        return detected
    return _first_valid_install_dir(_fixed_drive_jianying_candidates())


def _first_valid_install_dir(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if (resolved / "videoeditor.dll").is_file():
            return resolved
        if resolved.is_dir():
            try:
                nested = sorted(
                    resolved.glob("*/videoeditor.dll"),
                    key=lambda item: _version_key(item.parent.name),
                    reverse=True,
                )
            except OSError:
                nested = []
            if nested:
                return nested[0].parent.resolve()
    return None


def _fixed_drive_jianying_candidates() -> list[Path]:
    """Find Jianying version directories without recursively scanning whole drives.

    Custom installations commonly look like
    ``D:\\软件\\JianyingPro\\11.0.0.14274``.  Only a few bounded directory
    shapes are checked so collector startup does not turn into a full-disk scan.
    """

    patterns = (
        "JianyingPro/videoeditor.dll",
        "JianyingPro/*/videoeditor.dll",
        "JianyingPro/Apps/*/videoeditor.dll",
        "*/JianyingPro/videoeditor.dll",
        "*/JianyingPro/*/videoeditor.dll",
        "*/JianyingPro/Apps/*/videoeditor.dll",
        "*/*/JianyingPro/videoeditor.dll",
        "*/*/JianyingPro/*/videoeditor.dll",
        "*/*/JianyingPro/Apps/*/videoeditor.dll",
    )
    found: dict[str, Path] = {}
    for root in _windows_fixed_drive_roots():
        for pattern in patterns:
            try:
                matches = root.glob(pattern)
                for dll in matches:
                    if not dll.is_file() or dll.name.lower() != "videoeditor.dll":
                        continue
                    install_dir = dll.parent.resolve()
                    found.setdefault(os.path.normcase(str(install_dir)), install_dir)
            except OSError:
                continue
    return sorted(
        found.values(),
        key=lambda item: _version_key(item.name),
        reverse=True,
    )


def _windows_fixed_drive_roots() -> list[Path]:
    if os.name != "nt":
        return []

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        drive_mask = int(kernel32.GetLogicalDrives())
    except (AttributeError, OSError, ValueError):
        return []

    roots: list[Path] = []
    drive_type_fixed = 3
    for index in range(26):
        if not drive_mask & (1 << index):
            continue
        root = Path(f"{chr(ord('A') + index)}:/")
        try:
            if int(kernel32.GetDriveTypeW(str(root))) == drive_type_fixed:
                roots.append(root)
        except (OSError, ValueError):
            continue
    return roots


def ensure_jy_draftc_env(exe: Path, install_dir: Path | None) -> None:
    exe_dir = exe.resolve().parent
    env_path = exe_dir / ".env"

    if install_dir is None:
        saved_dir = _read_jy_install_dir(env_path)
        if saved_dir is not None and (saved_dir / "videoeditor.dll").is_file():
            return
        install_dir = detect_jianying_install_dir()
        if install_dir is None:
            raise FileNotFoundError(
                "没有找到剪映 videoeditor.dll。请安装剪映，或设置 JY_INSTALL_DIR 环境变量。"
            )

    install_dir = install_dir.resolve()
    if not install_dir.exists():
        raise FileNotFoundError(f"剪映安装目录不存在: {install_dir}")
    if not (install_dir / "videoeditor.dll").exists():
        raise FileNotFoundError(f"剪映安装目录下找不到 videoeditor.dll: {install_dir}")
    env_path.write_text(f"JY_INSTALL_DIR={install_dir}\n", encoding="utf-8")


def _read_jy_install_dir(env_path: Path) -> Path | None:
    if not env_path.is_file():
        return None
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip() == "JY_INSTALL_DIR" and value.strip():
            return Path(value.strip()).expanduser().resolve()
    return None


def _version_key(value: str) -> tuple[int, ...]:
    numbers: list[int] = []
    for part in value.replace("-", ".").split("."):
        try:
            numbers.append(int(part))
        except ValueError:
            numbers.append(0)
    return tuple(numbers)


def decrypt_json_file(
    path: Path,
    *,
    exe: Path | None = None,
    install_dir: Path | None = None,
    debug: bool = False,
) -> None:
    exe = (exe or default_jy_draftc_exe()).resolve()
    if not exe.exists():
        raise FileNotFoundError(f"jy-draftc.exe 不存在: {exe}")
    if not path.exists():
        raise FileNotFoundError(f"待解密文件不存在: {path}")

    ensure_jy_draftc_env(exe, install_dir)
    tmp_output = path.with_name(f"{path.name}.plain.{uuid.uuid4().hex}.tmp")

    args = [str(exe)]
    if debug:
        args.append("--debug")
    args.extend(["-d", str(path.resolve()), str(tmp_output.resolve())])

    proc = subprocess.run(
        args,
        cwd=str(exe.parent),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if proc.returncode != 0:
        if tmp_output.exists():
            tmp_output.unlink()
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"jy-draftc 解密失败: {path}，exit={proc.returncode}，{detail}")

    if not is_plain_json_file(tmp_output):
        if tmp_output.exists():
            tmp_output.unlink()
        raise RuntimeError(f"jy-draftc 输出不是合法明文 JSON: {tmp_output}")

    try:
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    except OSError:
        pass

    try:
        tmp_output.replace(path)
    except PermissionError:
        path.write_bytes(tmp_output.read_bytes())
        try:
            tmp_output.unlink()
        except OSError:
            pass


def prepare_plain_draft_dir(
    template_dir: Path,
    *,
    auto_decrypt: bool = True,
    force_decrypt: bool = False,
    work_root: Path | None = None,
    exe: Path | None = None,
    install_dir: Path | None = None,
    debug: bool = False,
) -> PreparedDraftDir:
    source_dir = template_dir.resolve()
    draft_content = source_dir / "draft_content.json"
    if not draft_content.exists():
        raise FileNotFoundError(f"模板草稿缺少 draft_content.json: {draft_content}")

    if not force_decrypt and is_plain_json_file(draft_content):
        return PreparedDraftDir(source_dir=source_dir, draft_dir=source_dir, was_decrypted=False)

    if not auto_decrypt and not force_decrypt:
        raise RuntimeError(f"{draft_content} 不是明文 JSON，并且自动解密已关闭")

    if work_root is None:
        configured_work_root = os.environ.get("JYD_DECRYPT_WORK_ROOT", "").strip()
        work_root = (
            Path(configured_work_root).expanduser()
            if configured_work_root
            else Path(__file__).resolve().parents[2] / "runtime" / "decrypted_work"
        )
    work_root = work_root.resolve()
    work_root.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir = work_root / f"{source_dir.name}_decrypted_{stamp}_{uuid.uuid4().hex[:8]}"
    shutil.copytree(source_dir, work_dir)

    for filename in ["draft_content.json", "draft_meta_info.json"]:
        target = work_dir / filename
        if not target.exists():
            continue
        if force_decrypt or not is_plain_json_file(target):
            decrypt_json_file(
                target,
                exe=exe,
                install_dir=install_dir,
                debug=debug,
            )

    return PreparedDraftDir(source_dir=source_dir, draft_dir=work_dir, was_decrypted=True)

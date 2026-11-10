from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


ROOT_FILES = {
    "启动媒体节点.cmd",
    "配置媒体节点.cmd",
    "更新媒体节点.cmd",
    "使用说明.txt",
}
CODE_ROOTS = {"app", "media_node"}


class PortableUpdateError(RuntimeError):
    pass


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig").strip()


def _safe_member(name: str) -> PurePosixPath:
    value = PurePosixPath(name)
    if value.is_absolute() or not value.parts or ".." in value.parts:
        raise PortableUpdateError(f"更新包包含不安全路径：{name}")
    if value.parts[0] not in CODE_ROOTS and name not in ROOT_FILES:
        raise PortableUpdateError(f"更新包包含未知文件：{name}")
    if value.parts[0] == "media_node":
        protected = {".env", ".runtime", "data", "logs"}
        if any(part in protected for part in value.parts[1:]):
            raise PortableUpdateError(f"更新包试图覆盖本机配置或数据：{name}")
    return value


def _backup_code(root: Path, backup: Path) -> None:
    sources = [root / "app", root / "media_node"]
    with zipfile.ZipFile(
        backup,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        allowZip64=True,
    ) as output:
        for source in sources:
            if not source.is_dir():
                continue
            for path in source.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(root)
                if "__pycache__" in relative.parts:
                    continue
                if relative.parts[0] == "media_node" and any(
                    part in {".env", ".runtime", "data", "logs"}
                    for part in relative.parts[1:]
                ):
                    continue
                output.write(path, relative.as_posix())
        for name in ROOT_FILES:
            path = root / name
            if path.is_file():
                output.write(path, name)


def apply_update(archive: Path, root: Path) -> Path:
    archive = archive.resolve()
    root = root.resolve()
    runtime_file = root / "portable-runtime.txt"
    if not (root / "python" / "python.exe").is_file() or not runtime_file.is_file():
        raise PortableUpdateError("目标目录不是完整的独立媒体节点")
    if not archive.is_file():
        raise PortableUpdateError(f"找不到更新包：{archive}")

    with zipfile.ZipFile(archive) as package:
        try:
            manifest = json.loads(package.read("portable-update.json"))
        except (KeyError, json.JSONDecodeError) as exc:
            raise PortableUpdateError("更新包缺少有效的版本清单") from exc
        required_runtime = str(manifest.get("runtimeId") or "").strip()
        installed_runtime = _read_text(runtime_file)
        if not required_runtime or required_runtime != installed_runtime:
            raise PortableUpdateError(
                "该更新需要不同的 Python/模型运行环境，请下载新的完整包"
            )

        members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        for info in package.infolist():
            if info.is_dir() or info.filename == "portable-update.json":
                continue
            members.append((info, _safe_member(info.filename)))

        updates = root / "updates"
        backups = updates / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = backups / f"code-{stamp}.zip"
        _backup_code(root, backup)

        staging = Path(tempfile.mkdtemp(prefix="apply-", dir=updates))
        try:
            for info, relative in members:
                target = staging.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            for _, relative in members:
                source = staging.joinpath(*relative.parts)
                target = root.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    applied = root / "updates" / f"applied-{stamp}.json"
    applied.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return backup


def main() -> int:
    if len(sys.argv) != 3:
        print("用法：apply_portable_update.py <更新 ZIP> <媒体节点目录>")
        return 2
    try:
        backup = apply_update(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, zipfile.BadZipFile, PortableUpdateError) as exc:
        print(f"更新失败：{exc}", file=sys.stderr)
        return 1
    print(f"更新完成；更新前代码备份：{backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import sys
import zipfile
from pathlib import Path


def create_archive(source: Path, archive: Path) -> None:
    source = source.resolve()
    archive = archive.resolve()
    if not source.is_dir():
        raise ValueError(f"便携包目录不存在：{source}")
    if archive.exists():
        raise ValueError(f"ZIP 已存在：{archive}")

    files = sorted(path for path in source.rglob("*") if path.is_file())
    total_bytes = sum(path.stat().st_size for path in files)
    written_bytes = 0
    next_report = 256 * 1024 * 1024
    root = source.parent
    try:
        with zipfile.ZipFile(
            archive,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as output:
            for index, path in enumerate(files, start=1):
                output.write(path, path.relative_to(root).as_posix())
                written_bytes += path.stat().st_size
                if written_bytes >= next_report or index == len(files):
                    percent = (
                        written_bytes / total_bytes * 100 if total_bytes else 100
                    )
                    print(
                        f"ZIP 进度：{index}/{len(files)} 个文件，"
                        f"{written_bytes / 1024 / 1024:.0f} MB "
                        f"({percent:.1f}%)",
                        flush=True,
                    )
                    next_report = written_bytes + 256 * 1024 * 1024
    except BaseException:
        archive.unlink(missing_ok=True)
        raise


def main() -> int:
    if len(sys.argv) != 3:
        print("用法：create_portable_zip.py <源目录> <输出 ZIP>", file=sys.stderr)
        return 2
    create_archive(Path(sys.argv[1]), Path(sys.argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

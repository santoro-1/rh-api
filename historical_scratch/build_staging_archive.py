from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tarfile


REPO = Path(r"D:\工作内容\轻盈健\数字人\runninghub_mvp")
OUTPUT_DIR = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\device-auth-staging")
ARCHIVE = OUTPUT_DIR / "runninghub-video-staging-release.tar"
ALLOWED_ROOTS = ("app/", "alembic/", "deploy/", "scripts/")
ALLOWED_FILES = {"alembic.ini", "requirements.txt"}


def tracked_and_untracked_files() -> list[str]:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.quotepath=false",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    info.mode = 0o755 if info.name.endswith((".sh", ".py")) else 0o644
    return info


def main() -> None:
    files = sorted(
        path
        for path in tracked_and_untracked_files()
        if path in ALLOWED_FILES or path.startswith(ALLOWED_ROOTS)
    )
    required = {
        "app/main.py",
        "alembic/versions/0050_device_work_admission.py",
        "deploy/device_auth_keys.py",
        "deploy/device_auth_control.py",
        "requirements.txt",
    }
    missing = sorted(required.difference(files))
    if missing:
        raise SystemExit(f"release manifest missing required files: {missing}")
    for relative in files:
        if relative == ".env" or relative.startswith(("data/", ".git/")):
            raise SystemExit(f"forbidden release path: {relative}")
        source = REPO / relative
        if not source.is_file() or source.is_symlink():
            raise SystemExit(f"release path is not a regular file: {relative}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE.unlink(missing_ok=True)
    with tarfile.open(ARCHIVE, "w", format=tarfile.PAX_FORMAT) as package:
        for relative in files:
            package.add(REPO / relative, arcname=relative, recursive=False, filter=normalize)

    digest = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest().upper()
    print(
        json.dumps(
            {
                "archive": str(ARCHIVE),
                "bytes": ARCHIVE.stat().st_size,
                "files": len(files),
                "sha256": digest,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

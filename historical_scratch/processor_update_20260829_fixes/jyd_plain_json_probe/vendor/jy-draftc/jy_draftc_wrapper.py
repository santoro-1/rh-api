from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def fail(message: str, code: int = 1) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def exe_dir_from(exe: Path) -> Path:
    return exe.resolve().parent


def write_env_if_requested(exe_dir: Path, install_dir: Path | None) -> None:
    env_path = exe_dir / ".env"
    if install_dir is None:
        if not env_path.exists():
            raise FileNotFoundError(
                f"{env_path} not found. Pass --install-dir or create .env next to jy-draftc.exe."
            )
        return

    install_dir = install_dir.resolve()
    if not install_dir.exists():
        raise FileNotFoundError(f"install dir does not exist: {install_dir}")
    if not (install_dir / "videoeditor.dll").exists():
        raise FileNotFoundError(f"videoeditor.dll not found under: {install_dir}")

    env_path.write_text(f"JY_INSTALL_DIR={install_dir}\n", encoding="utf-8")


def run_tool(
    exe: Path,
    mode: str,
    input_path: Path,
    output_path: Path | None,
    debug: bool,
) -> int:
    exe = exe.resolve()
    if not exe.exists():
        return fail(f"jy-draftc.exe not found: {exe}")

    input_path = input_path.resolve()
    if not input_path.exists():
        return fail(f"input file not found: {input_path}")

    args = [str(exe)]
    if debug:
        args.append("--debug")
    args.extend(["-d" if mode == "decrypt" else "-e", str(input_path)])
    if output_path is not None:
        args.append(str(output_path.resolve()))

    proc = subprocess.run(
        args,
        cwd=str(exe.parent),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )

    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Python wrapper for jy-draftc.exe.",
    )
    parser.add_argument(
        "mode",
        choices=("decrypt", "encrypt"),
        help="decrypt encrypted Jianying JSON or encrypt plaintext JSON back.",
    )
    parser.add_argument("input", type=Path, help="Input JSON file.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output file. If omitted, jy-draftc.exe uses its default suffix.",
    )
    parser.add_argument(
        "--install-dir",
        type=Path,
        help="Jianying install/version directory that contains videoeditor.dll. "
        "When provided, this updates .env next to jy-draftc.exe.",
    )
    parser.add_argument(
        "--exe",
        type=Path,
        default=Path(__file__).resolve().with_name("jy-draftc.exe"),
        help="Path to jy-draftc.exe. Defaults to jy-draftc.exe next to this script.",
    )
    parser.add_argument("--debug", action="store_true", help="Pass --debug to jy-draftc.exe.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    exe = args.exe.resolve()
    try:
        write_env_if_requested(exe_dir_from(exe), args.install_dir)
    except Exception as exc:
        return fail(str(exc))

    return run_tool(
        exe=exe,
        mode=args.mode,
        input_path=args.input,
        output_path=args.output,
        debug=args.debug,
    )


if __name__ == "__main__":
    raise SystemExit(main())

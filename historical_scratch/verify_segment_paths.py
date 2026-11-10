"""Isolated read/copy validation; never edits the user's original draft."""
import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\src")
from jyd_probe.render_job import run_render_job


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def ordinary_windows_read(path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        return {"readable": False, "error": ctypes.get_last_error()}
    kernel.CloseHandle(handle)
    return {"readable": True, "error": 0}


source = Path(r"D:\剪映草稿\JianyingPro Drafts\2-composition-07")
source_file = source / "draft_content.json"
before_hash = digest(source_file)
before = json.loads(source_file.read_text(encoding="utf-8"))
result = run_render_job({
    "source": {"type": "template", "template_draft_dir": str(source)},
    "output": {
        "draft_root": r"D:\工作内容\轻盈健\数字人\.codex_tmp\segment-access-20260831\verified-drafts",
        "draft_name": "short-path-verified",
        "skip_export": True,
    },
})
after = json.loads((result.output_draft_dir / "draft_content.json").read_text(encoding="utf-8"))
assert before["tracks"] == after["tracks"], "Timeline must stay identical"
assert before["duration"] == after["duration"]
old_media = {m["id"]: m for group in ("videos", "audios") for m in before["materials"][group]}
new_media = {m["id"]: m for group in ("videos", "audios") for m in after["materials"][group]}
checks = []
for key, old in old_media.items():
    old_path = Path(old["path"])
    new_path = Path(new_media[key]["path"])
    if old_path == new_path:
        continue
    read_check = ordinary_windows_read(new_path)
    assert read_check["readable"], read_check
    assert digest(old_path) == digest(new_path)
    assert new_path.parent == result.output_draft_dir / "jyd_media"
    assert len(str(new_path)) <= 240
    checks.append({
        "name": old.get("material_name", old.get("name")),
        "old_length": len(str(old_path)),
        "new_length": len(str(new_path)),
        "identical_bytes": True,
        "windows_read": read_check,
    })
assert len([row for row in checks if row["name"] == "segment.mp4"]) == 3
assert digest(source_file) == before_hash, "Original draft changed"
print(json.dumps({
    "output_draft": str(result.output_draft_dir),
    "original_untouched": True,
    "timeline_unchanged": True,
    "exported": result.exported,
    "media": checks,
}, ensure_ascii=False, indent=2))

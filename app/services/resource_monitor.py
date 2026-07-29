from __future__ import annotations

import ctypes
import os
import shutil
import threading
import time
from pathlib import Path

from app.config import Settings


_CPU_LOCK = threading.Lock()
_CPU_SAMPLE: tuple[int, int] | None = None


def _linux_cpu_times() -> tuple[int, int] | None:
    try:
        fields = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()
        values = [int(value) for value in fields[1:]]
    except (OSError, ValueError, IndexError):
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def _windows_cpu_times() -> tuple[int, int] | None:
    if os.name != "nt":
        return None
    idle = ctypes.c_ulonglong()
    kernel = ctypes.c_ulonglong()
    user = ctypes.c_ulonglong()
    if not ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
        ctypes.byref(idle),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        return None
    return kernel.value + user.value, idle.value


def _cpu_percent() -> float | None:
    global _CPU_SAMPLE
    sample = _linux_cpu_times() or _windows_cpu_times()
    if sample is None:
        return None
    with _CPU_LOCK:
        previous = _CPU_SAMPLE
        _CPU_SAMPLE = sample
    if previous is None:
        return None
    total_delta = sample[0] - previous[0]
    idle_delta = sample[1] - previous[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1)


def _linux_memory() -> tuple[int, int] | None:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
        return values["MemTotal"], values["MemAvailable"]
    except (OSError, ValueError, KeyError):
        return None


def _windows_memory() -> tuple[int, int] | None:
    if os.name != "nt":
        return None

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(  # type: ignore[attr-defined]
        ctypes.byref(status)
    ):
        return None
    return status.total_physical, status.available_physical


def _project_processes() -> tuple[int, int]:
    """Return process count and RSS for the dedicated project Linux user."""

    proc = Path("/proc")
    if not proc.is_dir():
        return 0, 0
    current_uid = os.getuid() if hasattr(os, "getuid") else None
    count = 0
    rss_bytes = 0
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if current_uid is not None and entry.stat().st_uid != current_uid:
                continue
            status_lines = (entry / "status").read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            rss_kb = next(
                (
                    int(line.split()[1])
                    for line in status_lines
                    if line.startswith("VmRSS:")
                ),
                0,
            )
        except (OSError, ValueError, StopIteration):
            continue
        count += 1
        rss_bytes += rss_kb * 1024
    return count, rss_bytes


def _ffmpeg_processes() -> tuple[int, int]:
    proc = Path("/proc")
    if not proc.is_dir():
        return 0, 0
    count = 0
    rss_bytes = 0
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text(encoding="ascii").strip() != "ffmpeg":
                continue
            status = (entry / "status").read_text(encoding="ascii")
            rss_kb = next(
                int(line.split()[1])
                for line in status.splitlines()
                if line.startswith("VmRSS:")
            )
        except (OSError, ValueError, StopIteration):
            continue
        count += 1
        rss_bytes += rss_kb * 1024
    return count, rss_bytes


def resource_snapshot(settings: Settings) -> dict[str, object]:
    memory = _linux_memory() or _windows_memory()
    total_memory, available_memory = memory or (0, 0)
    used_memory = max(total_memory - available_memory, 0)
    disk = shutil.disk_usage(settings.data_dir)
    project_count, project_rss = _project_processes()
    ffmpeg_count, ffmpeg_rss = _ffmpeg_processes()
    try:
        load = tuple(round(value, 2) for value in os.getloadavg())
    except (AttributeError, OSError):
        load = ()
    return {
        "cpuPercent": _cpu_percent(),
        "memory": {
            "totalBytes": total_memory,
            "usedBytes": used_memory,
            "availableBytes": available_memory,
            "usedPercent": (
                round(used_memory / total_memory * 100, 1)
                if total_memory
                else None
            ),
        },
        "disk": {
            "totalBytes": disk.total,
            "usedBytes": disk.used,
            "freeBytes": disk.free,
            "usedPercent": round(disk.used / disk.total * 100, 1),
        },
        "loadAverage": load,
        "project": {
            "processCount": project_count,
            "rssBytes": project_rss,
        },
        "ffmpeg": {
            "processCount": ffmpeg_count,
            "rssBytes": ffmpeg_rss,
        },
        "capturedAt": time.time(),
    }

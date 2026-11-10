# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path(SPECPATH).resolve().parents[1]
app_root = project_root / "apps" / "collector"

datas = [
    (str(app_root / "frontend"), "apps/collector/frontend"),
]
binaries = [
    (str(project_root / "vendor" / "jy-draftc" / "jy-draftc.exe"), "jy-draftc"),
]
hiddenimports = [
    "tkinter",
    "tkinter.filedialog",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

a = Analysis(
    [str(app_root / "run_local_collector.py")],
    pathex=[str(project_root), str(app_root), str(project_root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "PyQt5",
        "matplotlib",
        "pandas",
        "pytest",
        "scipy",
        "sphinx",
        "torch",
        "unittest",
        "__main__",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JianyingDraftCollector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="JianyingDraftCollector",
)

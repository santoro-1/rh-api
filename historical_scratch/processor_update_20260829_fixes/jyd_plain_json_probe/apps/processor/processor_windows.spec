# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_all

project_root = Path(SPECPATH).resolve().parents[1]
app_root = project_root / "apps" / "processor"

pyjyd_datas, pyjyd_binaries, pyjyd_hiddenimports = collect_all("pyJianYingDraft")
uia_datas, uia_binaries, uia_hiddenimports = collect_all("uiautomation")
cv2_datas, cv2_binaries, cv2_hiddenimports = collect_all("cv2")
jieba_datas, jieba_binaries, jieba_hiddenimports = collect_all("jieba")

datas = [
    (str(app_root / "frontend"), "apps/processor/frontend"),
    (str(project_root / "apps" / "collector" / "frontend"), "apps/collector/frontend"),
] + pyjyd_datas + uia_datas + cv2_datas + jieba_datas
binaries = pyjyd_binaries + uia_binaries + cv2_binaries + jieba_binaries
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "pyJianYingDraft.jianying_controller",
    "tkinter",
    "tkinter.filedialog",
] + pyjyd_hiddenimports + uia_hiddenimports + cv2_hiddenimports + jieba_hiddenimports

a = Analysis(
    [str(app_root / "processor_windows.py")],
    pathex=[str(project_root), str(project_root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["IPython", "PyQt5", "matplotlib", "pandas", "pytest", "scipy", "sphinx", "torch", "__main__"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JianyingRenderServer",
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
    name="JianyingRenderServer",
)

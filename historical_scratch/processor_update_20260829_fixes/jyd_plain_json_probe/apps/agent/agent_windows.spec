# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_all

project_root = Path(SPECPATH).resolve().parents[1]
app_root = project_root / "apps" / "agent"

pyjyd_datas, pyjyd_binaries, pyjyd_hiddenimports = collect_all("pyJianYingDraft")
uia_datas, uia_binaries, uia_hiddenimports = collect_all("uiautomation")

a = Analysis(
    [str(app_root / "agent_windows.py")],
    pathex=[str(project_root), str(project_root / "src")],
    binaries=pyjyd_binaries + uia_binaries,
    datas=pyjyd_datas + uia_datas,
    hiddenimports=["pyJianYingDraft.jianying_controller"] + pyjyd_hiddenimports + uia_hiddenimports,
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
    name="JianyingRenderAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="JianyingRenderAgent",
)

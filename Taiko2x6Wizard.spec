# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

# Tools are imported lazily from the Explorer's menus, so list every module
# of the package explicitly (new modules are picked up automatically).


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('taiko2x6/exe/data/KBD2EE.irx', 'taiko2x6/exe/data')],  # USB keyboard patch module
    hiddenimports=collect_submodules('taiko2x6'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['librosa', 'numba', 'llvmlite', 'scipy', 'matplotlib', 'cryptography', 'tkinter', 'IPython', 'pytest', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.Qt3DCore', 'PySide6.QtCharts', 'PySide6.QtQuick3D'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Taiko2x6Wizard',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

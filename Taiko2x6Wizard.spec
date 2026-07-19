# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['taiko256_explorer_gui6.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['apa_merge', 'appconfig', 'apppaths', 'archive_builder', 'audioplayer', 'bineditor_enso_parts', 'bineditor_fname', 'bineditor_hdbdinfo', 'bineditor_lamp', 'bineditor_musicinfo', 'bineditor_rank', 'bineditor_streaminfo', 'bineditor_tuning', 'flipbook_player', 'gen3_convert', 'gen3_song', 'generators', 'hdbd', 'hdd_browser', 'hdd_song_wizard', 'img_slim', 'iso_packer', 'omnimix_maker', 'omnimix_gui', 'pfsshell_tool', 'ps2hdd', 'ps2mc_card', 'sht_validator', 'song_builder', 'song_manager', 'song_replacer', 'songtex', 'songtex_all', 'swg', 'swg_editor', 'taiko256_archive_tool_v2', 'taiko_exe', 'tim2', 'tja2sht', 'vagtool'],
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

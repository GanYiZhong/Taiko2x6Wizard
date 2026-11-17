# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['taiko2x6.app.appconfig', 'taiko2x6.app.apppaths', 'taiko2x6.app.taiko256_explorer_gui6', 'taiko2x6.archive.archive_builder', 'taiko2x6.archive.taiko256_archive_tool_v2', 'taiko2x6.audio.audioplayer', 'taiko2x6.audio.hdbd', 'taiko2x6.audio.vagtool', 'taiko2x6.bineditors.bineditor_enso_parts', 'taiko2x6.bineditors.bineditor_fname', 'taiko2x6.bineditors.bineditor_hdbdinfo', 'taiko2x6.bineditors.bineditor_lamp', 'taiko2x6.bineditors.bineditor_musicinfo', 'taiko2x6.bineditors.bineditor_rank', 'taiko2x6.bineditors.bineditor_streaminfo', 'taiko2x6.bineditors.bineditor_tuning', 'taiko2x6.charts.gen3_convert', 'taiko2x6.charts.gen3_song', 'taiko2x6.charts.sht_validator', 'taiko2x6.charts.tja2sht', 'taiko2x6.exe.taiko_exe', 'taiko2x6.graphics.flipbook_player', 'taiko2x6.graphics.songtex', 'taiko2x6.graphics.songtex_all', 'taiko2x6.graphics.swg', 'taiko2x6.graphics.swg_editor', 'taiko2x6.graphics.tim2', 'taiko2x6.ps2.apa_merge', 'taiko2x6.ps2.hdd_browser', 'taiko2x6.ps2.hdd_song_wizard', 'taiko2x6.ps2.img_slim', 'taiko2x6.ps2.iso_packer', 'taiko2x6.ps2.pfsshell_tool', 'taiko2x6.ps2.ps2hdd', 'taiko2x6.ps2.ps2mc_card', 'taiko2x6.songs.generators', 'taiko2x6.songs.omnimix_gui', 'taiko2x6.songs.omnimix_maker', 'taiko2x6.songs.song_builder', 'taiko2x6.songs.song_manager', 'taiko2x6.songs.song_replacer'],
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

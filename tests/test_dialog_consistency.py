"""Every dialog behaves the same way: translated title, a default button (or an
explicit reason there is none), a sensible minimum size and nothing clipped.

Each dialog is built offscreen from minimal synthetic arguments; nothing here
touches a real image or game file."""
import os
import struct
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import (QAbstractButton, QAbstractScrollArea, QApplication,  # noqa: E402
                               QDialog, QDialogButtonBox, QLabel, QPushButton,
                               QScrollArea)

from taiko2x6.app import appconfig, i18n  # noqa: E402

MAX_W, MAX_H = 1280, 900


# ---------------------------------------------------------------- synthetic data
def _rank():
    return struct.pack("<8I", 10, 4, 0, 0, 0, 0, 0, 0) + struct.pack("<44I", *range(44))


def _musicinfo():
    pool = b"SONG_A\0SONG_B\0"
    recs = b""
    for ptr in (0, 7):
        recs += struct.pack("<21i", ptr, 1, 2, *([0] * 18))
    o0 = 0x34 + len(recs)
    hdr = struct.pack("<13I", 2, 0, 0, 0, 0, 0, 0, o0, o0, o0, o0, o0, o0)
    return hdr + recs + pool


def _tuning():
    from taiko2x6.bineditors import bineditor_tuning as TU
    sid = b"aaa\0music_aaa\0"
    rec = [0] * (TU.T14_REC // 4)
    for d in range(4):
        rec[4 + 28 * d] = 5
    return struct.pack("<I", 1) + struct.pack("<507i", *rec) + sid


def _streaminfo():
    return struct.pack("<3I", 0, 20, 12) + b"\0" * 4


def _fname():
    return struct.pack("<3I", 0, 0, 0) + bytes(10 * 2)


def _hdbdinfo():
    return struct.pack("<5I", 0, 0, 20, 20, 20) + b"\0" * 8


def _lamp():
    return struct.pack("<II", 0, 0)


def _enso():
    return bytes(64) + b"\0" * 16


def _swg():
    return b"SWG\0" + struct.pack("<I", 0) + bytes(0x48) + struct.pack("<HH", 640, 480) + bytes(64)


def _tim2(w=48, h=64):
    import numpy as np
    idx = np.zeros((h, w), np.uint8)
    idx[4, 3:7] = 1
    pixels = ((idx[:, 1::2] << 4) | idx[:, 0::2]).astype(np.uint8).tobytes()
    clut = bytes(4) + bytes([255, 255, 255, 128]) + bytes(4 * 14)
    head = bytearray(0x50)
    struct.pack_into("<IIIHHBBBBHH", head, 0, 0x50 + len(pixels) + len(clut),
                     len(clut), len(pixels), 0x50, 16, 0, 1, 3, 4, w, h)
    return b"TIM2\x04\x00\x01\x00" + bytes(8) + bytes(head) + pixels + clut


from types import SimpleNamespace as _NS  # noqa: E402


def _plan():
    return _NS(items=[_NS(sid="a1", source_label="A", stars=[1, 2, 3, -1], genre=0,
                          play=[1], preview=5)],
               skipped_dupes={"a2": ["B"]}, skipped_titles={"a3": ("A", "t", "a1")},
               genre_fixes={"x": 1}, star_fixes={"x": [1, 2, 3, 4]},
               play_fixes={"x": [1]}, preview_fixes={"x": 9},
               titles={"a1": "Song"}, play_estimated=set())


def _factories():
    """name -> zero-arg callable returning a built dialog."""
    from taiko2x6.app import drop_router, guide, jobs, settings_dialog
    from taiko2x6.audio import audioplayer, vagtool
    from taiko2x6.bineditors import (bineditor_enso_parts, bineditor_fname,
                                     bineditor_hdbdinfo, bineditor_lamp,
                                     bineditor_musicinfo, bineditor_rank,
                                     bineditor_streaminfo, bineditor_tuning)
    from taiko2x6.charts import chart_convert, sht_validator
    from taiko2x6.exe import taiko_exe, usb_keyboard
    from taiko2x6.graphics import flipbook_player, songtex, songtex_all, swg_editor
    from taiko2x6.ps2 import (apa_merge, backups_undo, hdd_browser, hdd_song_wizard, img_slim,
                              iso_packer, pfsshell_tool, ps2mc_card)
    from taiko2x6.songs import (generators, omnimix_gui, song_builder,
                                song_manager, song_replacer)

    def sound_bank():
        from taiko2x6.audio import hdbd
        orig = hdbd.list_bank
        hdbd.list_bank = lambda hd, bd: []
        try:
            return audioplayer.SoundBankDialog(b"", b"", "bank")
        finally:
            hdbd.list_bank = orig

    def flipbook():
        pix = QPixmap(8, 8)
        pix.fill(Qt.white)
        return flipbook_player.FlipbookPlayer([("a", pix)], "t")

    tmp = Path(os.environ.get("TEMP", ".")) / "_dlg_consistency"
    tmp.mkdir(exist_ok=True)
    for n in ("a.iso", "DATA.000", "LIST.BIN"):
        (tmp / n).write_bytes(b"x" * 64)
    ed = lambda mod, data: (lambda: mod.Editor(data, "t"))
    return {
        "CrashDialog": lambda: jobs._dialog_class()("boom", "trace"),
        "SettingsDialog": settings_dialog.SettingsDialog,
        "SoundBankDialog": sound_bank,
        "AudioImportDialog": lambda: vagtool.AudioImportDialog(),
        "EnsoPartsEditor": ed(bineditor_enso_parts, _enso()),
        "FnameEditor": ed(bineditor_fname, _fname()),
        "HdbdinfoEditor": ed(bineditor_hdbdinfo, _hdbdinfo()),
        "LampEditor": ed(bineditor_lamp, _lamp()),
        "MusicinfoEditor": ed(bineditor_musicinfo, _musicinfo()),
        "RankEditor": ed(bineditor_rank, _rank()),
        "StreaminfoEditor": ed(bineditor_streaminfo, _streaminfo()),
        "TuningEditor": ed(bineditor_tuning, _tuning()),
        "ChartConverterDialog": chart_convert.ChartConverterDialog,
        "ShtValidatorDialog": sht_validator.ShtValidatorDialog,
        "TaikoExeDialog": taiko_exe.TaikoExeDialog,
        "TaikoTimerDialog": taiko_exe.TaikoTimerDialog,
        "TaikoBootDialog": taiko_exe.TaikoBootDialog,
        "UsbKeyboardDialog": usb_keyboard.UsbKeyboardDialog,
        "FlipbookPlayer": flipbook,
        "SongTexDialog": lambda: songtex.SongTexDialog(_tim2(), "t"),
        "SongTextureDialog": songtex_all.SongTextureDialog,
        "SwgEditor": lambda: swg_editor.SwgEditor(_swg(), [], "t"),
        "ApaMergeDialog": apa_merge.ApaMergeDialog,
        "HddBrowserDialog": hdd_browser.HddBrowserDialog,
        "HddSongWizard": hdd_song_wizard.HddSongWizard,
        "ImgSlimDialog": img_slim.ImgSlimDialog,
        "BackupsUndoDialog": backups_undo.BackupsUndoDialog,
        "PackIsoDialog": lambda: iso_packer.PackIsoDialog(
            tmp / "DATA.000", tmp / "LIST.BIN", str(tmp / "a.iso")),
        "PfsShellDialog": pfsshell_tool.PfsShellDialog,
        "Ps2mcDialog": ps2mc_card.Ps2mcDialog,
        "VagGeneratorDialog": generators.VagGeneratorDialog,
        "OmnimixDialog": omnimix_gui.OmnimixDialog,
        "PlanDetailsDialog": lambda: omnimix_gui.PlanDetailsDialog(_plan(), [_NS(label="A")]),
        "StarsDialog": lambda: song_builder.StarsDialog("sid", [1, 2, 3, 4]),
        "SongBuilderDialog": lambda: song_builder.SongBuilderDialog(object()),
        "SongManager": lambda: song_manager.SongManager(_musicinfo(), _tuning(), _streaminfo()),
        "SongReplacerDialog": lambda: song_replacer.SongReplacerDialog(object()),
        "GuideDialog": lambda: guide.GuideDialog(),
        "OpenWithDialog": lambda: drop_router.OpenWithDialog(
            "a.img", drop_router.candidates("hdd")),
    }


NAME_LIST = ['CrashDialog', 'SettingsDialog', 'SoundBankDialog', 'AudioImportDialog', 'EnsoPartsEditor', 'FnameEditor', 'HdbdinfoEditor', 'LampEditor', 'MusicinfoEditor', 'RankEditor', 'StreaminfoEditor', 'TuningEditor', 'ChartConverterDialog', 'ShtValidatorDialog', 'TaikoExeDialog', 'TaikoTimerDialog', 'TaikoBootDialog', 'UsbKeyboardDialog', 'FlipbookPlayer', 'SongTexDialog', 'SongTextureDialog', 'SwgEditor', 'ApaMergeDialog', 'HddBrowserDialog', 'HddSongWizard', 'ImgSlimDialog', 'PackIsoDialog', 'PfsShellDialog', 'Ps2mcDialog', 'VagGeneratorDialog', 'OmnimixDialog', 'PlanDetailsDialog', 'StarsDialog', 'SongBuilderDialog', 'SongManager', 'SongReplacerDialog', 'GuideDialog', 'OpenWithDialog', 'BackupsUndoDialog']


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    app = QApplication.instance() or QApplication([])
    from taiko2x6.songs import song_builder
    monkeypatch.setattr(song_builder, "song_ids", lambda a: ["alpha", "beta"])
    from taiko2x6.app import theme
    f = app.font()
    app.setStyleSheet(theme._QSS)        # the real app's button padding
    yield
    app.setStyleSheet("")
    app.setFont(f)


def _build(name, lang="en", scale=1.0):
    app = QApplication.instance()
    i18n._lang = lang
    if scale != 1.0:
        f = app.font()
        f.setPointSizeF(f.pointSizeF() * scale)
        app.setFont(f)
    return _factories()[name]()


def _close(d):
    d.close()
    d.deleteLater()


def _names():
    return NAME_LIST


@pytest.mark.parametrize("name", _names())
def test_title_translated(name):
    en = _build(name, "en")
    t_en = en.windowTitle()
    _close(en)
    zh = _build(name, "zh-TW")
    t_zh = zh.windowTitle()
    _close(zh)
    assert t_en, "no window title"
    assert t_zh != t_en, "title not translated: %r" % t_en


@pytest.mark.parametrize("name", _names())
def test_default_button(name):
    d = _build(name)
    try:
        has = any(isinstance(b, QPushButton) and b.isDefault()
                  for b in d.findChildren(QPushButton)) \
            or bool(d.findChildren(QDialogButtonBox))
        reason = getattr(d, "NO_DEFAULT_REASON", "")
        assert has or reason, "no default button and no stated reason"
    finally:
        _close(d)


def _scroll_content(w):
    p = w.parentWidget()
    while p is not None:
        if isinstance(p, QAbstractScrollArea):
            return True
        p = p.parentWidget()
    return False


@pytest.mark.parametrize("scale", [1.0, 1.5])
@pytest.mark.parametrize("lang", ["en", "zh-TW"])
@pytest.mark.parametrize("name", _names())
def test_size_and_no_overflow(name, lang, scale):
    d = _build(name, lang, scale)
    try:
        msz = d.minimumSizeHint().expandedTo(d.minimumSize())
        # The ceiling is for the normal font. At 1.5x the fonts (not the
        # logical screen) grow, so a bigger minimum is expected there; only
        # clipping is checked.
        if scale == 1.0:
            assert msz.width() <= MAX_W and msz.height() <= MAX_H, \
                "minimum size %dx%d" % (msz.width(), msz.height())
        d.resize(d.size().expandedTo(msz))
        d.show()
        QApplication.processEvents()
        bad = []
        for w in d.findChildren(QAbstractButton) + d.findChildren(QLabel):
            if not w.isVisibleTo(d) or _scroll_content(w):
                continue
            if isinstance(w, QLabel) and (w.wordWrap() or (w.pixmap() is not None and not w.pixmap().isNull())
                                          or not w.text()):
                continue
            r = w.rect()
            tl = w.mapTo(d, r.topLeft())
            if tl.x() + r.width() > d.width() + 1 or tl.y() + r.height() > d.height() + 1:
                bad.append((type(w).__name__, w.text()[:30], "outside window"))
            elif w.width() < w.minimumSizeHint().width() - 1:
                bad.append((type(w).__name__, w.text()[:30],
                            "%d < %d" % (w.width(), w.minimumSizeHint().width())))
        assert not bad, bad
    finally:
        _close(d)

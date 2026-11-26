"""Replace a texture from a PNG/JPG: fit modes, format gate, dialog, staging."""
import struct

import numpy as np
import pytest
from PIL import Image

pytest.importorskip("PySide6")

from conftest import build_archive  # noqa: E402
from taiko2x6.archive.archive_model import Archive  # noqa: E402
from taiko2x6.graphics import image_import, tim2  # noqa: E402


@pytest.fixture(autouse=True)
def _english(monkeypatch):
    from taiko2x6.app import i18n
    monkeypatch.setattr(i18n, "_lang", "en")


def make_nut(w, h, image_type=4, colors=16):
    bits = 4 if image_type == 4 else 8
    img = bytes(w * h * bits // 8)
    clut = bytes(colors * 4)
    head = b"TIM2" + bytes([4, 0]) + struct.pack("<H", 1) + bytes(8)
    pic = bytearray(0x30)
    struct.pack_into("<III", pic, 0, 0x30 + len(img) + len(clut), len(clut), len(img))
    struct.pack_into("<HH", pic, 12, 0x30, colors)
    pic[0x12] = 3
    pic[0x13] = image_type
    struct.pack_into("<HH", pic, 0x14, w, h)
    return head + bytes(pic) + img + clut


def test_fit_modes_geometry():
    src = np.zeros((20, 40, 4), np.uint8)
    src[..., 0] = 255
    src[..., 3] = 255                           # opaque red, 2:1
    s = image_import.fit_rgba(src, 16, 16, "stretch")
    assert s.shape == (16, 16, 4) and (s[..., 3] == 255).all()
    f = image_import.fit_rgba(src, 16, 16, "fit")
    assert f[0, 8, 3] == 0 and f[15, 8, 3] == 0 and f[8, 8, 3] == 255
    c = image_import.fit_rgba(src, 16, 16, "crop")
    assert (c[..., 3] == 255).all()
    with pytest.raises(ValueError):
        image_import.fit_rgba(src, 16, 16, "bogus")


def test_target_formats():
    t = image_import.texture_target(make_nut(32, 16))
    assert t["ok"] and (t["width"], t["height"], t["bits"], t["colors"]) == (32, 16, 4, 16)
    t8 = image_import.texture_target(make_nut(16, 16, 5, 256))
    assert t8["ok"] and t8["bits"] == 8 and t8["colors"] == 256
    t9 = image_import.texture_target(make_nut(16, 16, 9, 0))
    assert not t9["ok"] and "type 9" in t9["reason"]
    assert not image_import.texture_target(b"garbage")["ok"]
    with pytest.raises(ValueError):
        image_import.convert(make_nut(16, 16, 9, 0), np.zeros((16, 16, 4), np.uint8),
                             "stretch", False)


def test_convert_round_trip_few_colours():
    src = np.zeros((16, 32, 4), np.uint8)
    src[:8, :16] = (255, 0, 0, 255)
    src[8:, 16:] = (0, 0, 255, 255)
    data, shown = image_import.convert(make_nut(32, 16), src, "stretch", False)
    assert shown.shape == (16, 32, 4)
    assert tuple(shown[0, 0][:3]) == (255, 0, 0) and tuple(shown[15, 31][:3]) == (0, 0, 255)
    assert shown[0, 31, 3] == 0                  # transparent stays transparent
    assert len(data) == len(make_nut(32, 16))    # layout untouched


@pytest.fixture
def qapp(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from taiko2x6.app import appconfig
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    return QApplication.instance() or QApplication([])


def test_dialog_ok_gated_and_result(qapp, tmp_path):
    png = tmp_path / "a.png"
    Image.new("RGBA", (64, 32), (10, 200, 30, 255)).save(png)
    d = image_import.ImageReplaceDialog(make_nut(32, 16), "x.nut")
    try:
        assert not d.b_ok.isEnabled() and d.result_bytes is None
        d.set_path(str(png))
        assert d.b_ok.isEnabled() and d.result_bytes is not None
        assert "16 x 32" not in d.lbl_status.text() and "32 x 16" in d.lbl_status.text()
        d.cb_fit.setCurrentIndex(1)
        d.chk_dither.setChecked(True)
        assert d.b_ok.isEnabled()
        d.set_path(str(tmp_path / "missing.png"))
        assert not d.b_ok.isEnabled() and d.result_bytes is None
    finally:
        d.close()
        d.deleteLater()


def test_dialog_accepts_8bit(qapp, tmp_path):
    png = tmp_path / "a.png"
    src = np.zeros((16, 16, 4), np.uint8)
    for i in range(16):
        src[i, :] = (i * 15, 255 - i * 10, i, 255)
    Image.fromarray(src, "RGBA").save(png)
    nut = make_nut(16, 16, 5, 256)
    d = image_import.ImageReplaceDialog(nut, "x.nut", path=str(png))
    try:
        assert d.b_ok.isEnabled() and d.result_bytes is not None
        assert "8-bit" in d.lbl_target.text()
        staged = d.result_bytes
        assert staged[:0x40] == nut[:0x40] and len(staged) == len(nut)
        assert (tim2.decode_tim2(staged)[0][2] == src).all()
    finally:
        d.close()
        d.deleteLater()


def test_explorer_stages_replacement(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from taiko2x6.app import taiko256_explorer_gui6 as g
    nut = make_nut(32, 16)
    lb, db = build_archive([("tex.grp", [("a.nut", nut), ("b.bin", b"B" * 50)])])
    lp, dp = tmp_path / "list.bin", tmp_path / "DATA.000"
    lp.write_bytes(lb)
    dp.write_bytes(db)
    png = tmp_path / "pic.png"
    Image.new("RGBA", (32, 16), (200, 20, 20, 255)).save(png)

    orig_init = image_import.ImageReplaceDialog.__init__

    def init(self, template, name="", parent=None, path=None):
        orig_init(self, template, name, parent, path=str(png))
    monkeypatch.setattr(image_import.ImageReplaceDialog, "__init__", init)
    monkeypatch.setattr(image_import.ImageReplaceDialog, "exec", lambda self: 1)
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: 0))

    w = g.MainWindow()
    try:
        w._load(lp, dp)
        item = w._find_file_item(0, 0)
        assert item is not None
        before = bytes(nut)
        w._replace_image(item)
        assert (0, 0) in w.archive.replacements and w.archive.dirty
        staged = w.archive.replacements[(0, 0)]
        assert staged != before and tim2.is_tim2(staged)
        assert staged[:0x30] == before[:0x30]            # header kept verbatim
        # the Image tab button follows the selection
        w.preview.show_file("a.nut", "tex.grp", w.archive.layout.files[0], staged, True)
        assert w.preview.btn_replace_img.isEnabled()
        w.preview.show_file("b.bin", "tex.grp", w.archive.layout.files[1], b"B" * 50, False)
        assert not w.preview.btn_replace_img.isEnabled()
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        w.discard_edits()
        assert not w.archive.dirty
    finally:
        w.close()
        w.deleteLater()

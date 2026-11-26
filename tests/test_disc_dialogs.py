"""Disc / image utility dialogs: construct, validate inline, primary button
state. Synthetic inputs only; nothing here touches a real image."""
import os

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from taiko2x6.app import appconfig, i18n  # noqa: E402
from taiko2x6.ps2 import (apa_merge, dialog_kit, img_slim, iso_packer,  # noqa: E402
                          pfsshell_tool)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    monkeypatch.setattr(i18n, "_lang", "en")
    QApplication.instance() or QApplication([])


def _files(tmp_path):
    iso = tmp_path / "game.iso"
    data = tmp_path / "DATA.000"
    lst = tmp_path / "LIST.BIN"
    for p in (iso, data, lst):
        p.write_bytes(b"x" * 4096)
    return iso, data, lst


def test_pack_problem(tmp_path):
    iso, data, lst = _files(tmp_path)
    out = tmp_path / "out.iso"
    P = iso_packer.pack_problem
    assert P(str(iso), str(data), str(lst), str(out)) == ""
    assert P("", str(data), str(lst), str(out))
    assert P(str(tmp_path / "nope.iso"), str(data), str(lst), str(out))
    assert P(str(iso), str(data), str(lst), "")
    assert P(str(iso), str(data), str(lst), str(iso))        # output == input
    assert P(str(iso), str(data), str(lst), str(tmp_path / "no" / "x.iso"))
    assert P(str(iso), str(data), str(lst), str(tmp_path))   # a folder


def test_pack_dialog_button_follows_validity(tmp_path):
    iso, data, lst = _files(tmp_path)
    d = iso_packer.PackIsoDialog(data, lst, str(iso))
    try:
        assert d.row_out.text().endswith(".patched.iso")
        assert d.b_go.isEnabled() and not d.lbl_problem.text()
        d.row_out.setText("")
        assert not d.b_go.isEnabled() and d.lbl_problem.text()
        d.row_out.setText(str(tmp_path / "o.iso"))
        assert d.b_go.isEnabled()
        d.row_data.setText(str(tmp_path / "missing"))
        assert not d.b_go.isEnabled()
    finally:
        d.close()
        d.deleteLater()


def test_slim_dialog(tmp_path):
    d = img_slim.ImgSlimDialog(None, default_img="")
    try:
        assert not d.btn_scan.isEnabled() and not d.btn_trunc.isEnabled()
        assert d.lbl_problem.text()
        img = tmp_path / "a.img"
        img.write_bytes(b"\0" * 8192)
        d.row_img.setText(str(img))
        d._path_settled()
        if os.name == "nt":
            assert d.btn_scan.isEnabled() and not d.lbl_problem.text()
        assert not d.btn_trunc.isEnabled()      # needs an analysis first
        d.row_img.setText(str(tmp_path / "gone.img"))
        assert not d.btn_scan.isEnabled()
    finally:
        d.close()
        d.deleteLater()


def test_merge_problem_and_dialog(tmp_path, pfs_image):
    img = str(pfs_image[0])
    assert apa_merge.merge_problem([], "")
    assert apa_merge.merge_problem([str(tmp_path / "none.img")], "x")
    d = apa_merge.ApaMergeDialog()
    try:
        assert not d.btn_merge.isEnabled()
        d.add_sources([img])
        d.row_out.setText(str(tmp_path / "merged.img"))
        # whether the synthetic image is mergeable or not, the dialog must
        # agree with merge_problem and stay alive
        prob = apa_merge.merge_problem(d._sources, d.row_out.text())
        assert d.btn_merge.isEnabled() == (prob == "")
        d.row_out.setText(img)                      # output may not be a source
        assert not d.btn_merge.isEnabled()
        d.row_out.setText("")
        assert not d.btn_merge.isEnabled()
    finally:
        d.close()
        d.deleteLater()


def test_pfsshell_dialog_constructs():
    assert pfsshell_tool.open_problem("")
    d = pfsshell_tool.PfsShellDialog(None)
    try:
        assert not d.b_open.isEnabled()
        assert not d.b_extract.isEnabled() and not d.b_replace.isEnabled()
        d.row_img.setText(r"Z:\definitely\missing.img")
        assert not d.b_open.isEnabled()
    finally:
        d.close()
        d.deleteLater()


def test_log_autoscrolls():
    log = dialog_kit.make_log()
    log.resize(200, 60)
    log.show()
    for i in range(200):
        dialog_kit.log_line(log, "line %d" % i)
    sb = log.verticalScrollBar()
    assert sb.value() == sb.maximum() > 0
    log.close()
    log.deleteLater()

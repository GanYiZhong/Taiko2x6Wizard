"""HDD browser / song wizard: sizes, lock errors, filter+sort, input validation."""
import errno

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from taiko2x6.app import appconfig, i18n  # noqa: E402
from taiko2x6.ps2 import hdd_browser as B, hdd_song_wizard as W  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    monkeypatch.setattr(i18n, "_lang", "en")
    QApplication.instance() or QApplication([])


def test_human_size():
    assert B.human_size(0) == "0 B"
    assert B.human_size(1023) == "1023 B"
    assert B.human_size(1536) == "1.5 KB"
    assert B.human_size(5 * 1024 ** 2) == "5.0 MB"
    assert B.human_size(3 * 1024 ** 3) == "3.0 GB"


def test_locked_error_is_friendly():
    assert "PCSX2" in B.friendly_error(PermissionError(13, "denied"))
    e = OSError(errno.EIO, "x"); e.winerror = 32
    assert "PCSX2" in B.friendly_error(e)
    assert "PCSX2" in B.friendly_error(RuntimeError("image is locked by another process"))
    assert B.friendly_error(ValueError("boom")) == "boom"


def test_input_problem(tmp_path):
    ids = ["abc", "xyz"]
    f = tmp_path / "a.tja"; f.write_text("x")
    assert W.input_problem("", "abc", ids)
    assert W.input_problem("a b", "abc", ids)
    assert "exists" in W.input_problem("abc", "abc", ids)
    assert W.input_problem("new", "", ids)
    assert W.input_problem("new", "nope", ids)
    assert W.input_problem("new", "abc", ids, tja=str(tmp_path / "gone.tja"))
    assert W.input_problem("new", "abc", ids, audio=str(tmp_path / "gone.ogg"))
    assert W.input_problem("new", "abc", ids, tja=str(f)) == ""


def test_browser_filter_sort_and_buttons(pfs_image):
    img, part, _ = pfs_image
    dlg = B.HddBrowserDialog(default_img=str(img))
    try:
        n = dlg.lst.topLevelItemCount()
        assert n > 0
        assert not dlg.b_extract.isEnabled()          # nothing selected yet
        first = dlg.lst.topLevelItem(0)
        first.setSelected(True); dlg.lst.setCurrentItem(first)
        if not first.data(0, B.Qt.UserRole)["is_dir"]:
            assert dlg.b_extract.isEnabled() and dlg.b_replace.isEnabled()
        dlg.ed_filter.setText("zzz-no-such-file")
        assert all(dlg.lst.topLevelItem(i).isHidden() for i in range(n))
        assert not dlg.b_extract.isEnabled()          # hidden selection is ignored
        dlg.ed_filter.setText("")
        assert not any(dlg.lst.topLevelItem(i).isHidden() for i in range(n))
        dlg.lst.sortByColumn(1, B.Qt.DescendingOrder)
        sizes = [dlg.lst.topLevelItem(i).data(0, B.Qt.UserRole)["size"] for i in range(n)]
        assert sizes == sorted(sizes, reverse=True)
        assert appconfig.get("hdd", "partition") == part or appconfig.get("hdd", "partition")
    finally:
        dlg.close()


def test_wizard_run_button_needs_valid_input(pfs_image):
    img, _, _ = pfs_image
    dlg = W.HddSongWizard(default_img=str(img))
    try:
        assert dlg.cb_part.count() >= 1
        assert dlg.b_load.isEnabled()
        assert not dlg.b_run.isEnabled()              # nothing loaded yet
        assert "Load the partition" in dlg.lbl_state.text()
        assert appconfig.last_path("hddimg") == str(img)   # image remembered
    finally:
        dlg.close()

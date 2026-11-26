"""Staged-edit workflow in the explorer: stage, list, discard one, drop, save."""
import pytest

pytest.importorskip("PySide6")

from taiko2x6.archive.archive_model import Archive


def test_unstage(archive_pair):
    a = Archive(*archive_pair)
    try:
        a.stage_replace(0, 0, b"x")
        assert a.dirty
        assert a.unstage(0, 0) is True
        assert a.unstage(0, 0) is False
        assert not a.dirty
    finally:
        a.close()


@pytest.fixture
def win(archive_pair, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox
    from taiko2x6.app import appconfig
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    QApplication.instance() or QApplication([])
    from taiko2x6.app import taiko256_explorer_gui6 as g
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: 0))
    w = g.MainWindow()
    lp, dp = archive_pair
    w._load(lp, dp)
    yield w
    w.close()
    w.deleteLater()


def _first_file(w):
    from taiko2x6.app import taiko256_explorer_gui6 as g
    return w._find_file_item(0, 0), g


def test_drop_stage_list_discard_save(win, tmp_path):
    w = win
    item, g = _first_file(w)
    assert item is not None
    assert not w.pending_box.isVisibleTo(w)
    src = tmp_path / "new.bin"
    src.write_bytes(b"Z" * 77)
    idx = w.proxy.mapFromSource(item.index())
    w._on_file_dropped(idx, str(src))
    assert w.archive.replacements[(0, 0)] == b"Z" * 77
    assert w.pending_list.count() == 1
    assert "77" in w.pending_list.item(0).text()
    assert w.pending_box.isVisibleTo(w)

    w._unstage_file(0, 0)
    assert not w.archive.dirty
    assert w.pending_list.count() == 0

    # stage again, then save and keep the selection
    w._on_file_dropped(idx, str(src))
    w.tree.setCurrentIndex(idx)
    w.save_archive()
    assert not w.archive.dirty
    cur = w._current_file_item()
    assert cur is not None and cur.data(g.ROLE_FILE) == 0
    assert "Wrote" in w.statusBar().currentMessage() or "已寫入" in w.statusBar().currentMessage()
    g0 = w.archive.layout.groups[0]
    e0 = w.archive.layout.files[0]
    assert w.archive.read_file(g0, e0) == b"Z" * 77


def test_drop_on_folder_does_nothing(win, tmp_path):
    src = tmp_path / "n.bin"
    src.write_bytes(b"1")
    win._on_file_dropped(win.proxy.index(0, 0), str(src))
    assert not win.archive.dirty

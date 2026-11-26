"""Main window constructs offscreen; filter, recent list and dirty label work."""
import pytest

pytest.importorskip("PySide6")


@pytest.fixture
def win(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from taiko2x6.app import appconfig
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    QApplication.instance() or QApplication([])
    from taiko2x6.app import taiko256_explorer_gui6 as g
    w = g.MainWindow()
    yield w
    w.close()


def test_constructs_with_shortcuts(win):
    assert win.act_find.shortcut().toString()
    assert win.act_extract_all.shortcut().toString() == "Ctrl+E"
    assert win.dirty_label.text() == ""
    assert not win.recent_menu.isEnabled()


def test_recent_folders(win, tmp_path):
    d = tmp_path / "arc"
    d.mkdir()
    win._remember_folder(d)
    win._remember_folder(d)
    assert win._recent_folders() == [str(d)]
    assert win.recent_menu.isEnabled()
    assert len(win.recent_menu.actions()) == 1

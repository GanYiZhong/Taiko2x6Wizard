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


@pytest.mark.parametrize("lang", ["en", "zh-TW"])
def test_toolbar_no_overflow_and_start_full_width(tmp_path, monkeypatch, lang):
    from PySide6.QtWidgets import QApplication, QToolBar
    from taiko2x6.app import appconfig, i18n
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    QApplication.instance() or QApplication([])
    old = i18n.lang()
    i18n.set_lang(lang)
    try:
        from taiko2x6.app import taiko256_explorer_gui6 as g
        w = g.MainWindow()
        try:
            w.resize(1280, 800)
            w.show()
            for _ in range(3):
                QApplication.processEvents()
            tb = w.findChild(QToolBar, "main_toolbar")
            ext = tb.findChild(object, "qt_toolbar_ext_button")
            assert ext is None or not ext.isVisible()
            assert w.central_stack.currentIndex() == 0
            assert w.start_page.width() >= 0.9 * w.centralWidget().width()
        finally:
            w.close()
    finally:
        i18n.set_lang(old)


def test_explorer_shown_after_open(win, archive_pair):
    win.open_folder(archive_pair[0].parent)
    assert win.central_stack.currentIndex() == 1
    assert win.central_stack.currentWidget() is win.split

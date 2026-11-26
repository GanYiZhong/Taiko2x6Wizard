"""Help > Quick guide: constructs, every page's tool button maps to a real
MainWindow action, every page is translated, and the "?" help buttons exist."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton, QToolButton  # noqa: E402

from taiko2x6.app import appconfig, i18n  # noqa: E402


@pytest.fixture
def win(tmp_path, monkeypatch):
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    QApplication.instance() or QApplication([])
    from taiko2x6.app import taiko256_explorer_gui6 as g
    w = g.MainWindow()
    yield w
    w.close()
    w.deleteLater()


def test_every_page_action_exists(win):
    from PySide6.QtGui import QAction
    from taiko2x6.app import guide
    names = guide.page_actions()
    assert names
    for n in names:
        assert isinstance(getattr(win, n, None), QAction), n


def test_guide_opens_from_help_menu_and_buttons_trigger(win):
    from taiko2x6.app import guide
    assert win.act_guide.shortcut().toString()
    win.open_guide()
    dlg = win._guide
    assert isinstance(dlg, guide.GuideDialog)
    assert win.open_guide() is None and win._guide is dlg      # reused
    assert dlg.listing.count() == len(guide._pages()) >= 7
    for row in range(dlg.listing.count()):
        dlg.select(row)
        assert dlg.text.toPlainText().strip()
        assert dlg._buttons, "page %d has no tool button" % row
        assert all(act is not None for _, act in dlg._buttons)
    dlg.close()
    dlg.deleteLater()


def test_button_runs_action(win):
    from taiko2x6.app import guide
    dlg = guide.GuideDialog(win)
    hit = []
    win.act_omnimix.triggered.disconnect()
    win.act_omnimix.triggered.connect(lambda: hit.append(1))
    row = next(i for i, p in enumerate(guide._pages())
               if p["actions"][0][0] == "act_omnimix")
    dlg.select(row)
    btn = dlg._buttons[0][0]
    assert btn.isEnabled()
    btn.click()
    assert hit == [1]
    dlg.close()
    dlg.deleteLater()


def test_disabled_action_disables_button(win):
    from taiko2x6.app import guide
    dlg = guide.GuideDialog(win)
    dlg.select(1)                       # songs page: needs an open archive
    by_act = {id(a): b for b, a in dlg._buttons}
    assert not win.act_songbuild.isEnabled()
    assert not by_act[id(win.act_songbuild)].isEnabled()
    dlg.close()
    dlg.deleteLater()


def test_pages_translated():
    from taiko2x6.app import guide
    i18n._lang = "zh-TW"
    try:
        zh = guide._pages()
    finally:
        i18n._lang = "en"
    en = guide._pages()
    assert len(zh) == len(en)
    for a, b in zip(en, zh):
        assert a["title"] != b["title"]
        assert a["intro"] != b["intro"]
        for key in ("needs", "steps", "wrong", "undo"):
            assert all(x != y for x, y in zip(a[key], b[key])), (a["title"], key)


def test_help_buttons_present():
    QApplication.instance() or QApplication([])
    from taiko2x6.exe import taiko_exe
    from taiko2x6.songs import omnimix_gui
    for cls in (omnimix_gui.OmnimixDialog, taiko_exe.TaikoBootDialog):
        d = cls()
        try:
            helps = [b for b in d.findChildren(QToolButton) if b.text() == "?"]
            assert helps, cls.__name__
            assert all(b.whatsThis() for b in helps)
        finally:
            d.close()
            d.deleteLater()

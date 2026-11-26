"""Icon set: every SVG renders in both themes; toolbar/cards use them; packaged."""
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    from taiko2x6.app import appconfig
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    from taiko2x6.app import taiko256_explorer_gui6 as g
    monkeypatch.setattr(g.MainWindow, "_recent_entries", lambda self: [])
    w = g.MainWindow()
    yield w
    w.close()
    w.deleteLater()


def test_required_icons_exist():
    from taiko2x6.app import icons
    need = {"archive", "open", "save", "extract", "replace", "discard",
            "song_list", "song_add", "song_replace", "omnimix", "chart",
            "patch", "timer", "keyboard", "hdd", "memcard", "slim", "iso",
            "settings", "help", "log", "app"}
    assert need <= set(icons.names())


@pytest.mark.parametrize("dark", [False, True])
def test_every_icon_renders_nonempty(app, dark):
    from taiko2x6.app import icons, theme
    pal = theme.make_palette(dark)
    for name in icons.names():
        ic = icons.icon(name, pal)
        assert not ic.isNull()
        for size in (16, 24, 32):
            img = ic.pixmap(size, size).toImage()
            assert img.width() >= size
            opaque = [img.pixelColor(x, y) for x in range(img.width())
                      for y in range(img.height()) if img.pixelColor(x, y).alpha() > 0]
            assert opaque, f"{name} blank at {size}px"
            want = pal.color(pal.ColorGroup.Active, pal.ColorRole.WindowText)
            assert abs(opaque[0].lightness() - want.lightness()) < 40


def test_toolbar_actions_have_icons_and_groups(win):
    from PySide6.QtWidgets import QToolBar
    tb = win.findChild(QToolBar, "main_toolbar")
    acts = tb.actions()
    assert sum(a.isSeparator() for a in acts) == 3
    for a in acts:
        if not a.isSeparator():
            assert not a.icon().isNull(), a.text()
    assert "Ctrl+S" in win.act_save.toolTip().replace("Strg", "Ctrl")
    assert not win.windowIcon().isNull()
    for a in (win.act_settings, win.act_guide, win.act_open_log):
        assert not a.icon().isNull()


def test_start_cards_have_icons_and_hdd_ones_differ(win):
    names = [c.icon_name for c in win.start_page.cards]
    assert all(names)
    assert len(set(names)) >= 10
    for c in win.start_page.cards:
        assert c.icon_label.pixmap() is not None and not c.icon_label.pixmap().isNull()


def test_spec_packages_icons():
    spec = (ROOT / "Taiko2x6Wizard.spec").read_text(encoding="utf-8")
    assert "taiko2x6/app/icons/*.svg" in spec
    assert list((ROOT / "taiko2x6" / "app" / "icons").glob("*.svg"))

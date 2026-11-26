"""Start page launcher: cards, recent list, responsive reflow (Qt offscreen)."""
import ast
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def win(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from taiko2x6.app import appconfig
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    QApplication.instance() or QApplication([])
    from taiko2x6.app import taiko256_explorer_gui6 as g
    monkeypatch.setattr(g.MainWindow, "_recent_entries", lambda self: [
        ("folder", p) for p in self._recent_folders()])
    w = g.MainWindow()
    yield w
    w.close()
    w.deleteLater()


def test_constructs_with_cards(win):
    page = win.start_page
    assert len(page.cards) >= 10
    assert all(c.focusPolicy() != 0 for c in page.cards)
    assert win.btn_setup is page.btn_setup and win.setup_box is page.setup_box


def test_each_card_triggers_its_slot(win, monkeypatch):
    names = ["open_archive_dialog", "open_pack_iso", "open_song_manager",
             "open_song_builder", "open_song_replacer", "open_omnimix",
             "open_taiko_boot", "open_taiko_exe", "open_taiko_timer",
             "open_usb_keyboard", "open_pfsshell", "open_ps2mc", "open_img_slim", "open_backups"]
    called = []
    for n in names:
        monkeypatch.setattr(win, n, lambda n=n: called.append(n))
    assert len(win.start_page.cards) == len(names)
    for card in win.start_page.cards:
        card.click()
    assert called == names


def test_enter_key_activates_card(win, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent
    called = []
    monkeypatch.setattr(win, "open_archive_dialog", lambda: called.append(1))
    card = win.start_page.cards[0]
    card.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    assert called == [1]


def test_recent_shows_and_greys_missing(win, tmp_path):
    ok = tmp_path / "arc"
    ok.mkdir()
    gone = tmp_path / "gone"
    win._remember_folder(gone)       # stored but absent
    win._remember_folder(ok)
    lst = win.start_page.recent_list
    assert lst.count() == 2
    kinds = {lst.item(i).toolTip(): lst.item(i).data(0x0100)[2] for i in range(2)}
    assert kinds[str(ok)] is True and kinds[str(gone)] is False
    missing = next(lst.item(i) for i in range(2) if lst.item(i).toolTip() == str(gone))
    assert "missing" in missing.text() or "找不到" in missing.text()
    # activating a missing entry does nothing; activating a real one opens it
    opened = []
    win.open_folder = lambda p: opened.append(p)
    win.start_page._on_recent_activated(missing)
    assert opened == []
    present = next(lst.item(i) for i in range(2) if lst.item(i).toolTip() == str(ok))
    win.start_page._on_recent_activated(present)
    assert opened == [ok]
    # removing the missing one
    lst.setCurrentItem(missing)
    win.start_page.remove_selected_recent()
    assert lst.count() == 1
    assert str(gone) not in win._recent_folders()


def test_recent_hidden_when_empty(win):
    assert win.start_page.recent_list.isHidden()
    assert win.start_page.recent_heading.isHidden()


@pytest.mark.parametrize("width", [800, 1600])
def test_reflow_without_overlap(win, width):
    from PySide6.QtWidgets import QApplication
    win.resize(width, 900)
    win.split.setSizes([width, 0])
    win.show()
    for _ in range(3):
        QApplication.processEvents()
    page = win.start_page
    page.resize(width, 900)
    page.relayout()
    QApplication.processEvents()
    body = page.scroll.widget()
    boxes = []
    for c in page.cards:
        p, sz = c.mapTo(body, c.rect().topLeft()), c.size()
        boxes.append((p.x(), p.y(), p.x() + sz.width(), p.y() + sz.height()))
    for i, a in enumerate(boxes):
        assert a[2] <= body.width() + 1
        for b in boxes[i + 1:]:
            overlap = a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
            assert not overlap


def test_wide_has_more_columns_than_narrow(win):
    from PySide6.QtWidgets import QApplication
    win.show()
    page = win.start_page
    page.resize(400, 800)
    QApplication.processEvents()
    page.relayout()
    narrow = max(g._cols for g in page.groups)
    page.resize(1600, 800)
    QApplication.processEvents()
    page.relayout()
    wide = max(g._cols for g in page.groups)
    assert wide > narrow


def test_start_page_strings_translated():
    from taiko2x6.app import i18n
    tree = ast.parse((ROOT / "taiko2x6/app/start_page.py").read_text(encoding="utf-8"))
    keys = {n.args[0].value for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "tr"
            and n.args and isinstance(n.args[0], ast.Constant)}
    assert keys and not [k for k in keys if k not in i18n.ZH_TW]


def test_shorten_path():
    from taiko2x6.app.start_page import shorten_path
    long = "C:\\" + "\\".join(["folder"] * 20) + "\\leaf"
    s = shorten_path(long)
    assert len(s) <= 48 and s.endswith("leaf")
    assert shorten_path("C:\\a") == "C:\\a"

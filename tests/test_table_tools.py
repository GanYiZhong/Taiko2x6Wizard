"""Shared table-editor helpers (taiko2x6/bineditors/table_tools.py) and the
editors that use them, on synthetic data."""
import os
import struct

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem  # noqa: E402

from taiko2x6.app import appconfig  # noqa: E402
from taiko2x6.bineditors import table_tools as TT  # noqa: E402
from taiko2x6.bineditors import bineditor_rank as RK  # noqa: E402
from taiko2x6.bineditors import bineditor_musicinfo as MI  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _ini(tmp_path, monkeypatch):
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")


def _table(rows=3, cols=2):
    t = QTableWidget(rows, cols)
    for r in range(rows):
        for c in range(cols):
            t.setItem(r, c, QTableWidgetItem(str(r * 10 + c)))
    return t


def test_edit_highlight_invalid_and_revert(app):
    t = _table()
    tools = TT.TableTools(t, int_range=(0, 100))
    t.item(0, 0).setText("5")
    assert tools.edited_count() == 1 and tools.invalid_count() == 0
    t.item(1, 1).setText("999")           # out of range
    t.item(2, 0).setText("abc")           # not a number
    assert tools.invalid_count() == 2
    assert t.item(0, 0).toolTip()
    tools.revert()
    assert tools.edited_count() == 0 and tools.invalid_count() == 0
    assert t.item(0, 0).text() == "0"


def test_hex_and_leading_zero_parse():
    assert TT.parse_int("0x10") == 16 and TT.parse_int("010") == 10


def test_filter_and_toolbar(app):
    t = _table(4)
    tools = TT.TableTools(t)
    bar = TT.ToolBar([tools])
    bar.ed.setText("20")
    assert [t.isRowHidden(r) for r in range(4)] == [True, True, False, True]
    bar.ed.setText("")
    assert not any(t.isRowHidden(r) for r in range(4))
    assert not bar.btn.isEnabled()
    t.item(0, 0).setText("7")
    assert bar.btn.isEnabled() and "1" in bar.lbl.text()
    bar.btn.click()
    assert t.item(0, 0).text() == "0"


def test_copy_paste_block(app):
    t = _table(3, 3)
    tools = TT.TableTools(t)
    t.setRangeSelected(__import__("PySide6.QtWidgets", fromlist=["x"]).QTableWidgetSelectionRange(0, 0, 1, 1), True)
    tools.copy()
    assert QApplication.clipboard().text() == "0\t1\n10\t11"
    t.clearSelection()
    t.setCurrentCell(2, 1)
    t.setRangeSelected(__import__("PySide6.QtWidgets", fromlist=["x"]).QTableWidgetSelectionRange(2, 1, 2, 1), True)
    tools.paste()                          # 2x2 block clipped at the table edge
    assert t.item(2, 1).text() == "0" and t.item(2, 2).text() == "1"
    assert tools.edited_count() == 2


def test_paste_skips_read_only(app):
    t = _table(1, 2)
    t.item(0, 1).setFlags(t.item(0, 1).flags() & ~Qt.ItemIsEditable)
    tools = TT.TableTools(t)
    QApplication.clipboard().setText("9\t9")
    t.setCurrentCell(0, 0)
    tools.paste()
    assert t.item(0, 0).text() == "9" and t.item(0, 1).text() == "1"


def test_max_len_flags_long_text(app):
    t = _table(1, 1)
    tools = TT.TableTools(t, int_cols=(), max_len={0: 2})
    t.item(0, 0).setText("abc")
    assert tools.invalid_count() == 1


def test_window_size_remembered(app):
    ed = RK.Editor(_rank_bytes())
    ed.resize(777, 555)
    ed.reject()
    ed2 = RK.Editor(_rank_bytes())
    assert (ed2.width(), ed2.height()) == (777, 555)


def _rank_bytes():
    return struct.pack("<8I", 10, 4, 0, 0, 0, 0, 0, 0) + struct.pack("<44I", *range(44))


def test_rank_editor_roundtrip_and_edit(app):
    data = _rank_bytes()
    ed = RK.Editor(data)
    ed._save()
    assert ed.result_bytes == data
    ed = RK.Editor(data)
    ed.tbl.item(0, 0).setText("0x10")
    ed._save()
    assert RK.parse(ed.result_bytes).rows[0][0] == 16


def _musicinfo_bytes():
    pool = b"SONG_A\0SONG_B\0"
    recs = b""
    for ptr in (0, 7):
        recs += struct.pack("<21i", ptr, 1, 2, *([0] * 18))
    o0 = 0x34 + len(recs)
    hdr = struct.pack("<13I", 2, 0, 0, 0, 0, 0, 0, o0, o0, o0, o0, o0, o0)
    return hdr + recs + pool


def test_musicinfo_editor_labels_rows_with_song_id(app):
    data = _musicinfo_bytes()
    assert MI.serialize(MI.parse(data)) == data
    ed = MI.Editor(data)
    assert ed.tbl0.verticalHeaderItem(1).text().endswith("SONG_B")
    ed.ed_filter = ed.findChild(TT.ToolBar).ed
    ed.ed_filter.setText("song_b")
    assert ed.tbl0.isRowHidden(0) and not ed.tbl0.isRowHidden(1)
    ed._save()
    assert ed.result_bytes == data

"""Explorer tree population: bulk-built model, filter reach, item lookup,
pending-edit styling after a rebuild, and light startup imports."""
import subprocess
import sys

import pytest

pytest.importorskip("PySide6")

from conftest import build_archive  # noqa: E402


@pytest.fixture
def win(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from taiko2x6.app import appconfig
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    QApplication.instance() or QApplication([])
    from taiko2x6.app import taiko256_explorer_gui6 as g
    groups = [(f"song.s{i // 20:02d}.g{i}",
               [(f"f{i}_{k}.bin", bytes([k + 1]) * 16) for k in range(3)]
               + [(f"only{i}.sht", b"x" * 8)])
              for i in range(400)]
    lb, db = build_archive(groups)
    lp, dp = tmp_path / "list.bin", tmp_path / "DATA.000"
    lp.write_bytes(lb)
    dp.write_bytes(db)
    w = g.MainWindow()
    w._load(lp, dp)
    yield w, g
    if w.archive:
        w.archive.close()
    w.close()
    w.deleteLater()


def _leaf_names(w):
    out = []
    stack = [w.proxy.index(r, 0) for r in range(w.proxy.rowCount())]
    while stack:
        ix = stack.pop()
        if w.proxy.rowCount(ix) == 0:
            out.append(ix.data())
        stack.extend(w.proxy.index(r, 0, ix) for r in range(w.proxy.rowCount(ix)))
    return out


def test_tree_built_and_lookup(win):
    w, g = win
    assert w.proxy.sourceModel() is w.model
    assert len(w._file_items) == 1600
    it = w._find_file_item(7, 30)
    assert it is not None and it.text() == "f7_2.bin"
    assert it.data(g.ROLE_GROUP) == 7 and it.data(g.ROLE_FILE) == 7 * 4 + 2
    assert w._find_file_item(9999, 0) is None


def test_filter_finds_inside_collapsed_groups(win):
    w, g = win
    w.filter_box.setText("only123")
    assert _leaf_names(w) == ["only123.sht"]
    w.filter_box.setText("")
    assert len(_leaf_names(w)) == 1600


def test_pending_edit_styling_survives_repopulate(win):
    w, g = win
    it = w._find_file_item(5, 21)
    w._stage_file(it, b"new-bytes")
    assert it.font().bold()
    w._populate_tree()
    again = w._find_file_item(5, 21)
    assert again is not it and again.font().bold()
    assert not w._find_file_item(5, 20).font().bold()
    w._unstage_file(5, 21)
    assert not w._find_file_item(5, 21).font().bold()


def test_startup_does_not_import_numpy_or_audio():
    code = ("import os; os.environ['QT_QPA_PLATFORM']='offscreen'\n"
            "import sys\n"
            "from PySide6.QtWidgets import QApplication\n"
            "from taiko2x6.app import taiko256_explorer_gui6 as g\n"
            "a = QApplication([]); w = g.MainWindow(); w.show(); a.processEvents()\n"
            "bad = [m for m in ('numpy', 'taiko2x6.audio.audioplayer') if m in sys.modules]\n"
            "print('BAD', bad)\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "BAD []" in r.stdout, r.stdout + r.stderr

"""Backups & undo: journal listing, chained rollback, delete, .bak restore, and
the dialog on top. Images are synthetic and live in tmp_path."""
import hashlib
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from taiko2x6 import safeio  # noqa: E402
from taiko2x6.ps2 import backups_undo as B  # noqa: E402


def _sha(p):
    return hashlib.sha1(p.read_bytes()).hexdigest()


def _session(img, edits, grow=0):
    """One 'write session' through safeio's own journal path."""
    j = safeio.UndoJournal(img)
    try:
        with open(img, "r+b") as f:
            for off, data in edits:
                j.save(f, off, len(data))
                f.seek(off)
                f.write(data)
            if grow:
                f.seek(0, 2)
                f.write(b"\xEE" * grow)
    finally:
        j.close()
    return j.path


@pytest.fixture
def image(tmp_path):
    img = tmp_path / "TK14.img"
    img.write_bytes(bytes((i * 7) & 0xFF for i in range(300_000)))
    return img


def _three_sessions(img):
    states = [_sha(img)]
    _session(img, [(100, b"A" * 5000), (200_000, b"B" * 70_000)])
    states.append(_sha(img))
    # overlaps session 1's range: order matters for correct restore
    _session(img, [(2000, b"C" * 9000)], grow=4096)
    states.append(_sha(img))
    _session(img, [(0, b"D" * 100), (250_000, b"E" * 10)])
    states.append(_sha(img))
    return states


def test_find_journals_newest_first_and_summary(image):
    _three_sessions(image)
    es = B.find_journals(image, summarize=True)
    assert len(es) == 3
    assert [e.key for e in es] == sorted((e.key for e in es), reverse=True)
    newest = es[0]
    assert newest.covers["bytes"] > 0 and newest.covers["ranges"][0][0] == 0
    assert newest.covers["original_size"] == 300_000 + 4096
    assert all(not e.used and e.target == image for e in es)
    # reading a listing never changes anything
    assert B.find_journals(image.parent)[0].path == es[0].path


def test_rollback_chain_restores_every_state_exactly(image):
    states = _three_sessions(image)
    es = B.find_journals(image)
    # roll back to before the oldest: original bytes, original length
    B.rollback_chain(es, es[-1])
    assert _sha(image) == states[0]
    assert image.stat().st_size == 300_000


@pytest.mark.parametrize("which,state", [(0, 2), (1, 1)])
def test_rollback_to_middle_journal(image, which, state):
    states = _three_sessions(image)
    es = B.find_journals(image)
    applied = B.rollback_chain(es, es[which])
    assert _sha(image) == states[state]
    assert len(applied) == which + 1


def test_rollback_applies_newest_first_and_marks_used(image, monkeypatch):
    _three_sessions(image)
    es = B.find_journals(image)
    seen = []
    real = safeio.rollback
    monkeypatch.setattr(safeio, "rollback",
                        lambda j, t=None, log=None: (seen.append(j), real(j, t))[1])
    B.rollback_chain(es, es[-1])
    assert seen == [e.path for e in es]                  # newest to oldest
    again = B.find_journals(image)
    assert len(again) == 3 and all(e.used for e in again)
    assert not list(image.parent.glob("*.undo"))
    with pytest.raises(ValueError):                      # cannot apply twice
        B.rollback_chain(again, again[0])


def test_chain_for_skips_used_and_other_images(image, tmp_path):
    _three_sessions(image)
    other = tmp_path / "OTHER.img"
    other.write_bytes(b"\x01" * 70_000)
    _session(other, [(0, b"Z" * 10)])
    es = B.find_journals(tmp_path)
    mine = [e for e in es if e.target == image]
    ch = B.chain_for(es, mine[-1])
    assert len(ch) == 3 and all(e.target == image for e in ch)


def test_rollback_failure_leaves_remaining_journals(image, monkeypatch):
    states = _three_sessions(image)
    es = B.find_journals(image)

    def boom(path, *a, **k):
        raise PermissionError("being used by another process")
    monkeypatch.setattr(B, "check_writable", boom)
    with pytest.raises(PermissionError):
        B.rollback_chain(es, es[-1])
    assert _sha(image) == states[3]                      # untouched
    assert all(not e.used for e in B.find_journals(image))


def test_delete_only_touches_journals(image):
    states = _three_sessions(image)
    (image.parent / "keep.txt").write_text("x")
    es = B.find_journals(image)
    victims = B.older_for_delete(es, es[1])
    assert len(victims) == 2
    assert B.delete_journals(victims) == 2
    left = B.find_journals(image)
    assert len(left) == 1 and left[0].path == es[0].path
    assert _sha(image) == states[3] and (image.parent / "keep.txt").exists()
    # a non-journal path is refused even if handed in
    fake = B.JournalEntry(image, image, "x", 0, 0, False)
    assert B.delete_journals([fake]) == 0 and image.exists()


def _bak_folder(tmp_path):
    d = tmp_path / "arc"
    d.mkdir()
    (d / "DATA.000").write_bytes(b"new-data")
    (d / "list.bin").write_bytes(b"new-list")
    (d / "DATA.000.bak").write_bytes(b"old-data")
    (d / "list.bin.bak").write_bytes(b"old-list")
    (d / "DATA.000.bak.7").write_bytes(b"odd-data")      # unpaired: not offered
    return d


def test_bak_pairs_and_restore_keeps_current(tmp_path):
    d = _bak_folder(tmp_path)
    pairs = B.find_bak_pairs(d)
    assert [p.label for p in pairs] == [".bak"]
    B.restore_pair(pairs[0])
    assert (d / "DATA.000").read_bytes() == b"old-data"
    assert (d / "list.bin").read_bytes() == b"old-list"
    # the pair that was current is now another backup, the original stays
    assert (d / "list.bin.bak").read_bytes() == b"old-list"
    assert (d / "list.bin.bak.1").read_bytes() == b"new-list"
    assert (d / "DATA.000.bak.1").read_bytes() == b"new-data"
    assert [p.n for p in B.find_bak_pairs(d)] == [1, 0] or \
        [p.n for p in B.find_bak_pairs(d)] == [0, 1]    # .bak.7 stays unpaired


# ---------------------------------------------------------------- dialog
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from taiko2x6.app import appconfig  # noqa: E402


@pytest.fixture
def dlg(tmp_path, monkeypatch):
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    app = QApplication.instance() or QApplication([])
    made = []

    def make(path):
        d = B.BackupsUndoDialog(default_path=str(path))
        made.append(d)
        d.wait()
        for _ in range(50):
            app.processEvents()
            if not d._busy():
                break
            d.wait()
        app.processEvents()
        return d
    yield make
    for d in made:
        d.wait()
        d.close()
        d.deleteLater()


def _settle(d):
    app = QApplication.instance()
    for _ in range(20):
        d.wait()
        app.processEvents()


def test_dialog_lists_and_rolls_back(dlg, image, monkeypatch):
    states = _three_sessions(image)
    d = dlg(image)
    _settle(d)
    assert d.tree.topLevelItemCount() == 3
    assert not d.btn_rollback.isEnabled()
    d.tree.topLevelItem(2).setSelected(True)             # oldest
    assert d.btn_rollback.isEnabled()
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: (asked.append(a[2]), QMessageBox.Yes)[1])
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    d._do_rollback()
    _settle(d)
    assert "PCSX2" in asked[0] and "3" in asked[0]
    assert _sha(image) == states[0]
    assert d.tree.topLevelItemCount() == 3
    d.tree.topLevelItem(0).setSelected(True)
    assert not d.btn_rollback.isEnabled()                # already rolled back


def test_dialog_rollback_declined_changes_nothing(dlg, image, monkeypatch):
    states = _three_sessions(image)
    d = dlg(image)
    _settle(d)
    d.tree.topLevelItem(0).setSelected(True)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    d._do_rollback()
    _settle(d)
    assert _sha(image) == states[3]


def test_dialog_locked_image_shows_friendly_error(dlg, image, monkeypatch):
    _three_sessions(image)
    d = dlg(image)
    _settle(d)
    d.tree.topLevelItem(0).setSelected(True)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(B, "check_writable",
                        lambda t: (_ for _ in ()).throw(PermissionError("denied")))
    d._do_rollback()
    _settle(d)
    assert shown and "PCSX2" in shown[0]


def test_dialog_delete_and_restore(dlg, image, tmp_path, monkeypatch):
    _three_sessions(image)
    arc = _bak_folder(tmp_path)
    d = dlg(image)
    _settle(d)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    d.tree.topLevelItem(1).setSelected(True)
    d._do_delete()
    _settle(d)
    assert d.tree.topLevelItemCount() == 1 and image.exists()

    d.row_path.setText(str(arc))
    _settle(d)
    assert d.tree_bak.topLevelItemCount() == 1
    d.tree_bak.topLevelItem(0).setSelected(True)
    d._do_restore()
    _settle(d)
    assert (arc / "DATA.000").read_bytes() == b"old-data"

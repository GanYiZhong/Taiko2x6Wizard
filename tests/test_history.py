"""Activity history: entries from the central write points, cap, dialog."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from taiko2x6 import history, safeio  # noqa: E402
from taiko2x6.archive.archive_model import Archive  # noqa: E402
from taiko2x6.ps2 import ps2hdd  # noqa: E402


def test_real_log_folder_untouched(tmp_path):
    p = history.history_path()                             # conftest redirect
    assert p.name == "activity_test.jsonl" and "history" in p.parent.name


def test_archive_save_is_recorded(archive_pair, tmp_path):
    lp, dp = archive_pair
    a = Archive(lp, dp)
    g = a.layout.groups[0]
    e = a.layout.files_for_group(g)[0]
    a.stage_replace(0, e["index"], b"N" * 200)
    a.close()
    a.save(lp, dp, allow_relayout=True, backup=True)
    ents = history.load()
    assert len(ents) == 1                      # nested write_pair merged in
    e = ents[0]
    assert e["tool"] == "Archive" and e["ok"] is True
    assert e["summary"].startswith("Saved 1 groups")
    assert {os.path.basename(t) for t in e["targets"]} == {"list.bin", "DATA.000"}
    assert e["undo"].endswith(".bak") and os.path.exists(e["undo"])
    raw = history.history_path().read_text(encoding="utf-8")
    assert "NNNN" not in raw                   # paths and summaries only


def test_write_pair_alone_and_failure(tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"1")
    safeio.write_pair([(a, b"2"), (b, b"3")])
    assert history.load()[0]["summary"] == "Wrote a.bin, b.bin"

    def boom(f):
        raise OSError("disk full")
    with pytest.raises(OSError):
        safeio.write_pair([(a, boom), (b, b"x")])
    e = history.load()[0]
    assert e["ok"] is False and "disk full" in e["error"]


def test_image_session_recorded_with_journal(pfs_image):
    img, part, fpath = pfs_image
    with ps2hdd.write_session(str(img)) as h:
        h.pfs_write(part, fpath, b"Z" * 3000)
    e = history.load()[0]
    assert e["tool"] == "HDD image" and e["ok"]
    assert e["targets"] == [str(img)]
    assert e["undo"].endswith(".undo") and os.path.exists(e["undo"])


def test_image_session_failure_and_noop(pfs_image):
    img, part, fpath = pfs_image
    with pytest.raises(RuntimeError):
        with ps2hdd.write_session(str(img)) as h:
            h.pfs_write(part, fpath, b"Q" * 2000)
            raise RuntimeError("bad")
    e = history.load()[0]
    assert e["ok"] is False and e["undo"] == "" and "bad" in e["error"]
    n = len(history.load())
    with ps2hdd.write_session(str(img)):          # wrote nothing
        pass
    assert len(history.load()) == n


def test_outer_scope_wins(pfs_image):
    img, part, fpath = pfs_image
    with history.operation("Game patches", "Boot patches: skip_notice on", [img]):
        with ps2hdd.write_session(str(img)) as h:
            h.pfs_write(part, fpath, b"B" * 1000)
    ents = history.load()
    assert len(ents) == 1
    assert ents[0]["summary"] == "Boot patches: skip_notice on"
    assert ents[0]["undo"].endswith(".undo")


def test_cap_rotation(monkeypatch):
    monkeypatch.setattr(history, "CAP", 50)
    for i in range(80):
        history.record("T", ["x"], f"s{i}")
    ents = history.load()
    assert len(ents) == 50
    assert ents[0]["summary"] == "s79" and ents[-1]["summary"] == "s30"


def test_bad_lines_skipped():
    history.record("T", [], "ok")
    with open(history.history_path(), "a", encoding="utf-8") as f:
        f.write("not json\n")
    assert [e["summary"] for e in history.load()] == ["ok"]


# --------------------------------------------------------------------------- #
#  dialog
# --------------------------------------------------------------------------- #
def _dialog(entries):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from taiko2x6.app import history_dialog
    return history_dialog.HistoryDialog(entries=entries)


ENTRIES = [
    {"time": "2026-11-26 10:00:00", "tool": "Archive", "targets": ["D:/a/DATA.000"],
     "summary": "Saved 3 groups", "ok": True, "undo": "D:/a/DATA.000.bak"},
    {"time": "2026-11-20 09:00:00", "tool": "HDD image", "targets": ["E:/x/game.img"],
     "summary": "Wrote", "ok": True, "undo": "E:/x/game.img.20261120-090000.undo"},
    {"time": "2026-11-10 08:00:00", "tool": "Game patches", "targets": ["E:/x/game.img"],
     "summary": "Boot patches", "ok": False, "undo": "", "error": "boom"},
]


def test_dialog_filters():
    from PySide6.QtCore import QDate
    d = _dialog(ENTRIES)
    try:
        assert d.table.rowCount() == 3
        d.ck_failed.setChecked(True)
        assert d.table.rowCount() == 1
        d.ck_failed.setChecked(False)
        d.ed_target.setText("game.img")
        assert d.table.rowCount() == 2
        d.cb_tool.setCurrentIndex(d.cb_tool.findData("HDD image"))
        assert d.table.rowCount() == 1
        d.cb_tool.setCurrentIndex(0)
        d.ed_target.setText("")
        d.d_from.setDate(QDate(2026, 11, 15))
        assert d.table.rowCount() == 2
        d.d_to.setDate(QDate(2026, 11, 22))
        assert d.table.rowCount() == 1
    finally:
        d.close()
        d.deleteLater()


def test_dialog_buttons_and_backups_handoff(monkeypatch):
    from taiko2x6.ps2 import backups_undo
    got = {}

    class Fake:
        def __init__(self, parent=None, default_path=""):
            got["path"] = default_path

        def exec(self):
            got["ran"] = True

    monkeypatch.setattr(backups_undo, "BackupsUndoDialog", Fake)
    d = _dialog(ENTRIES)
    try:
        assert not d.btn_undo.isEnabled() and not d.btn_folder.isEnabled()
        d.table.selectRow(1)                       # image entry with a journal
        assert d.btn_undo.isEnabled() and d.btn_folder.isEnabled()
        d.btn_undo.click()
        assert got == {"path": "E:/x/game.img", "ran": True}
        d.table.selectRow(0)                       # archive entry: .bak -> folder
        d.btn_undo.click()
        assert got["path"].replace("\\", "/") == "D:/a"
        d.table.selectRow(2)                       # failed, nothing to undo
        assert not d.btn_undo.isEnabled()
    finally:
        d.close()
        d.deleteLater()


def test_dialog_reads_real_file_via_history():
    history.record("Archive", ["z"], "hello")
    d = _dialog(None)
    try:
        assert d.table.rowCount() == 1
    finally:
        d.close()
        d.deleteLater()

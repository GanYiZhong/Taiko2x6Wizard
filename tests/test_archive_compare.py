"""Compare archives: exact categories and counts on two synthetic archives."""
import csv
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QThread  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from conftest import build_archive  # noqa: E402
from taiko2x6.app import appconfig, archive_compare as ac  # noqa: E402
from taiko2x6.archive.archive_model import Archive  # noqa: E402

A_GROUPS = [
    ("song.same", [("a.sht", b"S" * 100), ("a.vag", b"V" * 64)]),
    ("song.chg", [("c.sht", b"1" * 50), ("c2.sht", b"2" * 50), ("keep.sht", b"K" * 30),
                  ("gone.sht", b"G" * 10)]),
    ("song.old", [("o.sht", b"O" * 20)]),
]
B_GROUPS = [
    ("song.same", [("a.sht", b"S" * 100), ("a.vag", b"V" * 64)]),
    ("song.chg", [("c.sht", b"1" * 51),            # size differs
                  ("c2.sht", b"3" * 50),           # same size, content differs
                  ("keep.sht", b"K" * 30), ("new.sht", b"N" * 5)]),
    ("song.new", [("n.sht", b"N" * 20), ("n.vag", b"V" * 20)]),
]


def _write(folder, groups):
    folder.mkdir()
    lst, data = build_archive(groups)
    (folder / "LIST.BIN").write_bytes(lst)
    (folder / "DATA.000").write_bytes(data)
    return folder


@pytest.fixture
def pair(tmp_path):
    return _write(tmp_path / "a", A_GROUPS), _write(tmp_path / "b", B_GROUPS)


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    from taiko2x6.app import i18n
    monkeypatch.setattr(i18n, "_lang", "en")
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    QApplication.instance() or QApplication([])


def _compare(pair, **kw):
    a, b = ac.open_readonly(pair[0]), ac.open_readonly(pair[1])
    try:
        return ac.compare_archives(a, b, **kw)
    finally:
        a.close()
        b.close()


def test_exact_categories(pair):
    r = _compare(pair)
    assert r.file_counts() == {"added": 3, "removed": 2, "changed": 2, "unchanged": 3}
    assert r.group_counts() == {"added": 1, "removed": 1, "changed": 1, "unchanged": 1}
    st = {(f.group, f.name): f.status for g in r.groups for f in g.files}
    assert st[("song.chg", "c.sht")] == "changed"
    assert st[("song.chg", "c2.sht")] == "changed"
    assert st[("song.chg", "keep.sht")] == "unchanged"
    assert st[("song.chg", "gone.sht")] == "removed"
    assert st[("song.chg", "new.sht")] == "added"
    assert st[("song.old", "o.sht")] == "removed"
    assert st[("song.new", "n.vag")] == "added"
    assert {g.name: g.status for g in r.groups} == {
        "song.same": "unchanged", "song.chg": "changed",
        "song.old": "removed", "song.new": "added"}
    by = {(f.group, f.name): f for g in r.groups for f in g.files}
    assert by[("song.chg", "c.sht")].hash_a == ""            # size differs: never hashed
    c2 = by[("song.chg", "c2.sht")]
    assert c2.hash_a != c2.hash_b
    same = by[("song.same", "a.sht")]
    assert same.hash_a == same.hash_b != ""


def test_does_not_write(pair):
    before = [p.read_bytes() for d in pair for p in sorted(d.iterdir())]
    _compare(pair)
    assert before == [p.read_bytes() for d in pair for p in sorted(d.iterdir())]


def test_songs_when_tables_exist(tmp_path, monkeypatch):
    from taiko2x6.songs import song_builder
    ga = [("bins", [("tuning.bin", b"A"), ("musicinfo.bin", b"x")])]
    gb = [("bins", [("tuning.bin", b"B"), ("musicinfo.bin", b"x")])]
    pa, pb = _write(tmp_path / "sa", ga), _write(tmp_path / "sb", gb)
    monkeypatch.setattr(song_builder, "song_ids_from",
                        lambda t: ["aaa", "bbb"] if t == b"A" else ["bbb", "ccc"])
    r = _compare((pa, pb))
    assert r.songs["added"] == ["ccc"] and r.songs["removed"] == ["aaa"]
    assert (r.songs["count_a"], r.songs["count_b"]) == (2, 2)


def test_csv(pair, tmp_path):
    r = _compare(pair)
    r.songs = {"added": ["zzz"], "removed": [], "genre_changed": ["q"]}
    out = tmp_path / "r.csv"
    n = ac.write_csv(r, out)
    with open(out, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["group", "file", "status", "size_a", "size_b", "sha1_a", "sha1_b"]
    assert len(rows) == 1 + n == 1 + 10 + 2
    row = next(x for x in rows if x[:2] == ["song.chg", "c.sht"])
    assert row[2:5] == ["changed", "50", "51"]
    assert ["[songs]", "zzz", "added", "", "", "", ""] in rows
    assert ["[songs]", "q", "genre_changed", "", "", "", ""] in rows


def test_cancel_core(pair):
    with pytest.raises(ac.Cancelled):
        _compare(pair, cancelled=lambda: True)


def test_cancel_worker(pair):
    w = ac.CompareWorker(str(pair[0]), str(pair[1]))
    seen = []
    w.cancelled_sig.connect(lambda: seen.append("c"))
    w.finished_ok.connect(lambda r: seen.append("ok"))
    w.cancel()
    w.run()                                    # synchronous, same code path
    assert seen == ["c"]


def test_missing_pair(tmp_path):
    (tmp_path / "e").mkdir()
    with pytest.raises(FileNotFoundError):
        ac.open_readonly(tmp_path / "e")


def _wait(d):
    for _ in range(500):
        QApplication.processEvents()
        if not d.running:
            break
        QThread.msleep(10)
    QApplication.processEvents()


def test_dialog_flow_filter_preview_export(pair, tmp_path):
    d = ac.ArchiveCompareDialog()
    try:
        d.edit_a.setText(str(pair[0]))
        d.edit_b.setText(str(pair[1]))
        d.start()
        _wait(d)
        assert d.result is not None
        assert "3 added" in d.lbl_counts.text() and "2 removed" in d.lbl_counts.text()
        assert len(d.visible_files()) == 10
        d.cb_status.setCurrentIndex(d.cb_status.findData("changed"))
        assert sorted(f for _g, f, _s in d.visible_files()) == ["c.sht", "c2.sht"]
        d.cb_status.setCurrentIndex(0)
        d.edit_filter.setText("song.new")
        assert {g for g, _f, _s in d.visible_files()} == {"song.new"}
        d.edit_filter.setText("")
        f = next(f for g in d.result.groups for f in g.files if f.name == "c2.sht")
        assert d.file_bytes("a", f.ref_a) == b"2" * 50
        assert d.file_bytes("b", f.ref_b) == b"3" * 50
        out = tmp_path / "x.csv"
        d.export_csv(str(out))
        assert out.exists()
    finally:
        d.close()
        d.deleteLater()


def test_dialog_cancel(pair):
    d = ac.ArchiveCompareDialog()
    try:
        d.edit_a.setText(str(pair[0]))
        d.edit_b.setText(str(pair[1]))
        d.start()
        d.cancel()
        _wait(d)
        assert not d.running                    # the thread must end either way
        assert d.btn_compare.isEnabled()
    finally:
        d.close()
        d.deleteLater()


def test_dialog_open_archive_as_a(pair):
    a = Archive(pair[0] / "LIST.BIN", pair[0] / "DATA.000")
    d = ac.ArchiveCompareDialog(current=a)
    try:
        d.chk_open.setChecked(True)
        d.edit_b.setText(str(pair[1]))
        d.start()
        _wait(d)
        assert d.result.file_counts()["changed"] == 2
        assert a.layout.groups                      # the open archive was left open
    finally:
        d.close()
        d.deleteLater()
        a.close()

import json
import os

import pytest

from taiko2x6 import safeio


def test_atomic_write_replaces_and_leaves_no_temp(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"old")
    safeio.atomic_write(p, b"new")
    assert p.read_bytes() == b"new"
    assert [x.name for x in tmp_path.iterdir()] == ["f.bin"]


def test_make_backup_rolls_and_never_clobbers(tmp_path):
    p = tmp_path / "DATA.000"
    assert safeio.make_backup(p) is None          # missing source: no-op
    p.write_bytes(b"v1")
    b1 = safeio.make_backup(p)
    p.write_bytes(b"v2")
    b2 = safeio.make_backup(p)
    p.write_bytes(b"v3")
    b3 = safeio.make_backup(p)
    assert (b1.name, b2.name, b3.name) == ("DATA.000.bak", "DATA.000.bak.1",
                                           "DATA.000.bak.2")
    assert (b1.read_bytes(), b2.read_bytes(), b3.read_bytes()) == (b"v1", b"v2", b"v3")


def test_write_pair_commits_both(tmp_path):
    d, l = tmp_path / "DATA.000", tmp_path / "list.bin"
    d.write_bytes(b"D0"); l.write_bytes(b"L0")
    baks = safeio.write_pair([(d, b"D1"), (l, b"L1")], backup=True)
    assert d.read_bytes() == b"D1" and l.read_bytes() == b"L1"
    assert sorted(b.name for b in baks) == ["DATA.000.bak", "list.bin.bak"]
    left = sorted(x.name for x in tmp_path.iterdir())
    assert left == ["DATA.000", "DATA.000.bak", "list.bin", "list.bin.bak"]


def test_write_pair_failure_keeps_old_pair(tmp_path, monkeypatch):
    d, l = tmp_path / "DATA.000", tmp_path / "list.bin"
    d.write_bytes(b"D0"); l.write_bytes(b"L0")
    real = safeio._replace
    calls = []

    def flaky(src, dst, attempts=8):
        calls.append((str(src), str(dst)))
        # fail when the second NEW file is moved into place
        if str(dst) == str(l) and str(src).endswith(".tmp"):
            raise PermissionError("simulated lock")
        return real(src, dst, attempts)

    monkeypatch.setattr(safeio, "_replace", flaky)
    with pytest.raises(PermissionError):
        safeio.write_pair([(d, b"D1"), (l, b"L1")], backup=False)
    monkeypatch.setattr(safeio, "_replace", real)
    assert d.read_bytes() == b"D0" and l.read_bytes() == b"L0"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["DATA.000", "list.bin"]


def _crash_state(tmp_path, committed_first: bool):
    """Simulate a process killed during step 4 of write_pair."""
    d, l = tmp_path / "DATA.000", tmp_path / "list.bin"
    entries = []
    for p, new in ((d, b"D1"), (l, b"L1")):
        tmp = p.with_name(p.name + ".abc.tmp")
        old = p.with_name(p.name + ".txn-old")
        tmp.write_bytes(new)
        entries.append({"target": str(p), "tmp": str(tmp), "old": str(old),
                        "had_old": True})
    (tmp_path / "DATA.000.txn").write_text(json.dumps({"version": 1,
                                                       "files": entries}))
    # both originals parked
    (tmp_path / "DATA.000.txn-old").write_bytes(b"D0")
    (tmp_path / "list.bin.txn-old").write_bytes(b"L0")
    if committed_first:
        os.replace(entries[0]["tmp"], d)          # DATA already swapped in
    return d, l


def test_recover_pair_rolls_back_half_done(tmp_path):
    d, l = _crash_state(tmp_path, committed_first=True)
    assert safeio.recover_pair(tmp_path / "DATA.000.txn") == "rolled back"
    assert d.read_bytes() == b"D0" and l.read_bytes() == b"L0"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["DATA.000", "list.bin"]


def test_recover_pair_finishes_committed(tmp_path):
    d, l = _crash_state(tmp_path, committed_first=True)
    os.replace(str(l) + ".abc.tmp", l)            # second swap also done
    assert safeio.recover_pair(tmp_path / "DATA.000.txn") == "committed"
    assert d.read_bytes() == b"D1" and l.read_bytes() == b"L1"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["DATA.000", "list.bin"]


def test_recover_pair_without_journal_is_noop(tmp_path):
    assert safeio.recover_pair(tmp_path / "nope.txn") is None


def test_undo_journal_roundtrip_with_growth(tmp_path):
    p = tmp_path / "disk.img"
    orig = bytes((i * 7) & 0xFF for i in range(300_000)) + bytes(200_000)
    p.write_bytes(orig)
    j = safeio.UndoJournal(p, block=4096)
    with open(p, "r+b") as f:
        for off, data in [(10, b"A" * 5000), (100, b"B" * 10),        # overlap
                          (350_000, b"C" * 70_000),                  # zero region
                          (len(orig) - 100, b"D" * 9000)]:           # grows file
            j.save(f, off, len(data))
            f.seek(off)
            f.write(data)
    j.close()
    assert p.read_bytes() != orig
    info = safeio.journal_info(j.path)
    assert info["original_size"] == len(orig)
    safeio.rollback(j.path, p)
    assert p.read_bytes() == orig


def test_undo_journal_ignores_torn_tail(tmp_path):
    p = tmp_path / "disk.img"
    p.write_bytes(b"\x11" * 8192)
    j = safeio.UndoJournal(p, block=512)
    with open(p, "r+b") as f:
        j.save(f, 0, 512)
        f.seek(0); f.write(b"\x22" * 512)
    j.close()
    with open(j.path, "ab") as jf:                # half-written next record
        jf.write(b"\x00\x02\x00\x00")
    safeio.rollback(j.path, p)
    assert p.read_bytes() == b"\x11" * 8192


def test_rollback_infers_target_from_name(tmp_path):
    p = tmp_path / "TK14.img"
    p.write_bytes(b"\x01" * 1024)
    j = safeio.UndoJournal(p)
    assert j.path.name.startswith("TK14.img.") and j.path.suffix == ".undo"
    with open(p, "r+b") as f:
        j.save(f, 0, 4); f.seek(0); f.write(b"\xff" * 4)
    j.close()
    assert safeio.rollback(j.path)["target"] == str(p)
    assert p.read_bytes() == b"\x01" * 1024


def test_cli_info_and_rollback(tmp_path, capsys):
    p = tmp_path / "x.img"
    p.write_bytes(b"\x05" * 2048)
    j = safeio.UndoJournal(p)
    with open(p, "r+b") as f:
        j.save(f, 0, 10); f.seek(0); f.write(b"z" * 10)
    j.close()
    assert safeio._main(["info", str(j.path)]) == 0
    assert "1 range" in capsys.readouterr().out
    assert safeio._main(["rollback", str(j.path)]) == 0
    assert p.read_bytes() == b"\x05" * 2048

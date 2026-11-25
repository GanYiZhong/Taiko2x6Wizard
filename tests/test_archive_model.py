import hashlib

import pytest

from taiko2x6.archive.archive_model import Archive
from taiko2x6.archive import taiko256_archive_tool_v2 as core
from taiko2x6 import safeio

from conftest import SAMPLE_GROUPS


def _find(a, gname, fname):
    for g in a.layout.groups:
        if g["name"] == gname:
            for e in a.layout.files_for_group(g):
                if e["name"] == fname:
                    return g, e
    raise KeyError((gname, fname))


def _sha(p):
    return hashlib.sha1(p.read_bytes()).hexdigest()


def test_reads_every_file(archive_pair):
    lp, dp = archive_pair
    a = Archive(lp, dp)
    try:
        for gname, files in SAMPLE_GROUPS:
            for fname, blob in files:
                g, e = _find(a, gname, fname)
                assert a.read_file(g, e) == blob
                assert g["unknown2"] == core.compute_unknown2(a.decode_group(g, use_cache=False))
    finally:
        a.close()


def test_save_in_place_with_backup(archive_pair):
    lp, dp = archive_pair
    before = (_sha(lp), _sha(dp))
    a = Archive(lp, dp)
    g, e = _find(a, "song.abc", "abc_m.sht")
    a.stage_replace(g["index"], e["index"], b"N" * 290)
    a.close()
    summary = a.save(lp, dp, allow_relayout=False, backup=True)
    assert summary["changed_groups"] == 1
    assert (_sha(lp.with_name("list.bin.bak")), _sha(dp.with_name("DATA.000.bak"))) == before
    assert not safeio.txn_path(dp).exists()

    b = Archive(lp, dp)
    try:
        g, e = _find(b, "song.abc", "abc_m.sht")
        assert b.read_file(g, e) == b"N" * 290
        g2, e2 = _find(b, "song.abc", "abc.vag")
        assert b.read_file(g2, e2) == bytes(range(256)) * 4
        assert g["unknown2"] == core.compute_unknown2(b.decode_group(g, use_cache=False))
    finally:
        b.close()


def test_failed_save_touches_nothing(archive_pair):
    lp, dp = archive_pair
    before = (_sha(lp), _sha(dp))
    a = Archive(lp, dp)
    g, e = _find(a, "song.xyz", "xyz_m.sht")
    a.stage_replace(g["index"], e["index"], b"Y" * 50_000)   # cannot fit in place
    a.close()
    with pytest.raises(ValueError):
        a.save(lp, dp, allow_relayout=False, backup=True)
    assert (_sha(lp), _sha(dp)) == before
    assert sorted(p.name for p in lp.parent.iterdir()) == ["DATA.000", "list.bin"]


def test_rebuild_allows_any_size_and_keeps_layout(archive_pair):
    lp, dp = archive_pair
    a = Archive(lp, dp)
    untouched = _find(a, "bins", "musicinfo.bin")[0]["sector"]
    g, e = _find(a, "song.xyz", "xyz_m.sht")
    big = bytes((i * 31) & 0xFF for i in range(50_000))
    a.stage_replace(g["index"], e["index"], big)
    out_l, out_d = lp.with_name("list.new"), dp.with_name("DATA.new")
    summary = a.rebuild(out_l, out_d)
    a.close()
    assert summary["changed_groups"] == 1
    b = Archive(out_l, out_d)
    try:
        g, e = _find(b, "song.xyz", "xyz_m.sht")
        assert b.read_file(g, e) == big
        assert _find(b, "bins", "musicinfo.bin")[0]["sector"] == untouched
    finally:
        b.close()


def test_open_recovers_interrupted_save(archive_pair):
    lp, dp = archive_pair
    good = (lp.read_bytes(), dp.read_bytes())
    # crash after DATA was swapped but before list.bin: DATA is new, list old
    dp.replace(dp.with_name("DATA.000.txn-old"))
    dp.write_bytes(b"garbage-new-data")
    lp.with_name("list.bin.zz.tmp").write_bytes(b"garbage-new-list")
    import json
    safeio.txn_path(dp).write_text(json.dumps({"version": 1, "files": [
        {"target": str(dp), "tmp": str(dp) + ".gone.tmp",
         "old": str(dp.with_name("DATA.000.txn-old")), "had_old": True},
        {"target": str(lp), "tmp": str(lp.with_name("list.bin.zz.tmp")),
         "old": str(lp.with_name("list.bin.txn-old")), "had_old": True},
    ]}))
    a = Archive(lp, dp)                   # must roll back, then open cleanly
    try:
        assert (lp.read_bytes(), dp.read_bytes()) == good
        assert len(a.layout.groups) == len(SAMPLE_GROUPS)
    finally:
        a.close()


def test_gui_reexports_archive():
    pytest.importorskip("PySide6")
    from taiko2x6.app import taiko256_explorer_gui6 as gui
    assert gui.Archive is Archive


def test_cli_patch_overwrite_makes_backup(archive_pair, tmp_path):
    lp, dp = archive_pair
    src = tmp_path / "src"
    core.main(["extract", "--list", str(lp), "--data", str(dp), "--out", str(src)])
    f = src / "song" / "abc" / "abc_m.sht"
    f.write_bytes(b"Q" * 280)
    before = _sha(dp)
    core.main(["patch", "--list", str(lp), "--data", str(dp), "--src", str(src),
               "--out-list", str(lp), "--out-data", str(dp)])
    assert _sha(dp.with_name("DATA.000.bak")) == before
    a = Archive(lp, dp)
    try:
        g, e = _find(a, "song.abc", "abc_m.sht")
        assert a.read_file(g, e) == b"Q" * 280
    finally:
        a.close()


def test_free_hole_finds_dead_space_between_groups():
    S = core.SECTOR_SIZE
    spans = {0: (0, 3 * S), 1: (10 * S, 11 * S - 5), 2: (20 * S, 21 * S), 3: (3 * S, 4 * S)}
    # group 1 grows: its own old slot counts as free, so 4S..20S is one gap
    assert Archive._free_hole(spans, 1, 6 * S, 30 * S) == 4 * S
    assert Archive._free_hole(spans, 1, 16 * S, 30 * S) == 4 * S
    assert Archive._free_hole(spans, 1, 17 * S, 30 * S) is None   # DATA never grows
    assert Archive._free_hole(spans, 0, 2 * S, 30 * S) == 4 * S   # not over group 1


def test_save_relayout_without_a_hole_still_refuses(archive_pair):
    lp, dp = archive_pair
    before = (_sha(lp), _sha(dp))
    a = Archive(lp, dp)
    g, e = _find(a, "song.xyz", "xyz_m.sht")
    a.stage_replace(g["index"], e["index"], bytes((i * 31) & 0xFF for i in range(50_000)))
    a.close()
    with pytest.raises(ValueError):
        a.save(lp, dp, allow_relayout=True)
    assert (_sha(lp), _sha(dp)) == before

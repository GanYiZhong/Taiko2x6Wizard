"""The Omnimix planner's caches and indexes must not change any result."""
import os
import random
import struct

from conftest import build_archive

from taiko2x6.bineditors import bineditor_tuning as TU
from taiko2x6.songs import omnimix_maker as O
from taiko2x6.songs import song_builder


# ---- reference (pre-optimisation) implementations ------------------------- #
def _ref_harvest(archive, sid):
    from taiko2x6.charts import sht_t7
    new_groups, extra_files = [], {}
    want_select = {f"select_full_{sid}", f"select_short_{sid}"}
    non_name = f"select_non_{sid}"
    for grp in archive.layout.groups:
        gn = grp["name"]
        comp = grp.get("compression", 2)
        per_song = (O._fumen_belongs(gn, sid)
                    or gn == f"sound.stream.music_{sid}"
                    or any(gn == f"music_texture.{t}_{sid}" for t in O._TEX_TYPES))
        if per_song:
            files = [(e["name"], archive.read_file(grp, e))
                     for e in archive.layout.files_for_group(grp)]
            g = {"name": gn, "files": files, "compression": comp}
            if gn.startswith("fumen."):
                conv = [sht_t7.ensure_gen2(d) for _n, d in files]
                if any(c for _d, c in conv):
                    g["files"] = [(n, d) for (n, _o), (d, _c) in zip(files, conv)]
                    g["converted"] = True
            new_groups.append(g)
        elif gn == "music_texture.music_select":
            for e in archive.layout.files_for_group(grp):
                if e["name"] in want_select:
                    extra_files.setdefault(gn, []).append(
                        (e["name"], archive.read_file(grp, e)))
        elif gn.startswith("music_texture.music_select_"):
            for e in archive.layout.files_for_group(grp):
                if e["name"] == non_name:
                    extra_files.setdefault(gn, []).append(
                        (e["name"], archive.read_file(grp, e)))
    return new_groups, extra_files


def _ref_scan_negs(data, pool_offset):
    negs, end, off = [], pool_offset - 3, 0
    while off < end:
        if -300 < struct.unpack_from("<i", data, off)[0] < -1:
            negs.append(off)
        off += 4
    return negs


def _ref_printable(data, pool_offset):
    if pool_offset >= len(data):
        return False
    region = data[pool_offset:]
    if not region:
        return False
    return all(not (0 < b < 0x20) or b == 9 for b in region)


def _ref_strings(data, pool_offset):
    out, n, i = [], len(data), pool_offset
    while i < n:
        if 0x20 <= data[i] < 0x7F:
            j = i
            while j < n and 0x20 <= data[j] < 0x7F:
                j += 1
            if j - i >= 2:
                out.append((i, data[i:j].decode("ascii", "replace")))
            i = j
        else:
            i += 1
    return out


# ---- harvest index --------------------------------------------------------- #
def _song_archive(tmp_path, n=12):
    groups = [("tuning_misc", [("x.bin", b"X")])]
    sel = []
    non = []
    for i in range(n):
        sid = f"s{i}"
        for pl in ("1p", "2p"):
            for d in "enhm":
                groups.append((f"fumen.{sid}{pl}_{d}", [("sht", b"c%d%s%s" % (i, pl.encode(), d.encode()))]))
        groups.append((f"sound.stream.music_{sid}", [("vag", b"V%d" % i)]))
        groups.append((f"music_texture.games_{sid}", [("nut", b"G%d" % i)]))
        groups.append((f"music_texture.total_result_{sid}", [("nut", b"T%d" % i)]))
        sel += [(f"select_short_{sid}", b"short%d" % i), (f"select_full_{sid}", b"full%d" % i),
                (f"select_other_{i}", b"o")]
        non.append((f"select_non_{sid}", b"non%d" % i))
    groups.insert(3, ("music_texture.music_select", sel))
    groups.insert(5, ("music_texture.music_select_2", non))
    groups.append(("music_texture.music_selectx", [("select_non_s0", b"nope")]))
    lst, data = build_archive(groups)
    (tmp_path / "list.bin").write_bytes(lst)
    (tmp_path / "DATA.000").write_bytes(data)
    return O.open_pair(tmp_path / "list.bin", tmp_path / "DATA.000")


def test_harvest_index_equals_scan(tmp_path):
    a = _song_archive(tmp_path)
    try:
        idx = O.HarvestIndex(a)
        for sid in [f"s{i}" for i in range(12)] + ["nosuch", "s1p"]:
            assert O.harvest_song(a, sid, idx) == _ref_harvest(a, sid)
            assert O.harvest_song(a, sid) == _ref_harvest(a, sid)
        g, x = O.harvest_song(a, "s3", idx)
        assert len(g) == 8 + 1 + 2 and [n for n, _ in x["music_texture.music_select"]] == \
            ["select_short_s3", "select_full_s3"]
    finally:
        a.close()


# ---- read cache ------------------------------------------------------------ #
def test_read_cache_reads_each_file_once(monkeypatch):
    calls = []
    pool = b"".join(b"s%d\0music_s%d\0T%d\0" % (i, i, i) for i in range(4))

    def fake(arc, name):
        calls.append((arc.name, name))
        if name == "gone":
            raise FileNotFoundError(name)
        return b"\xff\xff" + pool

    monkeypatch.setattr(song_builder, "_read_named", fake)

    class A:
        name = "a"
    arc = A()
    cold = [O._ordered_ids(arc), O._ordered_ids(arc), O.song_titles(arc)]
    assert len(calls) == 3                     # uncached: every call reads
    calls.clear()
    with O._read_cache():
        warm = [O._ordered_ids(arc), O._ordered_ids(arc), O.song_titles(arc)]
        assert O._named(arc, "tuning.bin") is O._named(arc, "TUNING.BIN")
        for _ in range(2):
            try:
                O._named(arc, "gone")
            except FileNotFoundError:
                pass
        with O._read_cache():                  # nesting keeps the outer cache
            O._ordered_ids(arc)
    assert warm == cold
    assert calls == [("a", "tuning.bin"), ("a", "gone")]
    calls.clear()
    O._ordered_ids(arc)                        # cache is gone after the block
    assert len(calls) == 1
    # a returned id list is a copy: editing it cannot corrupt the cache
    with O._read_cache():
        O._ordered_ids(arc).append("zzz")
        assert "zzz" not in O._ordered_ids(arc)


# ---- bineditor fast paths -------------------------------------------------- #
def test_scan_and_pool_helpers_match_reference():
    rnd = random.Random(7)
    for _ in range(60):
        n = rnd.randrange(0, 400)
        data = bytes(rnd.choice([0, 9, 10, 0x20, 0x41, 0x7E, 0xFF, 0x80]) for _ in range(n))
        data = bytes(data)
        # sprinkle small negatives so the marker scan has hits
        buf = bytearray(data + bytes((-n) % 4))
        for k in range(0, len(buf) - 3, 16):
            struct.pack_into("<i", buf, k, rnd.choice([-5, -299, -1, -300, 7]))
        data = bytes(buf)
        for po in {0, 3, 4, len(data) // 2, len(data)}:
            if po > len(data):
                continue
            assert TU._pool_is_printable_to_eof(data, po) == _ref_printable(data, po)
            assert [(s.offset, s.text) for s in TU._extract_pool_strings(data, po)] == \
                _ref_strings(data, po)
            got = TU._scan_blocks(data, po)
            negs = _ref_scan_negs(data, po)
            assert bool(got) == bool(negs)


def test_pool_strings_equal_parse_strings():
    body = os.urandom(5000)
    pool = b"".join(b"s%d\0music_s%d\0Title %d\0" % (i, i, i) for i in range(30))
    for data in (body + pool, b"\x01" * 64 + pool, pool, b""):
        parsed = TU.parse(data).strings
        assert [(s.offset, s.text) for s in TU.pool_strings(data)] == \
            [(s.offset, s.text) for s in parsed]
    assert song_builder.song_ids_from(body + pool) == [f"s{i}" for i in range(30)]

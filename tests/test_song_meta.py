"""Genre / star readers used by the Omnimix merge (synthetic files)."""
import struct

from taiko2x6.bineditors import bineditor_tuning as TU
from taiko2x6.songs import omnimix_maker as O


def _t8_musicinfo(rows, recsz=104):
    """T8-style header (13 u32) + `recsz`-byte rows + pool; rows = (id, title, genre)."""
    pool = bytearray()
    recs = bytearray()
    for sid, title, genre in rows:
        io = len(pool); pool += sid.encode() + b"\0"
        to = len(pool); pool += title.encode() + b"\0"
        rec = bytearray(recsz)
        struct.pack_into("<3i", rec, 0, io, to, genre)
        recs += rec
    o0 = 0x34 + len(recs)
    hdr = struct.pack("<13I", len(rows), 0, 0, 0, 0, 0, 0, o0, o0, o0, o0, o0, o0)
    return hdr + bytes(recs) + bytes(pool)


def test_musicinfo_genres_t12t_rows():
    raw = _t8_musicinfo([("seljpp", "J-POP", 9), ("song1", "a", 0),
                         ("song2", "b", 4), ("cpop1", "c", 8)])
    assert O.musicinfo_genres(raw) == {"song1": 0, "song2": 4, "cpop1": 8}


def test_musicinfo_genres_t11t_100_byte_rows():
    raw = _t8_musicinfo([("x", "x", 6), ("y", "y", 2)], recsz=100)
    assert O.musicinfo_genres(raw) == {"x": 6, "y": 2}


def _t14_tuning(songs):
    """count + 2028-byte records (col0 = id offset) + pool."""
    pool = bytearray()
    recs = bytearray()
    for sid, stars in songs:
        rec = bytearray(TU.T14_REC)
        struct.pack_into("<i", rec, 0, len(pool))
        pool += sid.encode() + b"\0music_" + sid.encode() + b"\0"
        TU.set_t14_stars(rec, stars)
        recs += rec
    return struct.pack("<I", len(songs)) + bytes(recs) + bytes(pool)


def test_t14_stars_roundtrip():
    data = _t14_tuning([("aaa", [4, 7, 8, 10]), ("bbb", [1, 2, 3, -1])])
    assert TU.is_t14_layout(data)
    assert TU.t14_stars(data) == {"aaa": [4, 7, 8, 10], "bbb": [1, 2, 3, -1]}


def test_valid_stars():
    assert O._valid_stars([4, 7, 8, 10]) and O._valid_stars([1, 2, 3, -1])
    assert not O._valid_stars([2, 151, 550260, 0])


def test_fix_genre_jumps_points_cpop_slot_at_cpop_row():
    from types import SimpleNamespace
    rows = [[0, 0, 9], [0, 0, 7], [0, 0, 4], [0, 0, 0], [0, 0, 8], [0, 0, 15]]
    sec6 = [0, 0, 0, 0, 2, 3, 0, 0, 0, 0, 0, 0]
    m = SimpleNamespace(sec0=rows, sec6=sec6)
    O.fix_genre_jumps(m)
    assert sec6[11] == 4          # C-POP -> first genre-8 row
    assert sec6[4] == 2 and sec6[5] == 3   # already-valid slots untouched
    assert sec6[0:4] == [0, 0, 0, 0]       # non-folder slots untouched


def test_group_rows_by_genre_remaps_row_indices():
    from types import SimpleNamespace

    def row(g, ura=-1):
        r = [0] * 26
        r[2] = g
        r[10] = ura
        return r
    # header(9), namco, jpop, namco(ura -> row 4), namco ex, jpop, header(15)
    rows = [row(9), row(4), row(0), row(4, ura=4), row(4), row(0), row(15)]
    for i, r in enumerate(rows):
        r[13] = r[14] = i
        r[15] = r[16] = len(rows) - 1 - i
    m = SimpleNamespace(variant="t14", sec0=rows, sec1=[0, 3], sec4=[(3, 0, 1)],
                        sec6=[0, 0, 0, 0, 1, 2, 0, 0, 0, 0, 0, 6], sec7=[5, 2])
    O.group_rows_by_genre(m)
    assert [r[2] for r in m.sec0] == [9, 15, 4, 4, 4, 0, 0]
    assert [r[13] for r in m.sec0] == list(range(7))
    assert [r[14] for r in m.sec0] == list(range(7))
    assert sorted(r[15] for r in m.sec0) == list(range(7))
    # old 3 -> new 3 (namco: 1,3,4 -> 2,3,4), its ura old 4 -> new 4
    assert m.sec0[3][10] == 4
    assert m.sec1 == [0, 3] and m.sec4 == [(3, 0, 1)]
    assert m.sec6[4] == 2 and m.sec6[5] == 5 and m.sec6[11] == 1
    assert m.sec7 == [6, 5]


def test_set_t14_stars_by_id():
    data = _t14_tuning([("aaa", [4, 7, 8, 10]), ("bbb", [4, 7, 8, 10])])
    out = TU.set_t14_stars_by_id(data, {"bbb": [2, 4, 6, 6]})
    assert TU.t14_stars(out) == {"aaa": [4, 7, 8, 10], "bbb": [2, 4, 6, 6]}
    assert len(out) == len(data)


def test_target_star_fixes_only_template_clones(monkeypatch):
    from types import SimpleNamespace
    tmpl = [4, 7, 8, 10]
    tuning = _t14_tuning([("10jiku", tmpl), ("a12c01", tmpl), ("rot3", [5, 7, 8, 10]),
                          ("lonely", tmpl)])
    from taiko2x6.songs import song_builder
    monkeypatch.setattr(song_builder, "_read_named", lambda arc, name: tuning)
    monkeypatch.setattr(O, "pick_template_id", lambda target: "10jiku")
    spec = SimpleNamespace(label="SRC")
    metas = {"SRC": {"a12c01": {"stars": [2, 4, 6, 6]},       # clone -> fixed
                     "rot3": {"stars": [5, 6, 7, 9]},         # real rating: kept
                     "10jiku": {"stars": [1, 1, 1, 1]}}}      # template itself: kept
    fixes = O._target_star_fixes(object(), [(spec, None)], metas)
    assert fixes == {"a12c01": [2, 4, 6, 6]}                 # 'lonely': no source


def test_resolve_trusted_ignores_songs_a_mod_added(monkeypatch):
    from types import SimpleNamespace
    # base: stock (a, b); mod = base + x filed under Namco (4); official T13 has x as J-POP
    ids = {"base": ["a", "b"], "mod": ["a", "b", "x", "y"], "t13": ["x", "z"],
           "tgt": ["a", "b", "x", "y"]}
    gen = {"base": {"a": 4, "b": 1}, "mod": {"a": 4, "b": 1, "x": 4, "y": 4},
           "t13": {"x": 0, "z": 6}, "tgt": {"a": 4, "b": 1, "x": 4, "y": 4}}
    arcs = {k: SimpleNamespace(name=k) for k in ids}
    monkeypatch.setattr(O, "_ordered_ids", lambda arc: ids[arc.name])
    monkeypatch.setattr(O, "musicinfo_genres", lambda raw: gen[raw])
    from taiko2x6.songs import song_builder
    monkeypatch.setattr(song_builder, "_read_named", lambda arc, name: arc.name)
    monkeypatch.setattr(TU, "is_t14_layout", lambda raw: False)
    opened = [(SimpleNamespace(label=k), arcs[k]) for k in ("base", "mod", "t13")]
    metas = {"t13": {"x": {"stars": [2, 3, 4, 5]}, "z": {"stars": [1, 2, 3, 4]}},
             "mod": {"x": {"stars": [4, 7, 8, 10]}}}
    z = O.PlanItem(sid="z", source_label="t13", stars=[1, 1, 1, 1], genre=4)
    gfix, sfix, rank = O._resolve_trusted(arcs["tgt"], opened, [z], metas)
    assert gfix == {"x": 0} and sfix == {"x": [2, 3, 4, 5]}   # y: no trusted source
    assert z.genre == 6 and z.stars == [1, 2, 3, 4]
    assert rank["a"] == (0, 0) and rank["x"] == (2, 0) and "y" not in rank


def test_group_rows_orders_blocks_by_source():
    from types import SimpleNamespace
    pool = b""
    rows = []
    for sid, g in [("h", 9), ("late", 0), ("stock", 0), ("t13", 0)]:
        rows.append([len(pool), 0, g] + [0] * 7 + [-1, 0, 0, 0, 0, 0, 0] + [0] * 9)
        pool += sid.encode() + bytes(1)
    for i, r in enumerate(rows):
        r[13] = r[14] = i
    m = SimpleNamespace(variant="t14", sec0=rows, sec1=[], sec4=[], sec6=[], sec7=[], pool=pool)
    O.group_rows_by_genre(m, {"stock": (0, 5), "t13": (2, 1)})
    names = [pool[r[0]:pool.find(bytes(1), r[0])].decode() for r in m.sec0]
    assert names == ["h", "stock", "t13", "late"]


def test_resolve_trusted_same_format_mod_is_untrusted(monkeypatch):
    """A TK14-format pack that dropped some stock songs is still a mod."""
    from types import SimpleNamespace
    ids = {"base": ["a", "b"], "pack": ["a", "x"], "t13": ["x"], "tgt": ["a", "b", "x"]}
    gen = {"base": {"a": 4, "b": 1}, "pack": {"a": 4, "x": 4}, "t13": {"x": 3},
           "tgt": {"a": 4, "b": 1, "x": 4}}
    arcs = {k: SimpleNamespace(name=k) for k in ids}
    monkeypatch.setattr(O, "_ordered_ids", lambda arc: ids[arc.name])
    monkeypatch.setattr(O, "musicinfo_genres", lambda raw: gen[raw])
    from taiko2x6.songs import song_builder
    monkeypatch.setattr(song_builder, "_read_named", lambda arc, name: arc.name)
    monkeypatch.setattr(TU, "is_t14_layout", lambda raw: raw in ("base", "pack", "tgt"))
    opened = [(SimpleNamespace(label=k), arcs[k]) for k in ("base", "pack", "t13")]
    gfix, _sfix, rank = O._resolve_trusted(arcs["tgt"], opened, [], {})
    assert gfix == {"x": 3} and rank["x"] == (2, 0)

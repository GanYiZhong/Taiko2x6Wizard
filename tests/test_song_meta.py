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
    assert O.musicinfo_genres(raw) == {"song1": 0, "song2": 4, "cpop1": 0}


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

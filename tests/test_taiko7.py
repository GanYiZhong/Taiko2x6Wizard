"""Taiko 7 sources for Omnimix: chart layout conversion + tuning star reader."""
import struct

import pytest

from taiko2x6.bineditors import bineditor_tuning as TU
from taiko2x6.charts import sht_t7, sht_validator, tja2sht
from taiko2x6.songs import omnimix_maker as O

from conftest import build_archive


def _t7_chart(measures=((10, 96), (2509, 96), (5009, 120))):
    """A Taiko 7 chart: 112-byte measures, int time/bpm, one don per measure,
    duplicated in sub-tracks 0 and 3 like the retail charts."""
    n = len(measures)
    note_off = 16 + n * sht_t7.TRACK_T7_SIZE
    out = bytearray(struct.pack("<IIii", n, note_off, 2 * n, 0))
    for i, (t, bpm) in enumerate(measures):
        out += struct.pack("<4i", t, bpm, 1, 0)
        out += struct.pack("<6i", *([-1] * 6))
        rem = n - i - 1
        a, b = 2 * i, 2 * i + 1                 # empty slots point at the next note
        subs = [(a, 1, rem), (b, 0, 0), (b, 0, 0),
                (b, 1, rem), (b + 1, 0, 0), (b + 1, 0, 0)]
        for s in subs:
            out += struct.pack("<3i", *s)
    for i in range(n):
        out += struct.pack("<4i", 1, 0, -1, -1) * 2
    return bytes(out)


def test_t7_chart_converts_to_gen2():
    src = _t7_chart()
    assert sht_t7.is_t7_sht(src)
    out = sht_t7.t7_to_sht(src)
    assert not sht_t7.is_t7_sht(out)
    m = tja2sht.parse_sht(out)
    assert not m["isOld"]
    assert [(t["time"], t["bpm"]) for t in m["tracks"]] == \
        [(10.0, 96.0), (2509.0, 96.0), (5009.0, 120.0)]
    assert all(t["scrollSpeeds"] == [1.0] * 6 for t in m["tracks"])
    assert all(t["bunkis"] == [-1] * 6 for t in m["tracks"])
    assert m["tracks"][1]["subtracks"][0] == {"noteIndexSt": 2, "noteCount": 1, "pointGain": 1}
    assert out[m["noteOffset"]:] == src[16 + 3 * sht_t7.TRACK_T7_SIZE:]   # notes verbatim
    assert not [i for i in sht_validator.validate_sht(out, "easy") if i.severity == "ERROR"]


def test_ensure_gen2_leaves_gen2_alone():
    gen2 = sht_t7.t7_to_sht(_t7_chart())
    assert sht_t7.ensure_gen2(gen2) == (gen2, False)
    with pytest.raises(ValueError):
        sht_t7.t7_to_sht(gen2)
    assert not sht_t7.is_t7_sht(b"junk")


def _t7_tuning(stars):
    """count songs x (936-byte 1P + 948-byte 2P) blocks, 26-int records, then
    a pool starting '<id>\\0music_<id>'."""
    out = bytearray()
    for k, st in enumerate(stars):
        for size in (TU.T7_BLOCK_1P, TU.T7_BLOCK_2P):
            blk = bytearray(b"\xff" * size)
            for r in range(4):
                rec = [0] * TU.T7_RECORD_INTS
                rec[0] = len(stars) if (k == 0 and size == TU.T7_BLOCK_1P and r == 0) else 1
                rec[5] = st[r]
                rec[TU.T7_MARKER_INDEX] = -50
                rec[15] = 10000
                struct.pack_into("<%di" % TU.T7_RECORD_INTS, blk, r * TU.T7_RECORD_INTS * 4, *rec)
            out += blk
    out += b"\0" * 6
    for k in range(len(stars)):
        out += b"s%d\0music_s%d\0" % (k, k)
    return bytes(out)


def test_t7_tuning_stars():
    stars = [[3, 4, 5, 8], [1, 2, 2, 6], [5, 6, 7, 10]]
    tu = _t7_tuning(stars)
    assert TU.is_t7_layout(tu)
    assert TU.t7_stars(tu) == stars


def test_t7_tuning_rejects_bad_size():
    tu = _t7_tuning([[1, 2, 3, 4]])
    bad = struct.pack("<i", 5) + tu[4:]              # claims 5 songs
    with pytest.raises(ValueError):
        TU.t7_stars(bad)


def test_harvest_converts_t7_charts(tmp_path):
    chart = _t7_chart()
    groups = [("fumen.abc1p_m", [("sht", chart)]),
              ("sound.stream.music_abc", [("vag", b"V" * 64)])]
    lst, data = build_archive(groups)
    (tmp_path / "list.bin").write_bytes(lst)
    (tmp_path / "DATA.000").write_bytes(data)
    a = O.open_pair(tmp_path / "list.bin", tmp_path / "DATA.000")
    try:
        g, x = O.harvest_song(a, "abc")
    finally:
        a.close()
    fumen = [gg for gg in g if gg["name"] == "fumen.abc1p_m"][0]
    assert fumen["converted"] and fumen["files"][0][1] == sht_t7.t7_to_sht(chart)
    audio = [gg for gg in g if gg["name"].startswith("sound.")][0]
    assert "converted" not in audio and audio["files"][0][1] == b"V" * 64
    assert O.song_asset_summary(g, x)["t7_charts_converted"] == 1

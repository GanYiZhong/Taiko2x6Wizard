"""Per-chart soul-gauge / scoring values in tuning.bin (synthetic tables)."""
import struct

from taiko2x6.bineditors import bineditor_tuning as TU


def _t14(rows):
    """A T14 tuning.bin: rows = [(sid, {d: {col: value}})], star 5 everywhere."""
    cnt = len(rows)
    pool = b""
    offs = []
    for sid, _ in rows:
        offs.append(len(pool))
        pool += sid.encode() + b"\0" + b"music_" + sid.encode() + b"\0"
    out = bytearray(struct.pack("<I", cnt))
    for (sid, diffs), off in zip(rows, offs):
        rec = [0] * (TU.T14_REC // 4)
        rec[0] = off
        for d in range(4):
            rec[4 + 28 * d] = 5
            for c, v in diffs.get(d, {}).items():
                rec[c + 28 * d] = v
        out += struct.pack("<507i", *rec)
    return bytes(out + pool)


def test_set_play_fields_writes_only_the_given_songs_and_columns():
    data = _t14([("aaa", {3: {12: 16, 13: 8, 14: -32}}),
                 ("bbb", {3: {12: 16, 13: 8, 14: -32}})])
    assert TU.is_t14_layout(data)
    out = TU.set_t14_play_fields_by_id(
        data, {"bbb": [None, None, None, {12: 46, 13: 23, 14: -74, 4: 99}]})
    f = TU.t14_play_fields(out)
    assert [f["bbb"][3][c] for c in (12, 13, 14)] == [46, 23, -74]
    assert [f["aaa"][3][c] for c in (12, 13, 14)] == [16, 8, -32]
    assert TU.t14_stars(out)["bbb"] == [5, 5, 5, 5]          # star untouched
    diff = [i for i in range(0, len(data), 4) if data[i:i + 4] != out[i:i + 4]]
    assert len(diff) == 3


def test_no_chart_no_write():
    data = bytearray(_t14([("aaa", {})]))
    struct.pack_into("<i", data, 4 + 4 * (4 + 28 * 3), -1)    # no oni chart
    out = TU.set_t14_play_fields_by_id(bytes(data), {"aaa": [None] * 3 + [{12: 9}]})
    assert out == bytes(data)
    assert TU.t14_play_fields(out)["aaa"][3] is None


def test_older_layouts_map_onto_t14_columns():
    # T8..T13: ints 6..26 are T14 columns 5..25; Taiko 7 drops the scoring pair
    t8 = list(range(100, 127))
    t8[5] = 7                                     # star
    m = TU._mapped(t8, TU._T8_TO_T14)
    assert m[5] == t8[6] and m[12] == t8[13] and m[14] == t8[15] and m[25] == t8[26]
    t7 = list(range(200, 226))
    t7[5] = 7
    m7 = TU._mapped(t7, TU._T7_TO_T14)
    assert m7[5] == t7[6] and m7[12] == t7[12] and m7[14] == t7[14]
    assert 6 not in m7 and 7 not in m7           # T7 has no score init/diff
    t7[5] = -1
    assert TU._mapped(t7, TU._T7_TO_T14) is None


def test_estimate_gauge_reproduces_stock_charts():
    # (difficulty, stars, hits) and the (GOOD, OK, BAD) stock TK14 ships
    stock = [((0, 4, 147), (93, 70, -46)),      # 10jiku easy
             ((1, 7, 214), (62, 47, -62)),      # 10jiku normal
             ((2, 5, 268), (55, 41, -69)),      # 10tai hard
             ((3, 10, 831), (16, 8, -32)),      # 10jiku oni
             ((3, 7, 426), (33, 17, -53))]      # 2honno oni
    for args, want in stock:
        got = TU.estimate_gauge(*args)
        assert all(abs(g - w) <= 2 for g, w in zip(got, want)), (args, got, want)
    g, o, b = TU.estimate_gauge(3, 6, 400)
    assert o == g // 2 and b == -int(g * 1.6)
    g, o, b = TU.estimate_gauge(2, 99, 0)          # clamps stars and hits
    assert g > 0 and o > 0 and b < 0

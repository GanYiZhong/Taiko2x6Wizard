"""sht -> TJA and sht -> Gen3 fumen, checked by converting back."""
from taiko2x6.charts import gen3_convert, sht_export, tja2sht

TJA = """TITLE:t
BPM:150
OFFSET:-1.25
COURSE:Oni
LEVEL:8
BALLOON:12,7

#START
1020,
#MEASURE 3/4
#SCROLL 1.5
112,
#MEASURE 4/4
#BPMCHANGE 200
#GOGOSTART
500000000008,
#GOGOEND
#BARLINEOFF
7008A0B0,
#BARLINEON
9000,
0008,
#MEASURE 5/4
11111,
#END
"""


def _key(m):
    _div, rts = sht_export.routes(m)
    tracks = [(round(t["time"], 2), round(t["bpm"], 3), t["gogoFlag"],
               t["trackLine"], tuple(t["bunkis"]),
               round(t["scrollSpeeds"][0], 3)) for t in m["tracks"]]
    notes = [[(n["type"], n["measure"], n["balloonHitCount"],
               n["longNoteLength"]) for n in r[0]] for r in rts]
    return tracks, notes


def test_tja_round_trip():
    sht = tja2sht.parse_sht(tja2sht.convert_tja(TJA, "oni"))
    text = sht_export.sht_to_tja({"oni": sht}, title="t")
    back = tja2sht.parse_sht(tja2sht.convert_tja(text, "oni"))
    assert _key(back) == _key(sht)
    assert "#MEASURE 3/4" in text and "BALLOON:12,7" in text
    # the 5/4 measure is a bar plus a 1/4 tail without a bar line
    assert [t["trackLine"] for t in sht["tracks"]][-2:] == [1, 0]


def test_gen3_round_trip_and_encryption(tmp_path):
    sht = tja2sht.convert_tja(TJA, "oni")
    plain = sht_export.sht_to_gen3(sht, "oni", stars=8)
    back = tja2sht.parse_sht(gen3_convert.gen3_to_sht(
        gen3_convert.parse_gen3(plain)))
    assert _key(back) == _key(tja2sht.parse_sht(sht))
    p = tmp_path / "t_m.bin"
    p.write_bytes(sht_export.encrypt_fumen(plain))
    assert gen3_convert.decrypt_fumen(p) == plain


def test_hp_values_match_shipped_chart():
    # 10binz oni, 8 stars, 669 notes ships good 21 / ok 11
    good, ok, _bad = sht_export.hp_values("oni", 8, 669)
    assert (good, ok) == (21, 11)


def _gen3_measure(offset, branches, info):
    out = []
    for notes in branches:
        out.append({"count": len(notes), "unk": 0, "speed": 1.0, "notes": [
            {"type": t, "pos": p, "item": 0, "pad": 0, "u10": 0, "u12": 0,
             "dur": 0.0} for t, p in notes]})
    return {"bpm": 120.0, "offset": offset, "gogo": 0, "barline": 1,
            "branchInfo": info, "branches": out}


def test_branched_chart():
    none = [-1] * 6
    meas = [
        _gen3_measure(0.0, [[(1, 0.0), (1, 500.0), (1, 1000.0),
                             (1, 1500.0)]] * 3, none),
        _gen3_measure(2000.0, [[(1, 0.0)], [(4, 0.0), (4, 1000.0)],
                               [(7, 0.0), (8, 500.0), (7, 1000.0)]],
                      [40, 80, 40, 80, 40, 80]),
        _gen3_measure(4000.0, [[(1, 0.0)], [(4, 0.0)], [(7, 0.0)]], none),
    ]
    sht = gen3_convert.gen3_to_sht(meas)
    model = tja2sht.parse_sht(sht)
    assert sht_export.is_branched(model)

    g3 = gen3_convert.parse_gen3(sht_export.sht_to_gen3(sht, "oni"))
    assert g3[1]["branchInfo"] == [40, 80, 40, 80, 40, 80]
    assert [n["type"] for n in g3[1]["branches"][2]["notes"]] == [7, 8, 7]

    text = sht_export.sht_to_tja({"oni": sht})
    # 4 dons before the branch = 80 points; thresholds 40 / 80 points
    assert "#BRANCHSTART p,50,100" in text
    lines = text.splitlines()
    assert lines.count("#N") == lines.count("#E") == lines.count("#M") == 1
    back = tja2sht.parse_sht(tja2sht.convert_tja(text, "oni"))
    _div, rts = sht_export.routes(back)
    assert [[n["type"] for n in r[0]] for r in rts] == [[1, 1, 1, 1], [7, 8, 7], [7]]

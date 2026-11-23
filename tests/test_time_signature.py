"""Gen2 note positions are 1/48 of a 4/4 bar, whatever the time signature.

Official T14 charts put a 3/4 measure's notes on 0..35, never stretched over
0..47; both the Gen3 and the TJA converters must do the same.
"""
from taiko2x6.charts import gen3_convert, tja2sht


def _notes(sht, k):
    s = sht["tracks"][k]["subtracks"][0]
    return [n["measure"] for n in
            sht["notes"][s["noteIndexSt"]:s["noteIndexSt"] + s["noteCount"]]]


def _gen3_measure(offset, bpm, positions):
    notes = [{"type": 1, "pos": p, "item": 0, "pad": 0, "u10": 0, "u12": 0,
              "dur": 0.0} for p in positions]
    empty = {"count": 0, "unk": 0, "speed": 1.0, "notes": []}
    return {"bpm": bpm, "offset": offset, "gogo": 0, "barline": 1,
            "branchInfo": [-1] * 6,
            "branches": [{"count": len(notes), "unk": 0, "speed": 1.0,
                          "notes": notes}, empty, empty]}


def test_gen3_three_four_measure_uses_whole_bar_grid():
    # 120 bpm: a 4/4 bar is 2000 ms, a beat 500 ms. Measure 0 is 3/4.
    meas = [_gen3_measure(0.0, 120.0, [0.0, 500.0, 1000.0]),
            _gen3_measure(1500.0, 120.0, [0.0, 1000.0])]
    sht = tja2sht.parse_sht(gen3_convert.gen3_to_sht(meas))
    assert _notes(sht, 0) == [0, 12, 24]
    assert _notes(sht, 1) == [0, 24]


TJA = """TITLE:t
BPM:120
OFFSET:0
COURSE:Oni
LEVEL:1
#START
#MEASURE 3/4
111,
#MEASURE 5/4
11111,
#MEASURE 4/4
1,
#END
"""


def test_tja_odd_meters():
    sht = tja2sht.parse_sht(tja2sht.convert_tja(TJA, "oni"))
    tr = sht["tracks"]
    # 3/4, then 5/4 split into a bar + a 1/4 tail (no bar line), then 4/4.
    assert [round(t["time"]) for t in tr] == [0, 1500, 3500, 4000]
    assert [t["trackLine"] for t in tr] == [1, 1, 0, 1]
    assert _notes(sht, 0) == [0, 12, 24]
    assert _notes(sht, 1) == [0, 12, 24, 36]
    assert _notes(sht, 2) == [0]
    assert _notes(sht, 3) == [0]

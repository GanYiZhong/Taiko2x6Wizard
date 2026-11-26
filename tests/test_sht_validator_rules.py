"""Structure rules of sht_validator: no false positives on converter output,
and each rule fires on a deliberately broken chart."""
from __future__ import annotations

import copy
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_tja2sht_speed_equiv import gen_tja  # noqa: E402
from taiko2x6.charts import sht_validator as V, tja2sht  # noqa: E402

RULES = V.STRUCTURE_RULES


def _bad(issues):
    return [i for i in issues if i.rule in RULES and i.severity in (V.ERROR, V.WARN)]


@pytest.mark.parametrize("seed", range(60))
def test_converter_output_has_no_structure_errors_or_warnings(seed):
    txt = gen_tja(seed, measures=10 + seed * 2, branch=seed % 3 != 0)
    for name, data in tja2sht.tja_to_all_charts(txt).items():
        got = _bad(V.validate_sht(data, name.split("_")[0]))
        assert not got, (seed, name, [str(i) for i in got])


SIMPLE = """TITLE:x
BPM:120
OFFSET:-1
COURSE:Oni
LEVEL:8
BALLOON:10
#START
1010,
1020,
7000008000000000,
1111,
#END
"""
BRANCHED = """TITLE:x
BPM:120
OFFSET:-1
COURSE:Oni
LEVEL:8
#START
1111,
#BRANCHSTART p,50,80
#N
1010,
#E
1020,
#M
1212,
#BRANCHEND
1111,
#END
"""


def _model(txt=SIMPLE):
    return tja2sht.parse_sht(tja2sht.convert_tja(txt, "oni"))


def _issues(m, rule):
    out = V.validate_sht(tja2sht.serialize_sht(m), None)
    return [i for i in out if i.rule == rule]


def _first_note(m, typ):
    return next(n for n in m["notes"] if n["type"] == typ)


def _all(m, typ):
    return [n for n in m["notes"] if n["type"] == typ]


def test_baseline_is_clean():
    for txt in (SIMPLE, BRANCHED):
        m = _model(txt)
        assert not _bad(V.validate_sht(tja2sht.serialize_sht(m)))


def test_note_out_of_measure():
    m = _model()
    for n in _all(m, 1)[:2]:
        n["measure"] = 60
    got = _issues(m, "note.outOfMeasure")
    assert got and got[0].severity == V.ERROR and V.issue_measure(got[0]) is not None
    assert not _issues(m, "note.pastMeasure")


def test_note_past_measure_end():
    m = _model()
    m["tracks"][0]["time"] = 1000.0
    m["tracks"][1]["time"] = 1100.0            # measure 0 is only 100 ms long
    got = _issues(m, "note.pastMeasure")
    assert got and got[0].severity == V.WARN and V.issue_measure(got[0]) == 0


def test_same_slot_overlap_is_info():
    m = _model()
    t = m["tracks"][0]
    s = t["subtracks"][0]
    notes = m["notes"][s["noteIndexSt"]: s["noteIndexSt"] + s["noteCount"]]
    notes[1]["measure"] = notes[0]["measure"]
    got = _issues(m, "note.overlap")
    assert got and got[0].severity == V.INFO


def test_roll_overruns_next_note_and_chart_end():
    m = _model()
    b = _first_note(m, 10)
    b["longNoteLength"] += 1500
    got = _issues(m, "long.overrun")
    assert got and got[0].severity == V.WARN
    m2 = _model()
    for n in _all(m2, 10):
        n["longNoteLength"] = 600000
    assert any("end of the chart" in i.detail or "next note" in i.detail
               for i in _issues(m2, "long.overrun"))


def test_zero_length_roll_and_zero_hit_balloon():
    m = _model()
    b = _first_note(m, 10)
    b["longNoteLength"] = 0
    assert _issues(m, "long.zero")[0].severity == V.WARN
    m = _model()
    for n in _all(m, 10):
        n["balloonHitCount"] = 0
    assert _issues(m, "balloon.hits")[0].severity == V.WARN


def test_absurd_bpm():
    m = _model()
    m["tracks"][1]["bpm"] = 5000.0
    got = _issues(m, "track.bpmHigh")
    assert got and got[0].severity == V.WARN and V.issue_measure(got[0]) == 1


def test_negative_and_zero_measure_length():
    m = _model()
    m["tracks"][2]["time"] = m["tracks"][1]["time"] - 50
    got = _issues(m, "track.length")
    assert got and got[0].severity == V.WARN
    m = _model()
    m["tracks"][2]["time"] = m["tracks"][1]["time"]          # track 1 empty? no: has notes
    assert _issues(m, "track.length")


def test_gogo_blip_is_info():
    m = _model()
    m["tracks"][1]["gogoFlag"] = 1
    got = _issues(m, "track.gogoBlip")
    assert got and got[0].severity == V.INFO and V.issue_measure(got[0]) == 1


def test_first_note_before_zero_is_info():
    m = _model()
    for t in m["tracks"]:
        t["time"] -= 5000.0
    got = _issues(m, "note.beforeZero")
    assert got and got[0].severity == V.INFO


def test_branch_copies_disagree():
    m = _model(BRANCHED)
    t = next(t for t in m["tracks"]
             if t["subtracks"][1]["noteCount"] and
             t["subtracks"][1]["noteIndexSt"] != t["subtracks"][4]["noteIndexSt"])
    s = t["subtracks"][4]
    m["notes"][s["noteIndexSt"]]["type"] = 4 if m["notes"][s["noteIndexSt"]]["type"] == 1 else 1
    got = _issues(m, "branch.copyMismatch")
    assert got and got[0].severity == V.WARN and V.issue_measure(got[0]) is not None


def test_each_broken_variant_triggers_only_its_rule():
    """A mutation of the clean chart fires its own structure rule and no other
    structure ERROR/WARN."""
    def only(m, rule):
        got = {i.rule for i in V.validate_sht(tja2sht.serialize_sht(m))
               if i.rule in RULES and i.severity in (V.ERROR, V.WARN)}
        return got == {rule}
    m = _model(); _first_note(m, 10)["balloonHitCount"] = 0
    assert only(m, "balloon.hits")
    m = _model(); m["tracks"][1]["bpm"] = 2000.0
    assert only(m, "track.bpmHigh")
    m = _model(); _first_note(m, 10)["longNoteLength"] = 0
    assert only(m, "long.zero")

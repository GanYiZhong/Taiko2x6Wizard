"""Edge cases found in the converter review (TJA parsing, sht export,
chart_convert file handling)."""
import pytest

from taiko2x6.charts import (chart_convert, sht_export, sht_validator,
                             tja2sht)


def _tracks(tja, course="oni"):
    m = tja2sht.parse_sht(tja2sht.convert_tja(tja, course))
    return m, [(round(t["time"], 1), round(t["bpm"], 1), t["trackLine"])
               for t in m["tracks"]]


def _times(m):
    _d, rts = sht_export.routes(m)
    out = []
    for k, t in enumerate(m["tracks"]):
        bar = 240000.0 / t["bpm"]
        out += [round(t["time"] + n["measure"] / 48.0 * bar, 1) for n in rts[k][0]]
    return out


# --- tja2sht ---------------------------------------------------------------

def test_mid_measure_bpm_change_splits_the_measure():
    m, tr = _tracks("BPM:120\n#START\n1010\n#BPMCHANGE 240\n1010,\n1,\n#END")
    assert tr == [(0.0, 120.0, 1), (1000.0, 240.0, 0), (1500.0, 240.0, 1)]
    assert _times(m) == [0.0, 500.0, 1000.0, 1250.0, 1500.0]


def test_mid_measure_delay_and_gogo():
    m, tr = _tracks("BPM:120\n#START\n1000\n#DELAY 1\n#GOGOSTART\n1000,\n#END")
    assert [t[0] for t in tr] == [0.0, 2000.0]
    assert [t["gogoFlag"] for t in m["tracks"]] == [0, 1]
    assert _times(m) == [0.0, 2000.0]


def test_global_balloon_and_missing_course_is_oni():
    tja = "BPM:120\nBALLOON:9\n#START\n7008,\n#END"
    parsed = tja2sht.parse_tja(tja)
    assert list(parsed["courses"]) == [3]
    m, _ = _tracks(tja)
    assert {n["balloonHitCount"] for n in m["notes"]} == {9}


def test_per_course_bpm():
    _m, tr = _tracks("BPM:120\nCOURSE:Oni\nBPM:150\n#START\n1,\n1,\n#END")
    assert [t[0] for t in tr] == [0.0, 1600.0]


def test_style_double_keeps_one_chart():
    tja = ("BPM:120\nCOURSE:Oni\n#START\n1,\n#END\nSTYLE:Double\n"
           "#START P1\n2,\n#END\n#START P2\n3,\n#END")
    m, tr = _tracks(tja)
    assert len(tr) == 1 and {n["type"] for n in m["notes"]} == {1}


def test_unknown_course_does_not_replace_oni():
    tja = "COURSE:Oni\n#START\n1,\n#END\nCOURSE:Tower\n#START\n2,2,\n#END"
    assert list(tja2sht.parse_tja(tja)["courses"]) == [3]


def test_unknown_note_letters_keep_the_measure():
    _m, tr = _tracks("BPM:120\n#START\n1F,\n2,\n#END")
    assert len(tr) == 2


def test_dense_measure_stays_on_time():
    # 64 notes in a 48-slot bar: none may drift past its neighbours
    m, _ = _tracks("BPM:120\n#START\n" + "1" * 64 + ",\n1,\n#END")
    pos = [n["measure"] for n in m["notes"][:64]]
    assert max(pos) == 47 and pos.count(47) <= 2
    # a 1/4 measure's notes stay before the next bar
    m, _ = _tracks("BPM:120\n#START\n#MEASURE 1/4\n" + "1" * 16 +
                   ",\n#MEASURE 4/4\n1000,\n#END")
    assert max(n["measure"] for n in m["notes"][:16]) <= 11


def test_unterminated_roll_gets_a_length():
    m, _ = _tracks("BPM:120\n#START\n5000,\n1,\n#END")
    roll = [n for n in m["notes"] if n["type"] == tja2sht.NOTE_DRUMROLL]
    assert roll and all(n["longNoteLength"] == 2000 for n in roll)


BR = ("BPM:120\n#START\n1111,\n#SECTION\n#BRANCHSTART %s\n#N\n1,\n#E\n2,\n"
      "#M\n3,\n#BRANCHEND\n1,\n#END")


def test_section_right_before_branchstart_counts_first():
    m, _ = _tracks(BR % "p,50,80")
    assert list(m["tracks"][1]["bunkis"]) == [40, 64, 40, 64, 40, 64]


def test_branch_conditions_edge_cases():
    # nothing to count before the branch: never (stay on Normal)
    m, _ = _tracks("BPM:120\n#START\n#BRANCHSTART p,50,80\n#N\n1,\n#E\n2,\n"
                   "#M\n3,\n#BRANCHEND\n#END")
    assert list(m["tracks"][0]["bunkis"]) == [999] * 6
    # a score condition can't be expressed: never, with a warning
    warns = []
    sht = tja2sht.convert_tja(BR % "s,50000,90000", "oni", warn=warns.append)
    assert list(tja2sht.parse_sht(sht)["tracks"][1]["bunkis"]) == [999] * 6
    assert warns
    # a real threshold of 999 is not "never"
    assert tja2sht._bunki_values(("r", 999, 5), [0, 0, 0])[0] == 1000


def test_mismatched_routes_fall_back_with_warning():
    warns = []
    tja = ("BPM:120\n#START\n1,\n#BRANCHSTART p,50,80\n#N\n1,\n#E\n2,\n#M\n"
           "3,\n3,\n#BRANCHEND\n#END")
    sht = tja2sht.convert_tja(tja, "oni", warn=warns.append)
    assert not sht_export.is_branched(tja2sht.parse_sht(sht)) and warns


def test_branched_note_limit_is_per_route():
    body = "\n".join(["1" * 16 + ","] * 20)
    tja = ("BPM:120\nCOURSE:Normal\n#START\n1,\n#BRANCHSTART p,50,80\n#N\n%s\n"
           "#E\n%s\n#M\n%s\n#BRANCHEND\n#END" % (body, body, body))
    m = tja2sht.parse_sht(tja2sht.convert_tja(tja, "normal"))
    assert len(m["notes"]) > tja2sht.DIFFICULTY_NOTE_LIMIT["normal"]
    assert tja2sht.limit_note_count(m) == 2 * (1 + 320)
    issues = sht_validator.validate_sht(tja2sht.convert_tja(tja, "normal"),
                                        "normal")
    assert not [i for i in issues if i.severity == "ERROR"]


# --- sht_export ------------------------------------------------------------

def test_roll_end_is_not_overwritten_by_a_note():
    sht = tja2sht.parse_sht(tja2sht.convert_tja(
        "BPM:120\n#START\n5000000080001000,\n#END", "oni"))
    for n in sht["notes"]:
        if n["type"] == tja2sht.NOTE_DRUMROLL:
            n["longNoteLength"] = 1500      # ends exactly on the next don
    text = sht_export.sht_to_tja({"oni": tja2sht.serialize_sht(sht)})
    back = tja2sht.parse_sht(tja2sht.convert_tja(text, "oni"))
    kinds = sorted(n["type"] for n in back["notes"])
    assert kinds.count(tja2sht.NOTE_DRUMROLL) == 2
    assert kinds.count(tja2sht.NOTE_DON) == 2
    assert all(n["longNoteLength"] > 0 for n in back["notes"]
               if n["type"] == tja2sht.NOTE_DRUMROLL)


def test_hp_values_never_zero():
    for n in (1, 669, 2400, 2500, 2501, 9000):
        good, ok, _bad = sht_export.hp_values("Oni", 10, n)
        assert good > 0 and ok > 0


def test_tja_header_is_one_line_each():
    sht = tja2sht.convert_tja("BPM:120\n#START\n1,\n#END", "oni")
    text = sht_export.sht_to_tja({"oni": sht}, title="a\nCOURSE:Easy")
    assert "\nCOURSE:Easy" not in text.replace("COURSE:Oni", "")


# --- chart_convert ---------------------------------------------------------

TJA = "TITLE:Demo\nBPM:120\nCOURSE:Oni\nLEVEL:inf\n#START\n1111,\n#END\n"


def test_inputs_are_never_overwritten(tmp_path):
    src = tmp_path / "demo.tja"
    src.write_text(TJA, encoding="utf-8")
    before = src.read_bytes()
    _s, written, errors = chart_convert.convert(
        [src], tmp_path, {"tja", "bin"}, log=lambda s: None)
    assert src.read_bytes() == before
    assert any("overwrite" in e for e in errors)
    # the other format still got written
    assert [p.name for p in written] == ["demo_m.bin"]


def test_nothing_written_is_an_error(tmp_path):
    src = tmp_path / "empty.tja"
    src.write_text("TITLE:x\nBPM:120\n", encoding="utf-8")
    _s, written, errors = chart_convert.convert(
        [src], tmp_path / "out", {"tja"}, log=lambda s: None)
    assert not written and errors
    src2 = tmp_path / "demo.tja"
    src2.write_text(TJA, encoding="utf-8")
    _s, written, errors = chart_convert.convert(
        [src2], tmp_path / "out", {"vag"}, log=lambda s: None)
    assert not written and errors


def test_out_id_merges_inputs(tmp_path):
    a = tmp_path / "demo.tja"
    a.write_text(TJA, encoding="utf-8")
    b = tmp_path / "other.tja"
    b.write_text(TJA.replace("COURSE:Oni", "COURSE:Easy"), encoding="utf-8")
    songs, written, errors = chart_convert.convert(
        [a, b], tmp_path / "out", {"tja"}, out_id="10tai", log=lambda s: None)
    assert not errors and len(songs) == 1
    assert set(songs[0]["charts"]) == {"oni", "easy"}
    assert [p.name for p in written] == ["10tai.tja"]
    assert written[0].read_bytes().startswith(b"\xef\xbb\xbf")   # UTF-8 BOM


def test_course_from_file_name_before_folder():
    assert chart_convert._course_of("fumen/ab_e/ab_e_m.bin") == "oni"
    assert chart_convert._course_of("x/abc1p_h/sht") == "hard"
    assert chart_convert.detect("x/abc_m_1.bin") is None       # duet chart


def test_cli_validates_players(tmp_path):
    src = tmp_path / "demo.tja"
    src.write_text(TJA, encoding="utf-8")
    with pytest.raises(SystemExit):
        chart_convert.main([str(src), "--to", "sht", "-o", str(tmp_path / "o"),
                            "--players", "3p"])
    assert chart_convert.main([str(src), "--to", "sht", "-o",
                               str(tmp_path / "o"), "--players", "1P, 2p"]) == 0
    assert (tmp_path / "o" / "demo1p_m" / "sht").is_file()
    assert (tmp_path / "o" / "demo2p_m" / "sht").is_file()

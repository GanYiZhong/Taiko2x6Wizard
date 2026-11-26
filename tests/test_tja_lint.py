"""TJA check (taiko2x6/charts/tja_lint.py): every rule fires on a crafted TJA,
none fires on clean synthetic ones, and fixes give a TJA that converts."""
import os
import random

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from taiko2x6.charts import tja2sht, tja_lint  # noqa: E402

@pytest.fixture(autouse=True)
def _english():
    from taiko2x6.app import i18n
    old = i18n._lang
    i18n._lang = "en"
    yield
    i18n._lang = old


GOOD = """TITLE:Good
BPM:120
OFFSET:-1.5
WAVE:a.ogg

COURSE:Oni
LEVEL:8
BALLOON:5
#START
1020,
7008,
#END
"""


def rules(text, **kw):
    return {i.rule for i in tja_lint.lint_tja(text, **kw)}


def one(text, rule, **kw):
    got = [i for i in tja_lint.lint_tja(text, **kw) if i.rule == rule]
    assert got, (rule, [i.rule for i in tja_lint.lint_tja(text, **kw)])
    return got[0]


def gen_clean(seed):
    r = random.Random(seed)
    out = ["TITLE:Clean %d" % seed, "BPM:%d" % r.randint(90, 200),
           "OFFSET:-%.2f" % r.random(), "WAVE:a.ogg", ""]
    for ci, c in enumerate(("Easy", "Normal", "Hard", "Oni", "Edit")):
        meas = []

        def measure(balloons):
            n = r.choice([4, 8, 16, 12])
            s = []
            for k in range(n):
                x = r.random()
                if x < .5:
                    s.append("0")
                elif x < .9:
                    s.append(r.choice("1234AB"))
                else:
                    s.append("0")
            if r.random() < .3 and n >= 4:
                s[0] = r.choice("579")
                if s[0] in "79":
                    balloons.append(r.randint(3, 20))
                s[-1] = "8"
            return "".join(s) + ","
        balloons = []
        body = ["#START"]
        for _ in range(r.randint(4, 12)):
            if r.random() < .15:
                body.append("#BPMCHANGE %d" % r.randint(80, 200))
            body.append(measure(balloons))
        if r.random() < .5:
            body.append("#BRANCHSTART p,50,90")
            k = r.randint(1, 3)
            route_ball = []
            for tag in "NEM":
                body.append("#" + tag)
                for _ in range(k):
                    body.append(measure(route_ball))
            body.append("#BRANCHEND")
            balloons += route_ball
        body.append(measure(balloons))
        body.append("#END")
        out += ["COURSE:" + c, "LEVEL:%d" % (ci + 3)]
        if balloons:
            out.append("BALLOON:" + ",".join(map(str, balloons)))
        out += body + [""]
    return "\n".join(out)


@pytest.mark.parametrize("seed", range(30))
def test_clean_synthetic_tjas_have_no_issues(seed, tmp_path):
    (tmp_path / "a.ogg").write_bytes(b"x")
    txt = gen_clean(seed)
    tja2sht.tja_to_all_charts(txt)                     # converter-valid
    assert tja_lint.lint_tja(txt, base_dir=tmp_path, raw=txt.encode()) == []


def test_good():
    assert tja_lint.lint_tja(GOOD) == []


def test_course_and_level_rules():
    assert "unknown_course" in rules(GOOD.replace("COURSE:Oni", "COURSE:Insane"))
    assert "unknown_course" not in rules(GOOD.replace("COURSE:Oni", "COURSE:Dan"))
    i = one(GOOD.replace("COURSE:Oni\n", ""), "no_course")
    assert i.line == 8
    assert "no_level" in rules(GOOD.replace("LEVEL:8\n", ""))
    assert one(GOOD.replace("LEVEL:8", "LEVEL:eight"), "bad_level").line == 7


def test_balloon_rules_and_fix():
    short = GOOD.replace("BALLOON:5", "BALLOON:")
    i = one(short, "balloon_short")
    assert i.fixable and "5" in i.fix_text
    fixed, _ = tja_lint.apply_fixes(short, [i])
    assert "BALLOON:5\n" in fixed and not rules(fixed)
    # pads with the last value
    two = GOOD.replace("BALLOON:5", "BALLOON:30").replace("7008", "7878")
    i = one(two, "balloon_short")
    fixed, _ = tja_lint.apply_fixes(two, [i])
    assert "BALLOON:30,30" in fixed and not rules(fixed)
    # no BALLOON line at all: one is inserted before #START
    none = GOOD.replace("BALLOON:5\n", "")
    i = one(none, "balloon_missing")
    fixed, _ = tja_lint.apply_fixes(none, [i])
    assert fixed.index("BALLOON:5") < fixed.index("#START")
    assert not rules(fixed)
    # longer: info only, no fix
    i = one(GOOD.replace("BALLOON:5", "BALLOON:5,6,7"), "balloon_long")
    assert i.severity == tja_lint.INFO and not i.fixable


def test_outside_and_end_rules():
    i = one(GOOD + "1111,\n2222,\n", "outside_chart")
    assert i.line == 13 and "2" in i.message
    assert "no_end" in rules(GOOD.replace("#END\n", ""))
    assert "stray_end" in rules(GOOD + "#END\n")
    assert "outside_chart" not in rules(GOOD)


def test_unterminated_measure_fix():
    bad = GOOD.replace("7008,\n", "7008 // note\n")
    i = one(bad, "unterminated_measure")
    assert i.line == 11 and i.fixable
    fixed, _ = tja_lint.apply_fixes(bad, [i])
    assert "7008, // note" in fixed and not rules(fixed)
    assert (tja2sht.convert_tja(fixed, "oni")
            == tja2sht.convert_tja(bad, "oni"))


def test_roll_rules():
    open_roll = GOOD.replace("7008,", "5000,")
    assert one(open_roll, "roll_open").line == 11
    assert "roll_nested" in rules(GOOD.replace("7008,", "5600,\n8000,"))
    assert "roll_open" not in rules(GOOD)


BRANCH = """TITLE:B
BPM:120
OFFSET:-1
COURSE:Oni
LEVEL:8
#START
1000,
#BRANCHSTART p,50,90
#N
1000,
#E
1000,
#M
1000,
#BRANCHEND
1000,
#END
"""


def test_branch_rules():
    assert tja_lint.lint_tja(BRANCH) == []
    miss = BRANCH.replace("#M\n1000,\n", "")
    i = one(miss, "branch_missing_route")
    assert i.line == 8 and "#M" in i.message
    long = BRANCH.replace("#M\n1000,", "#M\n1000,\n1000,")
    assert "#M 2" in one(long, "branch_length").message
    assert "branch_open" in rules(BRANCH.replace("#BRANCHEND\n", ""))


def test_odd_chars_and_fix():
    i = one(GOOD.replace("1020,", "10x0,"), "odd_chars")
    assert not i.fixable and "x" in i.message
    full = GOOD.replace("1020,", "１０２０，")
    i = one(full, "odd_chars")
    assert i.fixable
    fixed, _ = tja_lint.apply_fixes(full, [i])
    assert fixed == GOOD
    # spaces and A/B are fine
    assert "odd_chars" not in rules(GOOD.replace("1020,", "10 AB,"))


def test_offset_sign():
    # chart time of the first note is 0 s: a positive OFFSET puts it before 0
    i = one(GOOD.replace("OFFSET:-1.5", "OFFSET:1.5"), "offset_sign")
    assert i.line == 3 and "-1.5" in i.suggestion
    assert not i.fixable
    assert "offset_sign" not in rules(GOOD)
    # a first note late enough in the chart is fine with a small positive
    late = GOOD.replace("OFFSET:-1.5", "OFFSET:1.5").replace(
        "#START\n", "#START\n0000,\n0000,\n")
    assert "offset_sign" not in rules(late)


def test_wave_rules(tmp_path):
    assert "wave_missing" in rules(GOOD, base_dir=tmp_path)
    (tmp_path / "a.ogg").write_bytes(b"x")
    assert not rules(GOOD, base_dir=tmp_path)
    assert "no_wave" in rules(GOOD.replace("WAVE:a.ogg\n", ""), base_dir=tmp_path)
    assert "wave_missing" not in rules(GOOD)           # no folder: no check


def test_encoding_rules():
    t = GOOD.replace("Good", "日本語")
    sj = t.encode("cp932")
    assert "encoding_sjis" in rules(t, raw=sj)
    assert "encoding_sjis" not in rules(t, raw=t.encode("utf-8"))
    assert "encoding_unknown" in rules(GOOD, raw=b"\xff\xfe\xfa\x81\x00")
    # UTF-8 text decoded as cp932: mojibake in TITLE, fixed by re-reading
    moji = t.encode("utf-8").decode("cp932", errors="ignore")
    moji = GOOD.replace("Good", "日本語".encode("utf-8").decode("cp932", "replace"))
    if "�" not in moji:
        i = one(moji, "mojibake")
        assert i.fixable
        fixed, _ = tja_lint.apply_fixes(moji, [i])
        assert "TITLE:日本語" in fixed
    # latin-1 style mojibake
    l1 = GOOD.replace("Good", "日本語".encode("utf-8").decode("latin-1"))
    i = one(l1, "mojibake")
    fixed, _ = tja_lint.apply_fixes(l1, [i])
    assert "TITLE:日本語" in fixed
    assert "mojibake" not in rules(t)


def test_fixes_differ_only_where_intended():
    bad = GOOD.replace("BALLOON:5", "BALLOON:").replace("7008,", "7008")
    issues = tja_lint.lint_tja(bad)
    fixed, applied = tja_lint.apply_fixes(bad, issues)
    assert {i.rule for i in applied} == {"balloon_short", "unterminated_measure"}
    a, b = bad.split("\n"), fixed.split("\n")
    assert [i for i, (x, y) in enumerate(zip(a, b)) if x != y] == [7, 10]
    assert tja_lint.lint_tja(fixed) == []
    tja2sht.convert_tja(fixed, "oni")
    # CRLF files keep their line endings
    crlf = bad.replace("\n", "\r\n")
    f2, _ = tja_lint.apply_fixes(crlf, tja_lint.lint_tja(crlf))
    assert f2.replace("\r\n", "\n") == fixed and "\r\n" in f2


def test_write_fixed_copy_never_overwrites(tmp_path):
    p = tmp_path / "s.tja"
    p.write_text(GOOD, encoding="utf-8")
    d1 = tja_lint.write_fixed_copy(p, "A")
    d2 = tja_lint.write_fixed_copy(p, "B")
    assert d1.name == "s.fixed.tja" and d2.name == "s.fixed2.tja"
    assert p.read_text(encoding="utf-8") == GOOD
    sj = tja_lint.write_fixed_copy(p, "日本", "cp932")
    assert sj.read_bytes().decode("cp932") == "日本"


def test_summary_shows_line_numbers():
    from taiko2x6.songs import song_dialog_ui as UI
    s = UI.summarize_tja(GOOD.replace("BALLOON:5", "BALLOON:"))
    assert any("BALLOON" in w for w in s["warnings"])
    assert any(w.startswith("line ") for w in s["warnings"])
    assert UI.summarize_tja(GOOD)["warnings"] == []


def test_dialog_checks_and_saves_fixed_copy(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QMessageBox
    QApplication.instance() or QApplication([])
    from taiko2x6.charts.tja_check_dialog import TjaCheckDialog
    p = tmp_path / "s.tja"
    bad = GOOD.replace("BALLOON:5", "BALLOON:")
    p.write_text(bad, encoding="utf-8")
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    d = TjaCheckDialog([str(p)])
    try:
        assert d.tbl.rowCount() >= 1 and d.b_save.isEnabled()
        d._confirm = lambda body: False
        assert d.save_fixed() == []
        assert not (tmp_path / "s.fixed.tja").exists()
        d._confirm = lambda body: True
        out = d.save_fixed()
        assert out == [str(tmp_path / "s.fixed.tja")]
        assert p.read_text(encoding="utf-8") == bad
        assert tja_lint.lint_tja((tmp_path / "s.fixed.tja").read_text(
            encoding="utf-8")) == []
    finally:
        d.close()
        d.deleteLater()

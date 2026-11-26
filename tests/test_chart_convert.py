"""The one-stop chart converter: TJA <-> sht <-> Gen3 .bin."""
from taiko2x6.charts import chart_convert, tja2sht

TJA = """TITLE:Demo
BPM:140
WAVE:demo.ogg
OFFSET:-0.5

COURSE:Oni
LEVEL:9
BALLOON:5

#START
1111,
#BRANCHSTART p,50,90
#N
1010,
#E
2020,
#M
33304440,
#BRANCHEND
70000008,
#END

COURSE:Easy
LEVEL:2
#START
1000,
1000,
1000,
#END
"""


def _notes(sht):
    m = tja2sht.parse_sht(sht)
    return [[(n["type"], n["measure"]) for n in m["notes"]]]


def test_tja_to_sht_bin_and_back(tmp_path):
    src = tmp_path / "demo.tja"
    src.write_text(TJA, encoding="utf-8")
    out = tmp_path / "out"
    songs, written, errors = chart_convert.convert(
        [src], out, {"sht", "bin"}, players=("1p", "2p"), out_id="demo1",
        log=lambda s: None)
    assert not errors and len(songs) == 1
    names = sorted(p.relative_to(out).as_posix() for p in written)
    assert names == ["demo1/demo1_e.bin", "demo1/demo1_m.bin",
                     "demo11p_e/sht", "demo11p_m/sht",
                     "demo12p_e/sht", "demo12p_m/sht"]
    assert chart_convert.detect(out / "demo1" / "demo1_m.bin") == "bin"
    assert chart_convert.detect(out / "demo11p_m" / "sht") == "sht"

    # the Gen3 files on their own give back the same charts
    again = chart_convert.group([chart_convert.load(p)
                                 for p in (out / "demo1").glob("*.bin")])
    assert len(again) == 1 and again[0]["id"] == "demo1"
    first = songs[0]["charts"]
    for course in ("easy", "oni"):
        assert _notes(again[0]["charts"][course]) == _notes(first[course])

    # and a TJA from them keeps the branch and both courses
    chart_convert.write(again[0], out, {"tja"}, log=lambda s: None)
    text = (out / "demo1.tja").read_text(encoding="utf-8")
    assert "COURSE:Oni" in text and "COURSE:Easy" in text
    lines = text.splitlines()
    assert "#M" in lines and lines.count("#BRANCHSTART p,50,90") == 1
    back = tja2sht.convert_tja(text, "oni")
    assert _notes(back) == _notes(first["oni"])


def test_levels_and_grouping(tmp_path):
    src = tmp_path / "demo.tja"
    src.write_text(TJA, encoding="utf-8")
    pc = chart_convert.load(src)
    assert pc["levels"] == {"oni": 9, "easy": 2}
    assert pc["title"] == "Demo" and pc["wave"] == "demo.ogg"
    assert set(pc["charts"]) == {"oni", "easy"}


def test_dialog_state_and_persistence(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from taiko2x6.app import appconfig
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    QApplication.instance() or QApplication([])
    d = chart_convert.ChartConverterDialog()
    assert not d.b_go.isEnabled()                  # nothing queued
    f = tmp_path / "a.tja"
    f.write_text("x")
    d.add_paths([str(f)])
    assert d.b_go.isEnabled() and d.ed_out.text()
    d.lst.selectAll()
    d._remove()
    assert not d.b_go.isEnabled()
    d._save_settings("O:/out", {"sht", "tja"}, ("1p",))
    d2 = chart_convert.ChartConverterDialog()
    assert d2.ed_out.text() == "O:/out"
    assert d2.ck["sht"].isChecked() and not d2.ck["vag"].isChecked()
    assert not d2.ck_2p.isChecked()

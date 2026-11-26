"""Results table of the chart converter: statuses, filter, report text."""
import pytest

from taiko2x6.charts import chart_convert

OK = """TITLE:Fine
BPM:120
WAVE:a.ogg
COURSE:Oni
LEVEL:8
#START
1111,
1010,
#END
"""

SCORE = """TITLE:Score
BPM:120
WAVE:b.ogg
COURSE:Oni
LEVEL:8
#START
1111,
#BRANCHSTART s,100,200
#N
1010,
#E
2020,
#M
3030,
#BRANCHEND
1000,
#END
"""

MISMATCH = """TITLE:Mismatch
BPM:120
WAVE:c.ogg
COURSE:Oni
LEVEL:8
#START
1111,
#BRANCHSTART p,50,90
#N
1010,
#E
2020,
#M
3030,
3030,
#BRANCHEND
1000,
#END
"""


@pytest.fixture
def run(tmp_path):
    files = {}
    for name, text in (("ok", OK), ("score", SCORE), ("mism", MISMATCH),
                       ("empty", "TITLE:Nothing\nBPM:120\n")):
        f = tmp_path / (name + ".tja")
        f.write_text(text, encoding="utf-8")
        files[name] = f
    report = []
    chart_convert.convert([str(f) for f in files.values()], tmp_path / "out",
                          {"tja"}, log=lambda s: None, report=report)
    return files, {r["input"]: r for r in report}, tmp_path


def test_report_statuses_and_lines(run):
    files, rows, tmp = run
    r = lambda k: rows[str(files[k])]
    assert r("ok")["status"] == "ok" and r("ok")["messages"] == []
    assert r("ok")["outputs"] and r("ok")["courses"] == ["oni"]
    assert r("score")["status"] == "warning"
    assert r("score")["messages"][0]["line"] == 8     # the #BRANCHSTART s line
    assert r("mism")["status"] == "warning"
    assert "different lengths" in r("mism")["messages"][0]["text"]
    assert r("empty")["status"] == "error" and not r("empty")["outputs"]


def test_report_does_not_change_output(run, tmp_path):
    files, _rows, tmp = run
    out2 = tmp_path / "out2"
    chart_convert.convert([str(files["score"])], out2, {"tja"},
                          log=lambda s: None)
    assert (out2 / "score.tja").read_bytes() == (
        tmp / "out" / "score.tja").read_bytes()


def test_dialog_table_filter_and_report(run):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    files, rows, tmp = run
    d = chart_convert.ChartConverterDialog()
    try:
        d.set_results(list(rows.values()))
        assert d.tbl.rowCount() == 4
        st = lambda: sorted(d.tbl.item(i, 0).data(0x100)
                            for i in range(d.tbl.rowCount())
                            if not d.tbl.isRowHidden(i))
        assert st() == ["error", "ok", "warning", "warning"]
        d.cb_filter.setCurrentIndex(d.cb_filter.findData("warning"))
        assert st() == ["warning", "warning"]
        d.cb_filter.setCurrentIndex(d.cb_filter.findData("error"))
        assert st() == ["error"]
        assert "empty.tja" in d.report_text()
        assert "ok.tja" not in d.report_text().replace("empty.tja", "")
        d.cb_filter.setCurrentIndex(d.cb_filter.findData("all"))
        text = d.report_text()
        for n in ("ok.tja", "score.tja", "mism.tja", "empty.tja"):
            assert n in text
        assert d.lb_summary.text()
        row = [i for i in range(4) if d.tbl.item(i, 1).text() == "score.tja"][0]
        assert "8" in d.tbl.item(row, 1).toolTip()
        assert d.log.isHidden()                      # collapsed under Details
        d.b_details.setChecked(True)
        assert not d.log.isHidden()
        d._copy_report()
        assert QApplication.clipboard().text() == text
        seen = []
        d._reveal = seen.append
        orow = [i for i in range(4) if d.tbl.item(i, 1).text() == "ok.tja"][0]
        d._cell_click(orow, 2)
        assert seen and seen[0].endswith("ok.tja")
    finally:
        d.close()
        d.deleteLater()

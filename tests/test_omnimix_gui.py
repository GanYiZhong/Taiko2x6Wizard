"""Omnimix dialog: input validation, per-source summary, remembered settings."""
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("PySide6")

from taiko2x6.app import appconfig  # noqa: E402
from taiko2x6.songs import omnimix_gui as G  # noqa: E402


def test_input_error(tmp_path):
    f = tmp_path / "a.img"; f.write_bytes(b"x")
    src = [{"kind": "img", "img": str(f), "partition": "p", "label": "A"}]
    assert G.input_error("", True, src)
    assert G.input_error(str(tmp_path / "no.img"), True, src)
    assert G.input_error(str(tmp_path), False, src)             # no DATA.000
    assert G.input_error(str(f), True, [])
    gone = [{"kind": "iso", "iso": str(tmp_path / "x.iso"), "label": "Gone"}]
    assert "Gone" in G.input_error(str(f), True, gone)
    assert G.input_error(str(f), True, src) == ""


def test_source_summary():
    plan = NS(items=[NS(source_label="A"), NS(source_label="A"), NS(source_label="B")],
              skipped_dupes={"x": ["B"], "y": ["B", "C"]},
              skipped_titles={"z": ("A", "t", "q")})
    specs = [NS(label="A"), NS(label="B"), NS(label="C")]
    assert G.source_summary(plan, specs) == [("A", 2, 1), ("B", 1, 2), ("C", 0, 1)]


def test_settings_round_trip_and_reorder(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    d = G.OmnimixDialog()
    d.ed_timg.setText("T.img")
    d._sources = [{"kind": "iso", "iso": "a.iso", "label": "A"},
                  {"kind": "iso", "iso": "b.iso", "label": "B"}]
    d._refresh_sources(); d.lst.setCurrentRow(1); d._move(-1)
    d.cb_audio.setCurrentIndex(1)
    d._save_settings()
    e = G.OmnimixDialog()
    assert e.ed_timg.text() == "T.img"
    assert [s["label"] for s in e._sources] == ["B", "A"]
    assert e.cb_audio.currentIndex() == 1
    assert e.lst.count() == 2 and not e.b_build.isEnabled()


def _plan():
    return NS(items=[NS(sid="a1", source_label="A", stars=[1, 2, 3, -1], genre=0,
                        play=[1], preview=5),
                     NS(sid="a2", source_label="B", stars=[4, 5, 6, 7], genre=99,
                        play=[1], preview=None),
                     NS(sid="a3", source_label="B", stars=[1, 1, 1, 1], genre=1,
                        play=None, preview=None)],
              skipped_dupes={"d1": ["B"]}, skipped_titles={"t1": ("B", "Same", "a1")},
              genre_fixes={"x": 1}, star_fixes={"x": [1, 2, 3, 4]},
              play_fixes={"x": [1]}, preview_fixes={"x": 9},
              titles={"a1": "Song One"}, play_estimated={"a2", "x"})


def test_plan_rows_and_csv(tmp_path):
    from taiko2x6.app import i18n
    i18n._lang = "en"
    p = _plan()
    rows = G.plan_song_rows(p)
    assert rows[0][:5] == ("a1", "Song One", "A", "J-POP", "1/2/3/-")
    assert rows[0][5] == "from source" and rows[0][6] == "from source"
    assert rows[1][5] == "estimated from chart" and rows[1][6] == "template default"
    assert rows[2][5] == "template default"
    assert G.plan_skipped_rows(p) == [
        ("d1", "", "B", "same id already added", "d1"),
        ("t1", "Same", "B", "same title as another id", "a1")]
    assert [r[2] for r in G.plan_fix_rows(p)] == ["genre", "stars", "gauge", "preview start"]
    out = tmp_path / "p.csv"
    assert G.write_plan_csv(p, out) == 3 + 2 + 4
    assert len(out.read_text(encoding="utf-8-sig").splitlines()) == 10


def test_plan_details_dialog_filters():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    d = G.PlanDetailsDialog(_plan(), [NS(label="A"), NS(label="B")], source="B")
    try:
        t = d.tables[0]
        vis = lambda: [r for r in range(t.rowCount()) if not t.isRowHidden(r)]
        assert len(vis()) == 2
        d.ed_filter.setText("a3")
        assert len(vis()) == 1
        d.cb_source.setCurrentIndex(0); d.ed_filter.setText("")
        assert len(vis()) == 3
    finally:
        d.close(); d.deleteLater()

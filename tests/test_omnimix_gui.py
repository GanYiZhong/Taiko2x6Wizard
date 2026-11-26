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

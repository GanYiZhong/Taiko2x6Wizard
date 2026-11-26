"""Chart timeline widget and the table-based SHT validator dialog."""
import pytest
from PySide6.QtCore import QMimeData, QUrl, Qt
from PySide6.QtWidgets import QApplication

from taiko2x6.charts import chart_preview, chart_timeline, sht_validator, tja2sht

TJA = """TITLE:T
BPM:120
WAVE:t.ogg

COURSE:Oni
LEVEL:8
BALLOON:5
#START
1100,
#GOGOSTART
2200,
#BPMCHANGE 150
#SCROLL 1.5
1020,
7008,
#GOGOEND
#END
"""


@pytest.fixture
def app(monkeypatch):
    from taiko2x6.app import i18n
    monkeypatch.setattr(i18n, "_lang", "en")
    return QApplication.instance() or QApplication([])


def _sht():
    return tja2sht.convert_tja(TJA, "oni")


def test_timeline_model_zoom_and_highlight(app):
    v = chart_timeline.ChartTimelineView()
    try:
        v.set_model(chart_preview.parse_chart(_sht()))
        tl = v.timeline
        assert tl.measure_count() == 4 and tl.lane_count() == 1
        w = tl.width()
        tl.set_px_per_measure(tl.px_per_measure() * 2)
        assert tl.width() > w
        v.set_markers({1: "ERROR"})
        v.show_measure(2)
        assert tl.highlight() == 2
        v.show_measure(99)                      # out of range: ignored
        assert tl.highlight() == 2
        v.resize(500, 200)
        v.show()
        app.processEvents()
        assert not v.grab().isNull()
        v.set_model(None)
        assert tl.measure_count() == 0
    finally:
        v.close()
        v.deleteLater()


def test_validator_table_filter_and_jump(app, tmp_path):
    data = bytearray(_sht())
    # break track 1's bpm so there is an ERROR that names a measure
    import struct
    off = 16 + tja2sht.TRACK_NEW_SIZE + 4
    struct.pack_into("<f", data, off, -5.0)
    d = sht_validator.ShtValidatorDialog()
    try:
        d._results = [{"label": "x_m.sht", "guess": "oni", "data": bytes(data)}]
        d._check(d._results[0])
        d.cb_file.addItem("x_m.sht")
        d._fill()
        rows = d.table.rowCount()
        assert rows >= 2
        assert d.table.item(0, 0).text().endswith("ERROR")
        assert "/" in d.lbl_limit.text()
        d.cb_sev.setCurrentIndex(1)             # errors only
        assert 1 <= d.table.rowCount() < rows
        d.cb_sev.setCurrentIndex(0)
        bpm_row = next(r for r in range(d.table.rowCount())
                       if "track.bpm" in d.table.item(r, 3).text())
        assert d.table.item(bpm_row, 1).text() == "2"
        d._jump(bpm_row)
        assert d.view.timeline.highlight() == 1
        assert d.table.item(bpm_row, 4).text()
    finally:
        d.close()
        d.deleteLater()


def test_validator_accepts_dropped_tja_without_writing(app, tmp_path):
    p = tmp_path / "song.tja"
    p.write_text(TJA, encoding="utf-8")
    before = sorted(x.name for x in tmp_path.iterdir())
    d = sht_validator.ShtValidatorDialog()
    try:
        d.load_paths([str(p)])
        assert d._results and d._results[0]["diff"] == "oni"
        assert d.table.rowCount() >= 1
        assert "VERDICT" in d.lbl_verdict.text()
        d.load_paths([str(tmp_path / "nothing.txt")])
        assert not d._results and d._load_errors
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p))])
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QDropEvent
        ev = QDropEvent(QPointF(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        d.dropEvent(ev)
        assert d._results
    finally:
        d.close()
        d.deleteLater()
    assert sorted(x.name for x in tmp_path.iterdir()) == before


def test_note_limit_info_busiest_route(app):
    info = sht_validator.note_limit_info(_sht(), "oni")
    n, limit, route, branched = info
    assert limit == tja2sht.DIFFICULTY_NOTE_LIMIT["oni"] and n > 0 and not branched
    assert sht_validator.note_limit_info(_sht(), None) is None

"""Preview usability: audio transport, image viewer zoom, flipbook, chart summary."""
import numpy as np
import pytest
from PySide6.QtGui import QPixmap, QColor
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication

from taiko2x6.charts import tja2sht, chart_preview


@pytest.fixture
def app(monkeypatch):
    from taiko2x6.app import i18n
    monkeypatch.setattr(i18n, "_lang", "en")      # assertions read English text
    return QApplication.instance() or QApplication([])


def _pix(w, h):
    p = QPixmap(w, h)
    p.fill(QColor(200, 30, 30))
    return p


def test_image_viewer_zoom_modes(app):
    from taiko2x6.graphics.imageview import ImageViewer
    v = ImageViewer()
    v.resize(400, 300)
    v.show()
    app.processEvents()
    assert not v.has_image() and not v.b_fit.isEnabled()
    v.set_pixmap(_pix(100, 50), "100 x 50 px")
    assert v.has_image() and v.b_fit.isEnabled()
    assert v.zoom() > 1.0                      # small image: fit magnifies
    v.actual_size()
    assert v.zoom() == 1.0
    v.zoom_by(2)
    assert v.zoom() == 2.0
    v.fit()
    assert "fit" in v.lbl_zoom.text()
    v.set_message("nope")
    assert not v.has_image()
    v.close()
    v.deleteLater()


def test_audio_player_loop_and_stop(app):
    from taiko2x6.audio.audioplayer import AudioPlayer
    a = AudioPlayer()
    pcm = np.zeros((4000, 1), dtype=np.int16)
    a.load_pcm(pcm, 8000, 1)
    a.chk_loop.setChecked(True)
    assert a.player.loops() == QMediaPlayer.Infinite
    a.chk_loop.setChecked(False)
    assert a.player.loops() == 1
    a.vol.setValue(40)
    assert a.lbl_vol.text() == "40%"
    a.stop()
    assert a.slider.value() == 0
    a.close()
    a.deleteLater()


def test_flipbook_speed_and_keys(app):
    from taiko2x6.graphics.flipbook_player import FlipbookPlayer
    d = FlipbookPlayer([("a", _pix(8, 8)), ("b", _pix(8, 8)), ("c", _pix(8, 8))], "t")
    d.sp_fps.setValue(10)
    d.cb_speed.setCurrentIndex(3)              # 2x
    d._apply_fps()
    assert d.timer.interval() == 50
    d._step(1)
    assert d.idx == 1 and "2/3" in d.lbl.text()
    d._show_frame(-1)
    assert d.idx == 2
    d.close()
    d.deleteLater()


def _sht():
    tracks = [{"time": 0.0 + i * 2000, "bpm": 120.0 + i * 10, "trackLine": 1,
               "gogoFlag": 0, "_unk": 0, "bunkis": [-1] * 6,
               "scrollSpeeds": [1.0] * 6,
               "subtracks": [{"noteIndexSt": i * 2, "noteCount": 2, "pointGain": 0}]
               + [{"noteIndexSt": 0, "noteCount": 0, "pointGain": 0}] * 5}
              for i in range(2)]
    notes = [{"type": t, "measure": 0, "balloonHitCount": 0, "longNoteLength": -1}
             for t in (1, 4, 1, 6)]
    return tja2sht.serialize_sht({"noteOffset": 16 + 2 * tja2sht.TRACK_NEW_SIZE,
                                  "padding": 0, "isOld": False, "tracks": tracks,
                                  "notes": notes, "_gap": b"", "_tail": b""})


def test_chart_summary(app):
    txt = chart_preview.summarize_sht("abc_m.sht", _sht())
    assert "oni" in txt
    assert "120 - 130" in txt
    assert "3 hits, 1 rolls, 0 balloons" in txt
    with pytest.raises(ValueError):
        chart_preview.summarize_sht("x.sht", b"short")
    assert chart_preview.is_chart_name("SHT") and not chart_preview.is_chart_name("a.nut")


def test_preview_pane_chart_and_image(app):
    from PySide6.QtCore import QBuffer, QByteArray
    from taiko2x6.app.taiko256_explorer_gui6 import PreviewPane
    pane = PreviewPane()
    entry = {"offset": 0, "index": 0}
    pane.show_file("abc_m.sht", "song.abc", entry, _sht(), False)
    assert "3 hits" in pane.info.toPlainText()
    assert not pane.image_view.has_image()
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.WriteOnly)
    _pix(10, 6).save(buf, "PNG")
    pane.show_file("a.png", "g", entry, bytes(ba), False)
    assert pane.image_view.has_image()
    assert "10 x 6 px  PNG" in pane.image_view.lbl_info.text()
    pane.close()
    pane.deleteLater()

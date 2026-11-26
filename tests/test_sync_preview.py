"""Song Builder 'Preview sync': click times and music shift must land where the
build puts them. Synthetic TJA + a generated WAV only."""
import os
import wave

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from taiko2x6.audio import vagtool  # noqa: E402
from taiko2x6.songs import sync_preview as SP  # noqa: E402

RATE = 44100


def _tja(offset, body, bpm=120):
    return ("TITLE:T\nBPM:%s\nOFFSET:%s\nCOURSE:Oni\nLEVEL:8\n#START\n%s\n#END\n"
            % (bpm, offset, body))


def _wav(path, impulse_ms=1000.0, total_ms=8000.0, rate=RATE):
    n = int(total_ms * rate / 1000)
    pcm = np.zeros(n, dtype="<i2")
    pcm[int(round(impulse_ms * rate / 1000))] = 20000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return str(path)


def _ms(sample):
    return sample * 1000.0 / RATE


# -------------------------------------------------------------- click times
def test_click_times_simple_and_kinds():
    clicks, sync = SP.chart_clicks(_tja(-1.5, "1000,\n2000,\n0010,"), "oni")
    # one blank lead measure (2000 ms at 120 BPM), then 2000 ms measures
    assert [k for _t, k in clicks] == ["don", "ka", "don"]
    assert [round(t) for t, _k in clicks] == [2000, 4000, 7000]
    assert sync["one_measure_ms"] == 2000.0


def test_click_times_independent_of_offset():
    """OFFSET moves the music, not the chart (it is baked into the audio)."""
    a, _ = SP.chart_clicks(_tja(0, "1000,\n1000,"), "oni")
    b, _ = SP.chart_clicks(_tja(-3.2, "1000,\n1000,"), "oni")
    assert [round(t) for t, _ in a] == [round(t) for t, _ in b] == [2000, 4000]


def test_click_times_bpm_change_and_delay():
    body = "1000,\n#BPMCHANGE 240\n1000,\n#DELAY 0.5\n1000,\n1000,"
    clicks, _ = SP.chart_clicks(_tja(0, body), "oni")
    # lead 2000; m1 @2000 (2000 ms); m2 @4000 (1000 ms at 240); m3 @5000+500
    # delay (1000 ms); m4 @6500
    assert [round(t) for t, _ in clicks] == [2000, 4000, 5500, 6500]


def test_big_notes_and_rolls():
    clicks, _ = SP.chart_clicks(_tja(0, "3400,\n5008,"), "oni")
    assert [k for _t, k in clicks] == ["don", "ka"]     # roll start is no click


# ----------------------------------------------------------- audio placement
@pytest.mark.parametrize("offset, nudge, shift_ms", [
    (0.0, 0.0, 4000.0),          # 2 measures + OFFSET
    (-1.5, 0.0, 2500.0),
    (-1.5, 100.0, 2600.0),
    (0.0, -250.0, 3750.0),
    (-3.0, 0.0, 1000.0),         # trim 1000 / lead 2000
    (-3.0, -500.0, 500.0),       # nudge eats into the lead silence
    (0.75, 0.0, 4750.0),
])
def test_music_lands_where_the_build_puts_it(tmp_path, offset, nudge, shift_ms):
    wav = _wav(tmp_path / "a.wav", impulse_ms=1000.0)
    tja = _tja(offset, "1000,")
    _clicks, sync = SP.chart_clicks(tja, "oni")
    lead, trim = SP.audio_shift(sync, nudge)
    pcm = vagtool.load_game_pcm(wav, RATE)
    out = SP.render_preview(pcm, [], lead, trim, 0, 9000)
    hit = int(np.argmax(np.abs(out[:, 0].astype(np.int32))))
    assert abs(_ms(hit) - (1000.0 + shift_ms)) <= 1.0


def test_clicks_render_at_note_samples(tmp_path):
    body = "1000,\n#BPMCHANGE 240\n2000,\n#DELAY 0.5\n1020,"
    clicks, sync = SP.chart_clicks(_tja(-1.0, body), "oni")
    silent = np.zeros((RATE * 10, 2), dtype=np.int16)
    out = SP.render_preview(silent, clicks, 0, 0, 0, 10000)
    ch = out[:, 0].astype(np.int32)
    for t_ms, _k in clicks:
        s = int(round(t_ms * RATE / 1000))
        assert ch[s] != 0 and ch[s - 1] == 0, t_ms
        assert abs(_ms(s) - t_ms) <= 1.0
    # kinds are told apart by pitch (don low, ka high): zero crossings in 30 ms
    def crossings(k):
        seg = SP.make_click(k)
        return int(np.count_nonzero(np.diff(np.sign(seg)) != 0))
    assert crossings("ka") > 3 * crossings("don")


def test_window_start_and_music_in_mix(tmp_path):
    wav = _wav(tmp_path / "a.wav", impulse_ms=5000.0)
    pcm = vagtool.load_game_pcm(wav, RATE)
    out = SP.render_preview(pcm, [(5500.0, "don")], 0, 0, start_ms=4000, length_ms=2000)
    assert len(out) == 2 * RATE
    assert int(np.argmax(np.abs(out[:, 0].astype(np.int32)))) == RATE   # music at 5000 ms
    assert out[int(1.5 * RATE), 0] != 0          # click at 5500 ms -> 1500 into window


# ------------------------------------------------- shared shift == old formula
@pytest.mark.parametrize("lead, trim", [(0, 0), (1234.5, 0), (0, 777.7), (500, 300),
                                        (0, 99999999)])
def test_shift_pcm_matches_reference_and_window(lead, trim):
    rng = np.random.default_rng(1)
    pcm = rng.integers(-3000, 3000, size=(RATE, 2)).astype(np.int16)
    ref = pcm
    if trim > 0:
        cut = int(round(trim * RATE / 1000.0))
        ref = ref[cut:] if cut < len(ref) else ref[:0]
    if lead > 0:
        pad = int(round(lead * RATE / 1000.0))
        if pad > 0:
            ref = np.concatenate([np.zeros((pad, 2), dtype=np.int16), ref])
    got = vagtool.shift_pcm(pcm, RATE, lead, trim)
    assert got.shape == ref.shape and np.array_equal(got, ref)
    n = len(ref)
    if n > 200:
        win = vagtool.shift_pcm_window(pcm, RATE, lead, trim, n // 3, n // 3 + 100)
        assert np.array_equal(win, ref[n // 3:n // 3 + 100])


def test_build_args_untouched_by_preview():
    """The preview reads the builder's math; the builder's output is unchanged."""
    from taiko2x6.songs import song_builder
    sync = song_builder.prepare_tja_for_game(_tja(-1.5, "1000,"))
    assert song_builder._sync_audio_args(sync, 0, lambda s: None) == (2500.0, 0.0)


# ------------------------------------------------------------------- dialog
@pytest.fixture
def app(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from taiko2x6.app import appconfig
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    return QApplication.instance() or QApplication([])


def test_dialog_renders_applies_and_closes(app, tmp_path):
    pytest.importorskip("PySide6")
    if SP.SyncPreviewDialog is None:
        pytest.skip("no Qt")
    wav = _wav(tmp_path / "a.wav", total_ms=70000)
    d = SP.SyncPreviewDialog(_tja(-1.5, "1000,\n2000,"), wav, 0.0)
    got = []
    d.nudge_applied.connect(got.append)
    try:
        assert d.cb_course.currentText() == "oni"
        d.refresh()
        pcm, lead, trim = d.last_render
        assert (lead, trim) == (2500.0, 0.0) and len(pcm) == 60 * RATE
        d.sp_nudge.setValue(-100)
        d.refresh()
        assert d.last_render[1] == 2400.0
        d._apply()
        assert got == [-100.0] and d.applied_nudge == -100.0
    finally:
        d.close()
        d.deleteLater()


def test_dialog_reports_bad_audio(app, tmp_path):
    d = SP.SyncPreviewDialog(_tja(0, "1000,"), str(tmp_path / "missing.wav"))
    try:
        d.refresh()
        assert d.last_render is None and not d.b_apply.isEnabled()
    finally:
        d.close()
        d.deleteLater()


def test_builder_button_applies_nudge(app, tmp_path, monkeypatch):
    from taiko2x6.songs import song_builder
    monkeypatch.setattr(song_builder, "song_ids", lambda a: ["alpha"])
    tja = tmp_path / "s.tja"
    tja.write_text(_tja(0, "1000,"), encoding="utf-8")
    wav = _wav(tmp_path / "a.wav")

    class Fake(SP.SyncPreviewDialog):
        def exec(self):
            self.sp_nudge.setValue(35)
            self._apply()
            return 1

    monkeypatch.setattr(SP, "SyncPreviewDialog", Fake)
    d = song_builder.SongBuilderDialog(object())
    try:
        assert not d.b_preview.isEnabled()
        d.ed_tja.setText(str(tja))
        d.ed_audio.setText(wav)
        assert d.b_preview.isEnabled()
        d._preview_sync()
        assert d.ed_gap.text() == "35"
    finally:
        d.close()
        d.deleteLater()

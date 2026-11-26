#!/usr/bin/env python3
"""
Sync preview for the Song Builder: hear the song with a click on every note.

The chart and the music are placed with the SAME code the build uses
(song_builder.prepare_tja_for_game -> tja2sht.convert_tja for the chart,
song_builder._sync_audio_args -> vagtool.shift_pcm for the audio), so what you
hear is what the game will play. Don = low click, ka = high click.

Only a window of the song is rendered (so a nudge change re-renders instantly);
nothing here touches the build output.
"""
from __future__ import annotations

import numpy as np

from taiko2x6.audio import vagtool
from taiko2x6.charts import sht_export, tja2sht

RATE = 44100
WINDOW_MS = 60000
_DON = {1, 2, 3, 7, 11}      # small/big/2-hand don
_KA = {4, 5, 8, 13}          # small/big/2-hand ka
_COURSE_NAMES = {0: "easy", 1: "normal", 2: "hard", 3: "oni", 4: "edit"}


# --------------------------------------------------------------------------- #
#  pure helpers (no Qt)
# --------------------------------------------------------------------------- #
def tja_courses(tja_text: str) -> list:
    """Course names the TJA has (easy..edit), lowest first."""
    parsed = tja2sht.parse_tja(tja_text)
    return [_COURSE_NAMES[k] for k in sorted(parsed["courses"])
            if k in _COURSE_NAMES]


def chart_clicks(tja_text: str, course: str):
    """([(time_ms, 'don'|'ka')...], sync) for one course, in GAME time.

    `sync` is the builder's prepare_tja_for_game dict; the chart it converts is
    the rewritten one (OFFSET:0 + a blank lead measure), exactly like build_song.
    """
    from taiko2x6.songs import song_builder
    sync = song_builder.prepare_tja_for_game(tja_text)
    text = sync["tja"] if sync["one_measure_ms"] is not None else tja_text
    model = tja2sht.parse_sht(tja2sht.convert_tja(text, course))
    branched, per_track = sht_export.routes(model)
    out = []
    for t, routes in zip(model["tracks"], per_track):
        bar_ms = sht_export._bar_ms(t)
        for n in routes[2 if branched else 0]:
            kind = ("don" if n["type"] in _DON else
                    "ka" if n["type"] in _KA else None)
            if kind:
                out.append((float(t["time"]) + n["measure"] / float(tja2sht.POS_DIV)
                            * bar_ms, kind))
    out.sort(key=lambda c: c[0])
    return out, sync


def audio_shift(sync, nudge_ms: float) -> tuple:
    """(lead_ms, trim_ms) -- the build's own math, including the nudge."""
    from taiko2x6.songs import song_builder
    return song_builder._sync_audio_args(sync, nudge_ms, lambda s: None)


def make_click(kind: str, rate: int = RATE) -> np.ndarray:
    """Short decaying cosine burst that is non-zero from its first sample."""
    freq = 420.0 if kind == "don" else 2400.0
    n = int(rate * 0.03)
    t = np.arange(n) / float(rate)
    return np.cos(2 * np.pi * freq * t) * np.exp(-t * 130.0)


def click_sample(time_ms: float, rate: int = RATE) -> int:
    return int(round(time_ms * rate / 1000.0))


def render_preview(pcm: np.ndarray, clicks, lead_ms: float, trim_ms: float,
                   start_ms: float = 0.0, length_ms: float = WINDOW_MS,
                   rate: int = RATE, click_gain: float = 0.45,
                   with_music: bool = True) -> np.ndarray:
    """Stereo int16 of game time [start_ms, start_ms+length_ms): shifted music
    plus a click at each note."""
    s0 = click_sample(start_ms, rate)
    s1 = s0 + click_sample(length_ms, rate)
    total = vagtool.shifted_length(len(pcm), rate, lead_ms, trim_ms)
    s1 = min(s1, max(total, 0)) if with_music else s1
    if with_music:
        mix = vagtool.shift_pcm_window(pcm, rate, lead_ms, trim_ms, s0, s1) \
            .astype(np.int32)
    else:
        mix = np.zeros((max(0, s1 - s0), 2), dtype=np.int32)
    amp = 32767 * click_gain
    cache = {k: (make_click(k, rate) * amp).astype(np.int32)
             for k in ("don", "ka")}
    for t_ms, kind in clicks:
        a = click_sample(t_ms, rate) - s0
        c = cache[kind]
        lo, hi = max(a, 0), min(a + len(c), len(mix))
        if hi > lo:
            mix[lo:hi] += c[lo - a:hi - a, None]
    return np.clip(mix, -32768, 32767).astype(np.int16)


# --------------------------------------------------------------------------- #
#  dialog
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtCore import Qt, QTimer, Signal
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QDoubleSpinBox,
                                   QFormLayout, QHBoxLayout, QLabel, QComboBox,
                                   QSpinBox, QVBoxLayout)

    from taiko2x6.app.dialog_polish import polish
    from taiko2x6.app.i18n import tr
    from taiko2x6.audio.audioplayer import AudioPlayer
    from taiko2x6.songs import song_dialog_ui as UI
except ImportError:           # headless
    QDialog = None            # type: ignore


if QDialog is not None:
    class SyncPreviewDialog(QDialog):
        """Play the song with a click per note and tune the sync nudge by ear."""

        nudge_applied = Signal(float)

        def __init__(self, tja_text: str, audio_path: str, nudge_ms: float = 0.0,
                     parent=None):
            super().__init__(parent)
            self.setWindowTitle(tr("Preview sync"))
            self.setMinimumWidth(760)
            self.resize(820, 320)
            self._tja = tja_text
            self._audio_path = audio_path
            self._pcm = None
            self._clicks = {}                    # course -> [(ms, kind)]
            self._sync = {}                      # course -> sync dict
            self._resume = None
            self.applied_nudge = None
            self.last_render = None              # (pcm, lead, trim) of the last render
            try:
                self._courses = tja_courses(tja_text)
            except Exception:
                self._courses = []

            lay = QVBoxLayout(self)
            form = QFormLayout()
            self.cb_course = QComboBox()
            self.cb_course.addItems(self._courses)
            if "oni" in self._courses:
                self.cb_course.setCurrentText("oni")
            elif self._courses:
                self.cb_course.setCurrentIndex(len(self._courses) - 1)
            form.addRow(tr("course:"), self.cb_course)
            self.sp_nudge = QDoubleSpinBox()
            self.sp_nudge.setRange(-10000.0, 10000.0)
            self.sp_nudge.setDecimals(0)
            self.sp_nudge.setSingleStep(5.0)
            self.sp_nudge.setSuffix(" ms")
            self.sp_nudge.setValue(nudge_ms)
            self.sp_nudge.setToolTip(tr(
                "+ moves the music later, − earlier. Same value as the Song "
                "Builder's sync nudge."))
            form.addRow(tr("sync nudge:"), self.sp_nudge)
            self.sp_from = QSpinBox()
            self.sp_from.setRange(0, 3600)
            self.sp_from.setSuffix(" s")
            self.sp_from.setToolTip(tr(
                "Only one minute is rendered, starting here, so changes are "
                "instant. Positions in the player are relative to this start."))
            form.addRow(tr("start at:"), self.sp_from)
            lay.addLayout(form)

            self.lbl_info = QLabel()
            self.lbl_info.setWordWrap(True)
            lay.addWidget(self.lbl_info)
            self.player = AudioPlayer()
            self.player.info.setVisible(False)   # the status line says it all
            lay.addWidget(self.player)
            self.lbl_status = UI.status_label()
            lay.addWidget(self.lbl_status)

            box = QDialogButtonBox()
            self.b_apply = box.addButton(tr("Apply nudge"), QDialogButtonBox.AcceptRole)
            self.b_apply.setToolTip(tr("Copy this nudge back into the Song Builder."))
            self.b_close = box.addButton(tr("Close"), QDialogButtonBox.RejectRole)
            self.b_apply.setDefault(True)
            self.b_close.setAutoDefault(False)
            self.b_apply.clicked.connect(self._apply)
            self.b_close.clicked.connect(self.close)
            lay.addWidget(box)

            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.setInterval(250)
            self._timer.timeout.connect(self.refresh)
            for w in (self.sp_nudge, self.sp_from):
                w.valueChanged.connect(self._schedule)
            self.cb_course.currentIndexChanged.connect(self._schedule)
            self.player.player.mediaStatusChanged.connect(self._media_status)
            polish(self, primary=self.b_apply)
            self._timer.start(0)

        # -- rendering --------------------------------------------------------
        def _schedule(self, *_):
            self._timer.start()

        def _error(self, msg: str):
            self.lbl_status.setText("⚠ " + msg)
            self.lbl_status.setStyleSheet("color:palette(bright-text);")
            self.b_apply.setEnabled(False)

        def refresh(self):
            """(Re)render the window for the current course / nudge / start."""
            self._timer.stop()
            course = self.cb_course.currentText()
            if not course:
                self._error(tr("This TJA has no playable course."))
                return
            try:
                if self._pcm is None:
                    self._pcm = vagtool.load_game_pcm(self._audio_path, RATE)
                if course not in self._clicks:
                    self._clicks[course], self._sync[course] = \
                        chart_clicks(self._tja, course)
            except Exception as exc:
                self._error(tr("Could not prepare the preview: {err}").format(err=exc))
                return
            self.b_apply.setEnabled(True)
            self.lbl_status.setStyleSheet("")
            sync = self._sync[course]
            lead, trim = audio_shift(sync, self.sp_nudge.value())
            start_ms = self.sp_from.value() * 1000.0
            pcm = render_preview(self._pcm, self._clicks[course], lead, trim,
                                 start_ms=start_ms)
            self.last_render = (pcm, lead, trim)
            n = sum(1 for t, _k in self._clicks[course]
                    if start_ms <= t < start_ms + WINDOW_MS)
            self.lbl_info.setText(
                tr("Music shift: +{lead:.0f} ms silence, −{trim:.0f} ms trimmed "
                   "off the front.").format(lead=lead, trim=trim))
            self.lbl_status.setText(
                tr("{n} notes in this window. Don = low click, ka = high click.")
                .format(n=n))
            p = self.player.player
            self._resume = (p.position(),
                            p.playbackState() == QMediaPlayer.PlayingState)
            self.player.load_pcm(pcm, RATE, 2, autoplay=False, name="sync_preview")

        def _media_status(self, st):
            if st == QMediaPlayer.LoadedMedia and self._resume is not None:
                pos, playing = self._resume
                self._resume = None
                self.player.player.setPosition(pos)
                if playing:
                    self.player.player.play()

        def _apply(self):
            self.applied_nudge = float(self.sp_nudge.value())
            self.nudge_applied.emit(self.applied_nudge)
            self.lbl_status.setText(tr("Nudge applied: {v:g} ms").format(
                v=self.applied_nudge))

        def closeEvent(self, ev):
            self._timer.stop()
            self.player.stop()
            self.player._cleanup()
            self.player._sweep_pending()
            super().closeEvent(ev)
else:
    SyncPreviewDialog = None  # type: ignore

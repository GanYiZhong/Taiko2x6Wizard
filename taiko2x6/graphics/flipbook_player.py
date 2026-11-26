#!/usr/bin/env python3
"""
Flipbook frame-animation player (PySide6).

Plays a Taiko archive group's TIM2 (".nut") textures as animation frames.
A group's textures are usually the screen's whole sprite library rather than a
single animation, so frames are auto-split into "clips": maximal runs of
consecutive frames sharing the same dimensions. Each clip is one playable
sequence (play/pause, FPS, scrub, loop, prev/next).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QCheckBox, QSpinBox,
    QSlider, QLabel, QPushButton, QStyle,
)

from taiko2x6.app.i18n import tr
from taiko2x6.graphics.imageview import ImageViewer


def detect_clips(frames: list) -> list:
    """frames: list of (name, QPixmap). Return clips as (start, end, label)."""
    clips = []
    if not frames:
        return clips
    start = 0
    def dim(i):
        p = frames[i][1]
        return (p.width(), p.height())
    for i in range(1, len(frames) + 1):
        if i == len(frames) or dim(i) != dim(start):
            w, h = dim(start)
            clips.append((start, i, tr("frames {a}-{b}  ({w}x{h}) x{n}").format(a=start, b=i-1, w=w, h=h, n=i-start)))
            start = i
    return clips


class FlipbookPlayer(QDialog):
    def __init__(self, frames: list, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Frame Player - {t}").format(t=title))
        self.resize(640, 560)
        self.frames = frames                       # list[(name, QPixmap)]
        self.clip_range = (0, len(frames))
        self.idx = 0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

        self._build_ui()
        self._populate_clips()
        if not self.frames:
            # No decodable frames: show a message and disable playback controls
            # rather than presenting a blank, dead dialog.
            self.lbl.setText(tr("no frames to display"))
            for w in (self.b_prev, self.b_play, self.b_next,
                      self.slider, self.cb_clip, self.sp_fps, self.chk_loop,
                      self.cb_speed):
                w.setEnabled(False)
        else:
            self._show_frame(0)

    # -- ui -------------------------------------------------------------------
    def _build_ui(self):
        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        self.cb_clip = QComboBox()
        self.cb_clip.currentIndexChanged.connect(self._on_clip)
        top.addWidget(QLabel(tr("clip:")))
        top.addWidget(self.cb_clip, 1)
        top.addWidget(QLabel(tr("FPS:")))
        self.sp_fps = QSpinBox(); self.sp_fps.setRange(1, 60); self.sp_fps.setValue(12)
        self.sp_fps.valueChanged.connect(self._apply_fps)
        top.addWidget(self.sp_fps)
        self.cb_speed = QComboBox()
        self.cb_speed.setToolTip(tr("Playback speed (multiplies FPS)"))
        for f in (0.25, 0.5, 1.0, 2.0):
            self.cb_speed.addItem(f"{f:g}x", f)
        self.cb_speed.setCurrentIndex(2)
        self.cb_speed.currentIndexChanged.connect(self._apply_fps)
        top.addWidget(self.cb_speed)
        self.chk_loop = QCheckBox(tr("loop")); self.chk_loop.setChecked(True)
        top.addWidget(self.chk_loop)
        lay.addLayout(top)

        self.view = ImageViewer(show_info=False)
        self.view.setMinimumSize(320, 240)
        lay.addWidget(self.view, 1)

        self.lbl = QLabel("", alignment=Qt.AlignCenter)
        lay.addWidget(self.lbl)

        ctl = QHBoxLayout()
        style = self.style()
        self.b_prev = QPushButton(style.standardIcon(QStyle.SP_MediaSkipBackward), "")
        self.b_play = QPushButton(style.standardIcon(QStyle.SP_MediaPlay), tr("Play"))
        self.b_play.setToolTip(tr("Play / pause (Space)"))
        self.b_next = QPushButton(style.standardIcon(QStyle.SP_MediaSkipForward), "")
        self.b_prev.setToolTip(tr("Previous frame (Left arrow)"))
        self.b_next.setToolTip(tr("Next frame (Right arrow)"))
        self.b_prev.clicked.connect(lambda: self._step(-1))
        self.b_next.clicked.connect(lambda: self._step(+1))
        self.b_play.clicked.connect(self._toggle_play)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.valueChanged.connect(self._on_slider)
        ctl.addWidget(self.b_prev)
        ctl.addWidget(self.b_play)
        ctl.addWidget(self.b_next)
        ctl.addWidget(self.slider, 1)
        lay.addLayout(ctl)
        for key, fn in ((Qt.Key_Space, self._toggle_play),
                        (Qt.Key_Left, lambda: self._step(-1)),
                        (Qt.Key_Right, lambda: self._step(+1)),
                        (Qt.Key_Home, lambda: self._show_frame(0)),
                        (Qt.Key_End, lambda: self._show_frame(-1))):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)

    def _populate_clips(self):
        self.clips = detect_clips(self.frames)
        self.cb_clip.blockSignals(True)
        self.cb_clip.addItem(tr("All frames (x{n})").format(n=len(self.frames)))
        for _s, _e, label in self.clips:
            self.cb_clip.addItem(label)
        self.cb_clip.blockSignals(False)
        # default to the longest real clip if it's more meaningful than "all"
        if self.clips:
            longest = max(range(len(self.clips)), key=lambda i: self.clips[i][1] - self.clips[i][0])
            if (self.clips[longest][1] - self.clips[longest][0]) > 1:
                self.cb_clip.setCurrentIndex(longest + 1)

    # -- clip / frame ---------------------------------------------------------
    def _on_clip(self, i):
        if i <= 0:
            self.clip_range = (0, len(self.frames))
        else:
            s, e, _ = self.clips[i - 1]
            self.clip_range = (s, e)
        n = self.clip_range[1] - self.clip_range[0]
        self.slider.setRange(0, max(0, n - 1))
        self._show_frame(0)

    def _show_frame(self, local_idx):
        s, e = self.clip_range
        n = e - s
        if n <= 0:
            return
        self.idx = local_idx % n
        name, pix = self.frames[s + self.idx]
        self.view.set_pixmap(pix)
        self.lbl.setText(tr("frame {i}/{n}   {name}   {w}x{h}").format(
            i=self.idx + 1, n=n, name=name, w=pix.width(), h=pix.height()))
        self.slider.blockSignals(True)
        self.slider.setValue(self.idx)
        self.slider.blockSignals(False)

    def _step(self, d):
        self._show_frame(self.idx + d)

    def _on_slider(self, v):
        self._show_frame(v)

    # -- playback -------------------------------------------------------------
    def _toggle_play(self):
        if self.timer.isActive():
            self.timer.stop()
            self.b_play.setText(tr("Play"))
            self.b_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        else:
            self._apply_fps()
            self.timer.start()
            self.b_play.setText(tr("Pause"))
            self.b_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))

    def _apply_fps(self):
        # round() avoids the systematic timing drift int() introduces at high
        # FPS (e.g. 60 -> 17ms instead of 16ms); FPS >= 1 so no divide-by-zero.
        speed = self.cb_speed.currentData() or 1.0
        self.timer.setInterval(max(1, round(1000 / (self.sp_fps.value() * speed))))

    def _tick(self):
        s, e = self.clip_range
        n = e - s
        nxt = self.idx + 1
        if nxt >= n:
            if not self.chk_loop.isChecked():
                self._toggle_play()
                return
            nxt = 0
        self._show_frame(nxt)

    def closeEvent(self, ev):
        self.timer.stop()
        super().closeEvent(ev)

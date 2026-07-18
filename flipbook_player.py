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

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QPainter, QColor, QPixmap
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QCheckBox, QSpinBox,
    QSlider, QLabel, QPushButton, QStyle,
)


class FrameView(QWidget):
    """Draws the current frame centered + scaled on a checkerboard."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(320, 240)
        self._pix: QPixmap | None = None

    def set_pixmap(self, pix: QPixmap | None):
        self._pix = pix
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(30, 30, 34))
        if self._pix is None or self._pix.isNull():
            return
        cb = 10
        for yy in range(self.height() // cb + 1):
            for xx in range(self.width() // cb + 1):
                if (xx + yy) & 1:
                    p.fillRect(xx * cb, yy * cb, cb, cb, QColor(44, 44, 50))
        pw, ph = self._pix.width(), self._pix.height()
        if pw <= 0 or ph <= 0:
            # A non-null but 0x0 pixmap (e.g. a failed TIM2 decode) would make
            # the scale computation divide by zero; nothing to draw.
            return
        scale = min(self.width() / pw, self.height() / ph)
        dw, dh = pw * scale, ph * scale
        x = (self.width() - dw) / 2
        y = (self.height() - dh) / 2
        p.drawPixmap(QRectF(x, y, dw, dh), self._pix, QRectF(self._pix.rect()))
        p.end()


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
            clips.append((start, i, f"frames {start}-{i-1}  ({w}x{h}) ×{i-start}"))
            start = i
    return clips


class FlipbookPlayer(QDialog):
    def __init__(self, frames: list, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Frame Player — {title}")
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
            self.lbl.setText("no frames to display")
            for w in (self.b_prev, self.b_play, self.b_next,
                      self.slider, self.cb_clip, self.sp_fps, self.chk_loop):
                w.setEnabled(False)
        else:
            self._show_frame(0)

    # -- ui -------------------------------------------------------------------
    def _build_ui(self):
        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        self.cb_clip = QComboBox()
        self.cb_clip.currentIndexChanged.connect(self._on_clip)
        top.addWidget(QLabel("clip:"))
        top.addWidget(self.cb_clip, 1)
        top.addWidget(QLabel("FPS:"))
        self.sp_fps = QSpinBox(); self.sp_fps.setRange(1, 60); self.sp_fps.setValue(12)
        self.sp_fps.valueChanged.connect(self._apply_fps)
        top.addWidget(self.sp_fps)
        self.chk_loop = QCheckBox("loop"); self.chk_loop.setChecked(True)
        top.addWidget(self.chk_loop)
        lay.addLayout(top)

        self.view = FrameView()
        lay.addWidget(self.view, 1)

        self.lbl = QLabel("", alignment=Qt.AlignCenter)
        lay.addWidget(self.lbl)

        ctl = QHBoxLayout()
        style = self.style()
        self.b_prev = QPushButton(style.standardIcon(QStyle.SP_MediaSkipBackward), "")
        self.b_play = QPushButton(style.standardIcon(QStyle.SP_MediaPlay), "Play")
        self.b_next = QPushButton(style.standardIcon(QStyle.SP_MediaSkipForward), "")
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

    def _populate_clips(self):
        self.clips = detect_clips(self.frames)
        self.cb_clip.blockSignals(True)
        self.cb_clip.addItem(f"All frames (×{len(self.frames)})")
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
        self.lbl.setText(f"{self.idx+1}/{n}   {name}   {pix.width()}x{pix.height()}")
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
            self.b_play.setText("Play")
            self.b_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        else:
            self._apply_fps()
            self.timer.start()
            self.b_play.setText("Pause")
            self.b_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))

    def _apply_fps(self):
        # round() avoids the systematic timing drift int() introduces at high
        # FPS (e.g. 60 -> 17ms instead of 16ms); FPS >= 1 so no divide-by-zero.
        self.timer.setInterval(max(1, round(1000 / self.sp_fps.value())))

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

#!/usr/bin/env python3
"""
Audio player widget for the Taiko explorer — plays decoded PCM (from VAG streams
or HD/BD sound-bank waveforms) with transport controls.

Decodes to int16 PCM, writes a temporary WAV, and drives it with QMediaPlayer so
play / pause / stop / seek and a position slider come for free.
"""
from __future__ import annotations

import os
import struct
import tempfile
import wave
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QUrl
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSlider, QLabel, QStyle,
    QComboBox,
)


def is_vag(data: bytes) -> bool:
    """Heuristic: the game's interleaved-VAG container header.

    Accept interleave in (0, 0x8000): decode_vag tolerates interleave == 0 by
    treating it as the standard block size, so is_vag must classify the same
    files as decodable (kept in sync with vagtool.decode_vag).
    """
    if len(data) < 0x10:
        return False
    interleave, _size, ch, rate = struct.unpack_from("<4I", data, 0)
    return interleave in (0, 0x8000) and ch in (1, 2) and 8000 <= rate <= 48000


def _write_wav(path: str, pcm: np.ndarray, rate: int, channels: int):
    pcm = np.asarray(pcm)
    if pcm.ndim == 1:
        channels = 1
        frames = pcm.astype("<i2").tobytes()
    else:
        channels = pcm.shape[1]
        frames = np.ascontiguousarray(pcm.astype("<i2")).tobytes()
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(frames)


def _fmt_time(ms: int) -> str:
    s = max(0, ms) // 1000
    return f"{s // 60}:{s % 60:02d}"


class AudioPlayer(QWidget):
    """Transport + seek bar. Feed it PCM via load_pcm()."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.9)
        self.player.setAudioOutput(self.audio)
        self._tmp: str | None = None
        # Temp files QMediaPlayer hasn't released yet (Windows holds the handle
        # briefly after setSource(QUrl())). Swept on next load and on teardown.
        self._pending_delete: list[str] = []
        self._seeking = False
        self._build_ui()
        self.player.positionChanged.connect(self._on_pos)
        self.player.durationChanged.connect(self._on_dur)
        self.player.playbackStateChanged.connect(self._on_state)

    def _build_ui(self):
        lay = QVBoxLayout(self)
        style = self.style()
        row = QHBoxLayout()
        self.b_play = QPushButton(style.standardIcon(QStyle.SP_MediaPlay), "")
        self.b_play.clicked.connect(self._toggle)
        self.b_stop = QPushButton(style.standardIcon(QStyle.SP_MediaStop), "")
        self.b_stop.clicked.connect(self.player.stop)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.slider.sliderReleased.connect(self._seek_release)
        self.lbl = QLabel("0:00 / 0:00")
        row.addWidget(self.b_play)
        row.addWidget(self.b_stop)
        row.addWidget(self.slider, 1)
        row.addWidget(self.lbl)
        lay.addLayout(row)

        vol = QHBoxLayout()
        vol.addWidget(QLabel("vol"))
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(90)
        self.vol.setFixedWidth(120)
        self.vol.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        vol.addWidget(self.vol)
        self.b_wav = QPushButton("Export WAV…"); self.b_wav.clicked.connect(self._export_wav)
        self.b_wav.setEnabled(False)
        self.b_vag = QPushButton("Export VAG…"); self.b_vag.clicked.connect(self._export_vag)
        self.b_vag.setEnabled(False)
        vol.addWidget(self.b_wav); vol.addWidget(self.b_vag)
        self.info = QLabel("")
        self.info.setStyleSheet("color:#999;")
        vol.addWidget(self.info, 1)
        lay.addLayout(vol)
        self._src_vag = None
        self._name = "audio"

    # -- loading --------------------------------------------------------------
    def load_pcm(self, pcm: np.ndarray, rate: int, channels: int = 2,
                 autoplay: bool = False, info: str = "", name: str = "audio"):
        self.player.stop()
        self.player.setSource(QUrl())          # release previous temp file
        self._cleanup()
        self._sweep_pending()                  # retry deletes Windows blocked earlier
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="taikoaud_")
        os.close(fd)
        _write_wav(path, pcm, rate, channels)
        self._tmp = path
        self._name = name
        n = pcm.shape[0]
        self.info.setText(info or f"{rate} Hz · {channels}ch · {n/rate:.1f}s")
        self.player.setSource(QUrl.fromLocalFile(path))
        self.b_wav.setEnabled(True)
        self.b_vag.setEnabled(self._src_vag is not None)
        if autoplay:
            self.player.play()

    def load_vag(self, vag_bytes: bytes, autoplay: bool = False, name: str = "audio"):
        import vagtool
        rate, ch, pcm = vagtool.decode_vag(vag_bytes)
        self._src_vag = vag_bytes
        self.load_pcm(pcm, rate, ch, autoplay=autoplay, name=name,
                      info=f"VAG · {rate} Hz · {ch}ch · {pcm.shape[0]/rate:.1f}s")

    def _export_wav(self):
        if not self._tmp:
            return
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        import shutil
        import appconfig                           # last-used-path memory
        dest = appconfig.pick_save(self, "wav_export", "Export WAV",
                                   self._name + ".wav", "WAV (*.wav)")
        if dest:
            try:
                shutil.copyfile(self._tmp, dest)
            except Exception as exc:
                QMessageBox.critical(self, "Export failed", str(exc))

    def _export_vag(self):
        if not self._src_vag:
            return
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        import appconfig
        dest = appconfig.pick_save(self, "vag_export", "Export VAG",
                                   self._name + ".vag", "VAG (*.vag);;All (*)")
        if dest:
            try:
                Path(dest).write_bytes(self._src_vag)
            except Exception as exc:
                QMessageBox.critical(self, "Export failed", str(exc))

    def stop(self):
        self.player.stop()

    # -- transport ------------------------------------------------------------
    def _toggle(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_state(self, state):
        icon = QStyle.SP_MediaPause if state == QMediaPlayer.PlayingState else QStyle.SP_MediaPlay
        self.b_play.setIcon(self.style().standardIcon(icon))

    def _on_pos(self, ms):
        if not self._seeking:
            self.slider.setValue(ms)
        self.lbl.setText(f"{_fmt_time(ms)} / {_fmt_time(self.player.duration())}")

    def _on_dur(self, ms):
        self.slider.setRange(0, ms)

    def _seek_release(self):
        self._seeking = False
        self.player.setPosition(self.slider.value())

    def _cleanup(self):
        if self._tmp and os.path.exists(self._tmp):
            try:
                os.remove(self._tmp)
            except OSError:
                # Windows may still hold the handle; defer and retry later
                # instead of leaking the file into %TEMP%.
                self._pending_delete.append(self._tmp)
        self._tmp = None

    def _sweep_pending(self):
        """Retry deleting temp files Windows previously refused to release."""
        still: list[str] = []
        for p in self._pending_delete:
            if not os.path.exists(p):
                continue
            try:
                os.remove(p)
            except OSError:
                still.append(p)
        self._pending_delete = still

    def closeEvent(self, ev):
        # Release the current source so its temp WAV can be removed, then sweep.
        try:
            self.player.stop()
            self.player.setSource(QUrl())
        except Exception:
            pass
        self._cleanup()
        self._sweep_pending()
        super().closeEvent(ev)


from PySide6.QtWidgets import QDialog, QListWidget, QLabel as _QLabel


class SoundBankDialog(QDialog):
    """List + play every waveform in a Sony HD/BD sound-effect bank."""

    def __init__(self, hd: bytes, bd: bytes, title: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Sound Bank — {title}" if title else "Sound Bank")
        self.resize(460, 420)
        import hdbd
        self.sounds = hdbd.list_bank(hd, bd)      # [{index,name,sample_rate,seconds,pcm}]

        lay = QVBoxLayout(self)
        lay.addWidget(_QLabel(f"{len(self.sounds)} waveforms — double-click or press ▶ to play"))
        self.lst = QListWidget()
        for s in self.sounds:
            nm = s.get("name") or f"wave {s['index']:03d}"
            self.lst.addItem(f"[{s['index']:03d}] {nm}   {s['sample_rate']} Hz · {s['seconds']:.2f}s")
        self.lst.currentRowChanged.connect(self._load)
        self.lst.itemDoubleClicked.connect(self._play_current)
        lay.addWidget(self.lst, 1)

        self.player = AudioPlayer()
        lay.addWidget(self.player)
        self._loaded_row = -1
        if self.sounds:
            self.lst.setCurrentRow(0)

    def _load(self, row: int):
        if 0 <= row < len(self.sounds):
            s = self.sounds[row]
            nm = s.get("name") or f"wave{s['index']:03d}"
            self.player.load_pcm(s["pcm"], s["sample_rate"], 1, autoplay=False, name=nm,
                                 info=f"{s['sample_rate']} Hz · mono · {s['seconds']:.2f}s")
            self._loaded_row = row

    def _play_current(self, *_):
        # Ensure the highlighted row is actually loaded before playing — a
        # double-click before currentRowChanged fires would otherwise play
        # stale/empty PCM. Only (re)load when the row isn't already loaded so we
        # don't restart a track the user double-clicks again.
        row = self.lst.currentRow()
        if 0 <= row < len(self.sounds) and row != self._loaded_row:
            self._load(row)
        self.player.player.play()

    def closeEvent(self, ev):
        self.player.stop()
        self.player._cleanup()
        self.player._sweep_pending()
        super().closeEvent(ev)

#!/usr/bin/env python3
"""
Standalone generator — export loose game files to a folder without touching an
archive.

  * VagGeneratorDialog : wav/ogg   -> the game's interleaved VAG stream

It writes to a plain output folder so you can inspect the file, drop it into an
extracted DATA.000 tree, or repack it yourself. Charts are converted by
taiko2x6.charts.chart_convert.
"""
from __future__ import annotations

import logging
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLineEdit,
    QPushButton, QCheckBox, QPlainTextEdit, QLabel, QMessageBox,
    QWidget, QProgressDialog,
)

from taiko2x6.app import appconfig
from taiko2x6.audio import vagtool

logger = logging.getLogger("taiko.generators")

_DIFF_LETTERS = [("e", "Easy"), ("n", "Normal"), ("h", "Hard"), ("m", "Oni/魔王")]
_DIFF_COURSE = {"e": "easy", "n": "normal", "h": "hard", "m": "oni"}


# ===========================================================================
# VAG generator
# ===========================================================================
class _VagWorker(QThread):
    done = Signal(object)                       # bytes | ("ERROR", exc, tb)

    def __init__(self, path, rate):
        super().__init__()
        self.path, self.rate = path, rate

    def run(self):
        try:
            self.done.emit(vagtool.convert_audio_file(self.path, self.rate))
        except Exception as exc:
            self.done.emit(("ERROR", exc, traceback.format_exc()))


class VagGeneratorDialog(QDialog):
    """wav/ogg -> the game's interleaved VAG stream, written to a folder."""

    def __init__(self, parent=None, default_audio="", default_out=""):
        super().__init__(parent)
        self.setWindowTitle("VAG Generator — audio → VAG")
        self.resize(640, 420)
        self._build_ui()
        if default_audio:
            self.ed_audio.setText(default_audio)
        if default_out:
            self.ed_out.setText(default_out)

    def _build_ui(self):
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.ed_audio = QLineEdit(); b_a = QPushButton("Audio…")
        b_a.clicked.connect(self._pick_audio)
        form.addRow("audio (wav/ogg):", self._row(self.ed_audio, b_a))
        self.cb_rate = QComboBox(); self.cb_rate.addItems(["44100", "32000", "48000", "22050"])
        self.cb_rate.setEditable(True)
        form.addRow("sample rate:", self.cb_rate)
        self.ed_id = QLineEdit(); self.ed_id.setPlaceholderText("song id, e.g. 10tai")
        form.addRow("song id:", self.ed_id)
        self.ed_out = QLineEdit(); b_out = QPushButton("…")
        b_out.clicked.connect(self._pick_out)
        form.addRow("output folder:", self._row(self.ed_out, b_out))
        lay.addLayout(form)

        self.ck_folder = QCheckBox(
            "folder layout  (music_<id>/vag — matches an extracted DATA.000 sound/stream)")
        self.ck_folder.setChecked(True)
        lay.addWidget(self.ck_folder)

        self.log = QPlainTextEdit(readOnly=True)
        lay.addWidget(QLabel("log:")); lay.addWidget(self.log, 1)

        btns = QHBoxLayout(); btns.addStretch(1)
        self.b_go = QPushButton("Generate VAG"); self.b_go.clicked.connect(self._go)
        b_close = QPushButton("Close"); b_close.clicked.connect(self.reject)
        btns.addWidget(self.b_go); btns.addWidget(b_close)
        lay.addLayout(btns)

    def _row(self, e, b):
        w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(e, 1); h.addWidget(b); return w

    def _pick_audio(self):
        p = appconfig.pick_open(self, "wav", "Audio", "Audio (*.wav *.ogg);;All (*)")
        if p:
            self.ed_audio.setText(p)
            if not self.ed_id.text():
                self.ed_id.setText(Path(p).stem)

    def _pick_out(self):
        p = appconfig.pick_dir(self, "outdir", "Output folder")
        if p:
            self.ed_out.setText(p)

    def _go(self):
        audio = self.ed_audio.text().strip()
        if not audio or not Path(audio).exists():
            QMessageBox.warning(self, "VAG Generator", "Pick an audio file."); return
        if not self.ed_out.text().strip():
            QMessageBox.warning(self, "VAG Generator", "Pick an output folder."); return
        try:
            rate = int(self.cb_rate.currentText())
        except ValueError:
            QMessageBox.warning(self, "VAG Generator", "Sample rate must be a number."); return
        self.log.appendPlainText(f"encoding {Path(audio).name} @ {rate} Hz…")
        logger.info("vag encode %s @ %d Hz", audio, rate)
        self.b_go.setEnabled(False)
        self._prog = QProgressDialog("Encoding VAG…", None, 0, 0, self)
        self._prog.setWindowModality(Qt.WindowModal); self._prog.setMinimumDuration(0)
        self._prog.show()
        self._worker = _VagWorker(audio, rate)
        self._worker.done.connect(self._done)
        self._worker.start()

    def _done(self, res):
        self._prog.close(); self.b_go.setEnabled(True)
        if isinstance(res, tuple) and res and res[0] == "ERROR":
            self.log.appendPlainText("FAILED:\n" + res[2])
            QMessageBox.critical(self, "VAG failed", str(res[1])); return
        vag = res
        sid = self.ed_id.text().strip() or Path(self.ed_audio.text()).stem
        outdir = Path(self.ed_out.text().strip()); outdir.mkdir(parents=True, exist_ok=True)
        if self.ck_folder.isChecked():
            d = outdir / f"music_{sid}"; d.mkdir(exist_ok=True); dest = d / "vag"
        else:
            dest = outdir / f"music_{sid}.vag"
        dest.write_bytes(vag)
        import struct
        _ib, ds, ch, sr = struct.unpack_from("<4I", vag, 0)
        secs = (ds // ch) / 16 * 28 / sr if ch else 0
        self.log.appendPlainText(
            f"✓ wrote {dest}\n  {len(vag):,} bytes, {ch}ch {sr} Hz, ~{secs:.1f}s")
        logger.info("vag -> %s (%d B, %.1fs)", dest, len(vag), secs)
        QMessageBox.information(self, "VAG built", f"Wrote:\n{dest}\n\n{len(vag):,} bytes, ~{secs:.1f}s")


# ===========================================================================
# Standalone launcher
# ===========================================================================
def main():
    import sys
    from PySide6.QtWidgets import QApplication
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    app = QApplication(sys.argv)
    dlg = VagGeneratorDialog()
    dlg.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

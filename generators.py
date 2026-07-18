#!/usr/bin/env python3
"""
Standalone generators — export loose game files to a folder without touching an
archive.

  * ShtGeneratorDialog : TJA chart -> Gen2 .sht (per difficulty / player)
  * VagGeneratorDialog : wav/ogg   -> the game's interleaved VAG stream

Both write to a plain output folder so you can inspect the files, drop them into
an extracted DATA.000 tree, or repack them yourself. The sht generator also
checks each chart against the game's per-difficulty note-buffer limit (over the
limit freezes the game at load) and warns.
"""
from __future__ import annotations

import logging
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLineEdit,
    QPushButton, QFileDialog, QCheckBox, QPlainTextEdit, QLabel, QMessageBox,
    QWidget, QProgressDialog, QGroupBox,
)

import appconfig
import tja2sht
import vagtool

logger = logging.getLogger("taiko.generators")

_DIFF_LETTERS = [("e", "Easy"), ("n", "Normal"), ("h", "Hard"), ("m", "Oni/魔王")]
_DIFF_COURSE = {"e": "easy", "n": "normal", "h": "hard", "m": "oni"}


def _read_tja_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "shift_jis", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


# ===========================================================================
# SHT generator
# ===========================================================================
class ShtGeneratorDialog(QDialog):
    """TJA -> .sht for the chosen difficulties / players, written to a folder."""

    def __init__(self, parent=None, default_tja="", default_out=""):
        super().__init__(parent)
        self.setWindowTitle("SHT Generator — TJA → .sht")
        self.resize(660, 560)
        self._build_ui()
        if default_tja:
            self.ed_tja.setText(default_tja)
        if default_out:
            self.ed_out.setText(default_out)

    def _build_ui(self):
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.ed_tja = QLineEdit(); b_tja = QPushButton("TJA…")
        b_tja.clicked.connect(self._pick_tja)
        form.addRow("TJA chart:", self._row(self.ed_tja, b_tja))
        self.ed_id = QLineEdit(); self.ed_id.setPlaceholderText("song id, e.g. 10tai")
        form.addRow("song id:", self.ed_id)
        self.ed_out = QLineEdit(); b_out = QPushButton("…")
        b_out.clicked.connect(self._pick_out)
        form.addRow("output folder:", self._row(self.ed_out, b_out))
        lay.addLayout(form)

        # difficulty + player selection
        sel = QGroupBox("Generate for")
        sl = QHBoxLayout(sel)
        self.ck_diff = {}
        dcol = QVBoxLayout(); dcol.addWidget(QLabel("difficulties:"))
        for letter, name in _DIFF_LETTERS:
            c = QCheckBox(name); c.setChecked(True)
            self.ck_diff[letter] = c; dcol.addWidget(c)
        sl.addLayout(dcol)
        self.ck_1p = QCheckBox("1P"); self.ck_1p.setChecked(True)
        self.ck_2p = QCheckBox("2P"); self.ck_2p.setChecked(True)
        pcol = QVBoxLayout(); pcol.addWidget(QLabel("players:"))
        pcol.addWidget(self.ck_1p); pcol.addWidget(self.ck_2p); pcol.addStretch(1)
        sl.addLayout(pcol)
        lay.addWidget(sel)

        # layout mode
        self.ck_folder = QCheckBox(
            "folder-per-chart layout  (<id><p>_<d>/sht — matches an extracted DATA.000)")
        self.ck_folder.setChecked(True)
        lay.addWidget(self.ck_folder)

        self.log = QPlainTextEdit(readOnly=True)
        lay.addWidget(QLabel("log:")); lay.addWidget(self.log, 1)

        btns = QHBoxLayout(); btns.addStretch(1)
        self.b_go = QPushButton("Generate .sht"); self.b_go.clicked.connect(self._go)
        b_close = QPushButton("Close"); b_close.clicked.connect(self.reject)
        btns.addWidget(self.b_go); btns.addWidget(b_close)
        lay.addLayout(btns)

    def _row(self, e, b):
        w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(e, 1); h.addWidget(b); return w

    def _pick_tja(self):
        p = appconfig.pick_open(self, "tja", "TJA chart", "TJA (*.tja)")
        if p:
            self.ed_tja.setText(p)
            if not self.ed_id.text():
                self.ed_id.setText(Path(p).stem)

    def _pick_out(self):
        p = appconfig.pick_dir(self, "outdir", "Output folder")
        if p:
            self.ed_out.setText(p)

    def _go(self):
        tja = self.ed_tja.text().strip()
        sid = self.ed_id.text().strip() or "song"
        out = self.ed_out.text().strip()
        if not tja or not Path(tja).exists():
            QMessageBox.warning(self, "SHT Generator", "Pick a TJA file."); return
        if not out:
            QMessageBox.warning(self, "SHT Generator", "Pick an output folder."); return
        outdir = Path(out); outdir.mkdir(parents=True, exist_ok=True)
        try:
            tja_text = _read_tja_text(Path(tja))
        except Exception as exc:
            QMessageBox.critical(self, "SHT Generator", f"Read TJA failed:\n{exc}"); return

        players = ([("1p", self.ck_1p)] if self.ck_1p.isChecked() else []) + \
                  ([("2p", self.ck_2p)] if self.ck_2p.isChecked() else [])
        diffs = [(l, _DIFF_COURSE[l]) for l, _ in _DIFF_LETTERS if self.ck_diff[l].isChecked()]
        if not players or not diffs:
            QMessageBox.warning(self, "SHT Generator", "Select at least one difficulty and player.")
            return
        self.log.clear()
        n_ok = n_warn = 0
        for pl, _ in players:
            for letter, course in diffs:
                name = f"{sid}{pl}_{letter}"
                try:
                    sht = tja2sht.convert_tja(tja_text, course, pl)
                    m = tja2sht.parse_sht(sht)
                    nc = len(m["notes"])
                    limit = tja2sht.DIFFICULTY_NOTE_LIMIT.get(course)
                    over = limit is not None and nc > limit
                    if self.ck_folder.isChecked():
                        d = outdir / name; d.mkdir(exist_ok=True)
                        dest = d / "sht"
                    else:
                        dest = outdir / f"{name}.sht"
                    dest.write_bytes(sht)
                    tag = ""
                    if over:
                        tag = f"  ⚠ {nc} notes EXCEEDS {course} limit {limit} — will FREEZE on this difficulty!"
                        n_warn += 1
                    self.log.appendPlainText(f"✓ {name}: {nc} notes -> {dest}{tag}")
                    logger.info("sht %s: %d notes -> %s%s", name, nc, dest,
                                " OVER-LIMIT" if over else "")
                    n_ok += 1
                except Exception as exc:
                    self.log.appendPlainText(f"✗ {name}: {exc}")
                    logger.warning("sht %s failed: %s", name, exc)
        self.log.appendPlainText(f"\nDone: {n_ok} chart(s) written"
                                 + (f", {n_warn} OVER the note limit (would freeze)." if n_warn else "."))
        if n_warn:
            QMessageBox.warning(
                self, "Over the note limit",
                f"{n_warn} chart(s) exceed their difficulty's note-buffer limit and "
                f"WILL FREEZE the game at load. Provide a lighter course for those "
                f"difficulties in the TJA (Easy≤610, Normal≤834, Hard≤3430, Oni≤4668).")


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
    which = sys.argv[1] if len(sys.argv) > 1 else "sht"
    dlg = VagGeneratorDialog() if which.lower().startswith("vag") else ShtGeneratorDialog()
    dlg.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

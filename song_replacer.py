#!/usr/bin/env python3
"""
Song Replacer — put a custom song INTO an existing slot (no new groups).

Adding a brand-new 91st song bumps the archive's group/file count and the song
DB, which the game's fixed-size song table rejects at boot. REPLACING an
existing slot keeps every count identical (group_count/file_count unchanged) and
only rewrites that slot's assets in place — structurally the same as swapping a
single file, which boots fine.

This dialog is deliberately replace-only: pick a song to overwrite, give it a
new chart / audio / title, and it stages the regenerated assets via
song_builder.build_song. The host then writes them out with the layout-preserving
full rebuild (unchanged groups keep their original sectors).
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLineEdit,
    QPushButton, QFileDialog, QCheckBox, QPlainTextEdit, QLabel, QMessageBox,
    QWidget, QProgressDialog,
)

import appconfig
import song_builder

logger = logging.getLogger("taiko.songreplacer")


class _BuildWorker(QThread):
    log_sig = Signal(str)
    done_sig = Signal(object)                     # summary dict | ("ERROR", exc, tb)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done_sig.emit(self._fn(lambda s: self.log_sig.emit(s)))
        except Exception as exc:
            import traceback
            self.done_sig.emit(("ERROR", exc, traceback.format_exc()))


class SongReplacerDialog(QDialog):
    """Replace an existing song slot's chart/audio/textures/stars in place.

    On success sets ``self.changed = True`` and stages the edits into
    ``archive.replacements``; the caller writes them out (boot-safe full rebuild).
    """

    def __init__(self, archive, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Song Replacer — overwrite an existing song")
        self.resize(640, 560)
        self.archive = archive
        self.changed = False
        self._build_ui()

    # -- ui -------------------------------------------------------------------
    def _build_ui(self):
        lay = QVBoxLayout(self)
        note = QLabel(
            "Replaces an EXISTING song slot in place — counts stay identical, so "
            "the game still boots. (Adding a brand-new song overflows the game's "
            "fixed song table and won't boot.)")
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        lay.addWidget(note)

        form = QFormLayout()
        self.cb_song = QComboBox()
        try:
            self.cb_song.addItems(song_builder.song_ids(self.archive))
        except Exception as exc:
            QMessageBox.warning(self, "Song Replacer", f"Could not list songs: {exc}")
        self.ed_title = QLineEdit()
        self.ed_lyr = QLineEdit()
        self.ed_comp = QLineEdit()
        self.ed_copy = QLineEdit("© 20XX")
        form.addRow("replace song (id):", self.cb_song)
        form.addRow("title 曲名:", self.ed_title)
        form.addRow("作詞 lyricist:", self.ed_lyr)
        form.addRow("作曲 composer:", self.ed_comp)
        form.addRow("© copyright:", self.ed_copy)

        self.ed_tja = QLineEdit(appconfig.last_existing("tja")); b_tja = QPushButton("TJA…")
        b_tja.clicked.connect(lambda: self._pick(self.ed_tja, "TJA charts (*.tja)"))
        self.ed_audio = QLineEdit(appconfig.last_existing("wav")); b_aud = QPushButton("Audio…")
        b_aud.clicked.connect(lambda: self._pick(self.ed_audio, "Audio (*.wav *.ogg)"))
        form.addRow("chart .tja:", self._row(self.ed_tja, b_tja))
        form.addRow("audio wav/ogg:", self._row(self.ed_audio, b_aud))
        self.ed_gap = QLineEdit("0")
        self.ed_gap.setToolTip(
            "Leave at 0. Sync is computed from the TJA's BPM/OFFSET and baked in "
            "automatically — feed the ORIGINAL .tja/.ogg, not files already run "
            "through test.py. Use this only to taste-tune a song that feels a hair "
            "off: + moves the music later, − earlier.")
        form.addRow("sync nudge (ms, optional):", self.ed_gap)
        lay.addLayout(form)

        opts = QHBoxLayout()
        self.ck_tex = QCheckBox("textures"); self.ck_tex.setChecked(True)
        self.ck_chart = QCheckBox("charts"); self.ck_chart.setChecked(True)
        self.ck_audio = QCheckBox("audio"); self.ck_audio.setChecked(True)
        self.ck_stars = QCheckBox("difficulty stars"); self.ck_stars.setChecked(True)
        for w in (self.ck_tex, self.ck_chart, self.ck_audio, self.ck_stars):
            opts.addWidget(w)
        opts.addStretch(1)
        lay.addLayout(opts)

        self.log = QPlainTextEdit(readOnly=True)
        lay.addWidget(QLabel("log:"))
        lay.addWidget(self.log, 1)

        btns = QHBoxLayout(); btns.addStretch(1)
        self.b_build = QPushButton("Replace → stage edits")
        self.b_build.clicked.connect(self._build)
        b_close = QPushButton("Close"); b_close.clicked.connect(self.reject)
        btns.addWidget(self.b_build); btns.addWidget(b_close)
        lay.addLayout(btns)

    def _row(self, edit, btn):
        w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1); h.addWidget(btn)
        return w

    def _pick(self, edit, filt):
        # remembers the last-used path per file type in config.ini
        p = appconfig.pick_open(self, appconfig.key_for_filter(filt),
                                "Choose file", filt)
        if p:
            edit.setText(p)

    # The old "Auto gap" button is gone on purpose: song_builder.build_song now
    # derives the sync from the TJA itself, so pre-filling the same figure here
    # applied it twice and put the whole song a measure out.

    # -- build ----------------------------------------------------------------
    def _build(self):
        sid = self.cb_song.currentText()
        if not sid:
            return
        self.log.clear()
        tja_text = None
        tja_path = self.ed_tja.text().strip()
        if tja_path:
            if not Path(tja_path).exists():
                QMessageBox.warning(self, "Song Replacer", f"TJA not found:\n{tja_path}")
                return
            try:
                tja_text, tja_warn = song_builder._read_tja_text(tja_path)
                if tja_warn:
                    self.log.appendPlainText("WARNING: " + tja_warn)
            except Exception as exc:
                QMessageBox.critical(self, "Song Replacer", f"Could not read TJA:\n{exc}")
                return
        audio_path = self.ed_audio.text().strip()
        if audio_path and not Path(audio_path).exists():
            QMessageBox.warning(self, "Song Replacer", f"Audio not found:\n{audio_path}")
            return
        audio_path = audio_path or None

        if not any((self.ck_tex.isChecked(), self.ck_chart.isChecked(),
                    self.ck_audio.isChecked(), self.ck_stars.isChecked())):
            QMessageBox.warning(self, "Song Replacer", "Nothing selected to replace.")
            return

        title = self.ed_title.text() or sid
        lyr, comp, copy = self.ed_lyr.text(), self.ed_comp.text(), self.ed_copy.text()
        do_tex, do_chart = self.ck_tex.isChecked(), self.ck_chart.isChecked()
        do_audio, do_stars = self.ck_audio.isChecked(), self.ck_stars.isChecked()
        logger.info("replacing slot '%s' (title=%r audio=%s)", sid, title, bool(audio_path))

        try:
            gap_ms = float(self.ed_gap.text().strip() or "0")
        except ValueError:
            gap_ms = 0.0

        def task(log):
            return song_builder.build_song(
                self.archive, sid, title=title, lyricist=lyr, composer=comp,
                copyright_=copy, tja_text=tja_text, audio_path=audio_path,
                do_textures=do_tex, do_charts=do_chart, do_audio=do_audio,
                do_stars=do_stars, lead_silence_ms=gap_ms, log=log)

        self.b_build.setEnabled(False)
        self._prog = QProgressDialog("Generating song assets…", None, 0, 0, self)
        self._prog.setWindowModality(Qt.WindowModal); self._prog.setMinimumDuration(0)
        self._prog.show()
        self._worker = _BuildWorker(task)
        self._worker.log_sig.connect(self.log.appendPlainText)
        self._worker.done_sig.connect(self._done)
        self._worker.start()

    def _done(self, res):
        self._prog.close()
        self.b_build.setEnabled(True)
        if isinstance(res, tuple) and res and res[0] == "ERROR":
            self.log.appendPlainText("FAILED:\n" + res[2])
            QMessageBox.critical(self, "Replace failed", str(res[1]))
            return
        summary = res or {}
        errs = summary.get("errors") or []
        msg = (f"Staged: {summary.get('textures', 0)} textures, "
               f"{summary.get('charts', 0)} charts, "
               f"audio={'yes' if summary.get('audio') else 'no'}, "
               f"stars={summary.get('stars')}")
        self.log.appendPlainText(msg)
        logger.info("replace staged — %s", msg)
        if errs:
            self.log.appendPlainText("ERRORS:\n  " + "\n  ".join(errs))
        self.changed = True
        QMessageBox.information(
            self, "Staged",
            msg + "\n\nClose this dialog; the edits will be written to DATA.000 "
            "with the boot-safe full rebuild.")

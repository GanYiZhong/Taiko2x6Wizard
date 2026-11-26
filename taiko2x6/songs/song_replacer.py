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

Two source modes:
  * TJA + audio file — a community .tja chart plus a .wav/.ogg; synced here.
  * Gen3 (Nijiiro) song folder — a …/fumen/<id> folder from a Nijiiro dump;
    charts, stars and music are decoded straight from the game's own data with
    the shared gen3_convert converter (branch-preserving, bpm-aware timing), so
    no sync is applied. Same converter the Custom Song Builder uses.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLineEdit,
    QPushButton, QFileDialog, QCheckBox, QPlainTextEdit, QLabel, QMessageBox,
    QWidget, QProgressDialog, QGroupBox,
)

from taiko2x6.app import appconfig
from taiko2x6.app.i18n import tr
from taiko2x6.songs import song_dialog_ui as UI
from taiko2x6.songs import song_builder

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
        self.setWindowTitle(tr("Song Replacer — overwrite an existing song"))
        self.resize(680, 740)
        self.archive = archive
        self.changed = False
        self._gen3 = None
        self._gen3_folder = None
        self._worker = None
        self._build_ui()
        self._update_mode()

    def _log(self, text):
        UI.log_append(self.log, text)

    # -- ui -------------------------------------------------------------------
    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        note = QLabel(tr(
            "Replaces an EXISTING song slot in place — counts stay identical, so "
            "the game still boots. (Adding a brand-new song overflows the game's "
            "fixed song table and won't boot.)"))
        note.setWordWrap(True)
        note.setStyleSheet("color:palette(mid);")
        lay.addWidget(note)

        # ---- Target ----------------------------------------------------------
        g_target = QGroupBox(tr("Target"))
        f_target = QFormLayout(g_target)
        self.cb_song = QComboBox()
        try:
            self.cb_song.addItems(song_builder.song_ids(self.archive))
        except Exception as exc:
            QMessageBox.warning(self, tr("Song Replacer"),
                                tr("Could not list songs: {err}").format(err=exc))
        f_target.addRow(tr("replace song (id):"), self.cb_song)
        lay.addWidget(g_target)

        # ---- Source ----------------------------------------------------------
        g_src = QGroupBox(tr("Source"))
        f_src = QFormLayout(g_src)
        self.cb_mode = QComboBox()
        self.cb_mode.addItems([tr("TJA + audio file"), tr("Gen3 (Nijiiro) song folder")])
        self.cb_mode.setToolTip(tr(
            "TJA: a community .tja chart + .wav/.ogg audio (synced here).\n"
            "Gen3: point at a Nijiiro song folder (…/fumen/<id>). Charts, stars "
            "and music are decoded from the game's own data — no sync needed, "
            "and branching/tempo timing is preserved."))
        self.cb_mode.currentIndexChanged.connect(self._update_mode)
        f_src.addRow(tr("source mode:"), self.cb_mode)

        # --- TJA-mode rows ---
        self.ed_tja = QLineEdit(appconfig.last_existing("tja"))
        b_tja = QPushButton(tr("TJA…"))
        b_tja.clicked.connect(lambda: self._pick(self.ed_tja, "TJA charts (*.tja)"))
        self.ed_audio = QLineEdit(appconfig.last_existing("wav"))
        b_aud = QPushButton(tr("Audio…"))
        b_aud.clicked.connect(lambda: self._pick(self.ed_audio, "Audio (*.wav *.ogg)"))
        self.row_tja = self._row(self.ed_tja, b_tja)
        self.row_audio = self._row(self.ed_audio, b_aud)
        f_src.addRow(tr("chart .tja:"), self.row_tja)
        self.lbl_tja = f_src.labelForField(self.row_tja)
        f_src.addRow(tr("audio wav/ogg:"), self.row_audio)
        self.lbl_audio = f_src.labelForField(self.row_audio)
        self.ed_gap = QLineEdit("0")
        self.ed_gap.setToolTip(tr(
            "Leave at 0. Sync is computed from the TJA's BPM/OFFSET and baked in "
            "automatically — feed the ORIGINAL .tja/.ogg, not files already run "
            "through test.py. Use this only to taste-tune a song that feels a hair "
            "off: + moves the music later, − earlier."))
        f_src.addRow(tr("sync nudge (ms, optional):"), self.ed_gap)
        self.lbl_gap = f_src.labelForField(self.ed_gap)

        # --- Gen3-mode row ---
        self.ed_gen3 = QLineEdit()
        b_g3 = QPushButton(tr("Folder…"))
        self.ed_gen3.setPlaceholderText(tr("…/fumen/<song id>  (a Nijiiro song folder)"))
        b_g3.clicked.connect(self._pick_gen3)
        self.row_gen3 = self._row(self.ed_gen3, b_g3)
        f_src.addRow(tr("Gen3 song folder:"), self.row_gen3)
        self.lbl_gen3 = f_src.labelForField(self.row_gen3)

        # parsed-chart preview
        self.lbl_summary = QLabel()
        self.lbl_summary.setWordWrap(True)
        self.tbl_summary = UI.SummaryTable()
        self.tbl_summary.setVisible(False)
        f_src.addRow(self.lbl_summary)
        f_src.addRow(self.tbl_summary)
        lay.addWidget(g_src)

        # ---- Song info -------------------------------------------------------
        g_info = QGroupBox(tr("Song info"))
        f_info = QFormLayout(g_info)
        self.ed_title = QLineEdit()
        self.ed_lyr = QLineEdit()
        self.ed_comp = QLineEdit()
        self.ed_copy = QLineEdit("© 20XX")
        f_info.addRow(tr("title 曲名:"), self.ed_title)
        f_info.addRow(tr("作詞 lyricist:"), self.ed_lyr)
        f_info.addRow(tr("作曲 composer:"), self.ed_comp)
        f_info.addRow(tr("© copyright:"), self.ed_copy)
        lay.addWidget(g_info)

        # ---- Output ----------------------------------------------------------
        g_out = QGroupBox(tr("Output"))
        opts = QHBoxLayout(g_out)
        self.ck_tex = QCheckBox(tr("textures")); self.ck_tex.setChecked(True)
        self.ck_chart = QCheckBox(tr("charts")); self.ck_chart.setChecked(True)
        self.ck_audio = QCheckBox(tr("audio")); self.ck_audio.setChecked(True)
        self.ck_stars = QCheckBox(tr("difficulty stars")); self.ck_stars.setChecked(True)
        for w in (self.ck_tex, self.ck_chart, self.ck_audio, self.ck_stars):
            opts.addWidget(w)
            w.toggled.connect(self._validate)
        opts.addStretch(1)
        lay.addWidget(g_out)

        # ---- Log + status + buttons -----------------------------------------
        self.log = UI.make_log()
        lay.addWidget(QLabel(tr("log:")))
        lay.addWidget(self.log, 1)
        self.lbl_status = UI.status_label()
        lay.addWidget(self.lbl_status)
        box, self.b_build = UI.make_buttons(
            tr("Replace → stage edits"), tr("Close"), self._build, self.reject)
        lay.addWidget(box)

        for e in (self.ed_tja, self.ed_audio, self.ed_gen3, self.ed_gap):
            e.textChanged.connect(self._validate)
        self.ed_tja.textChanged.connect(self._refresh_tja_summary)
        self.cb_song.currentIndexChanged.connect(self._validate)

    def _row(self, edit, btn):
        return UI.path_row(edit, btn)

    def _pick(self, edit, filt):
        # remembers the last-used path per file type in config.ini
        p = appconfig.pick_open(self, appconfig.key_for_filter(filt),
                                tr("Choose file"), filt)
        if p:
            edit.setText(p)

    # -- validation -----------------------------------------------------------
    def _validate(self, *_):
        """Mark missing/bad inputs inline; Replace stays off until clean."""
        if not hasattr(self, "b_build"):
            return
        problems = []
        g3 = self._is_gen3()
        if not self.cb_song.currentText():
            problems.append(tr("No target song in this archive."))
        if not any((self.ck_tex.isChecked(), self.ck_chart.isChecked(),
                    self.ck_audio.isChecked(), self.ck_stars.isChecked())):
            problems.append(tr("Nothing selected to replace."))
        if g3:
            folder = self.ed_gen3.text().strip()
            bad = not folder or not Path(folder).is_dir()
            UI.mark_bad(self.ed_gen3, bad, tr("Choose an existing Gen3 song folder."))
            if bad:
                problems.append(tr("Choose a Gen3 song folder."))
            for e in (self.ed_tja, self.ed_audio, self.ed_gap):
                UI.mark_bad(e, False)
        else:
            UI.mark_bad(self.ed_gen3, False)
            for e, what in ((self.ed_tja, tr("TJA file")),
                            (self.ed_audio, tr("Audio file"))):
                msg = UI.file_problem(e.text(), what)
                UI.mark_bad(e, bool(msg), msg or "")
                if msg:
                    problems.append(msg)
            try:
                float(self.ed_gap.text().strip() or "0")
                UI.mark_bad(self.ed_gap, False)
            except ValueError:
                UI.mark_bad(self.ed_gap, True, tr("Enter a number of milliseconds."))
                problems.append(tr("Sync nudge must be a number."))
        UI.set_status(self.lbl_status, problems)
        self.b_build.setEnabled(self._worker is None and not problems)

    def _refresh_tja_summary(self, *_):
        """Parse the chosen .tja and show title/BPM/courses before building."""
        if self._is_gen3():
            return
        path = self.ed_tja.text().strip()
        if not path or not Path(path).is_file():
            self.lbl_summary.setText("")
            self.tbl_summary.set_rows([])
            return
        try:
            text, warn = song_builder._read_tja_text(path)
        except Exception as exc:
            self.lbl_summary.setText(tr("Could not read TJA: {err}").format(err=exc))
            self.tbl_summary.set_rows([])
            return
        s = UI.summarize_tja(text)
        warns = ([warn] if warn else []) + s["warnings"]
        line = UI.tja_summary_line(s)
        if warns:
            line += "\n" + "\n".join("⚠ " + w for w in warns)
        self.lbl_summary.setText(line)
        self.tbl_summary.set_rows(s["rows"])
        if s["title"] and not self.ed_title.text().strip():
            self.ed_title.setPlaceholderText(s["title"])

    # -- mode -----------------------------------------------------------------
    def _is_gen3(self):
        return self.cb_mode.currentIndex() == 1

    def _update_mode(self, *_):
        g3 = self._is_gen3()
        for w in (self.row_tja, self.lbl_tja, self.row_audio, self.lbl_audio,
                  self.ed_gap, self.lbl_gap):
            w.setVisible(not g3)
        for w in (self.row_gen3, self.lbl_gen3):
            w.setVisible(g3)
        # Gen3 audio is decoded from the folder, not a separate file.
        self.ck_audio.setText(tr("audio") if not g3 else tr("audio (from Gen3 folder)"))
        if g3:
            self._show_gen3_summary(self._gen3)
        else:
            self._refresh_tja_summary()
        self._validate()

    def _pick_gen3(self):
        d = appconfig.pick_dir(self, "gen3_dir",
                               tr("Choose a Gen3 song folder (…/fumen/<id>)"))
        if not d:
            return
        self.ed_gen3.setText(d)
        self._load_gen3_preview(d)

    def _show_gen3_summary(self, song):
        if not song:
            self.lbl_summary.setText("")
            self.tbl_summary.set_rows([])
            return
        self.lbl_summary.setText(
            tr("Title: {t}").format(t=song.get("title") or song.get("sid")))
        stars = song.get("stars") or {}
        onpu = song.get("onpu") or {}
        self.tbl_summary.set_rows(
            [(c, stars.get(c), onpu.get(c)) for c in sorted(song.get("sht") or {})])

    def _load_gen3_preview(self, folder):
        try:
            from taiko2x6.charts import gen3_song
            song = gen3_song.load_song(folder)
        except Exception as exc:
            self._log(tr("Gen3: {err}").format(err=exc))
            QMessageBox.warning(self, tr("Gen3 song"), str(exc))
            return
        self._gen3 = song
        self._gen3_folder = folder
        if song.get("title") and not self.ed_title.text().strip():
            self.ed_title.setText(song["title"])
        self._log(
            tr("Gen3 song {sid} — {title}\n  stars: {stars}\n  charts: {charts}\n  audio: {audio}").format(
                sid=song["sid"], title=repr(song.get("title")), stars=song.get("stars"),
                charts={k: len(v) for k, v in song.get("sht", {}).items()},
                audio=tr("yes") if song.get("audio_path") else tr("NONE")))
        self._show_gen3_summary(song)

    # -- build ----------------------------------------------------------------
    def _build(self):
        sid = self.cb_song.currentText()
        if not sid:
            return
        if not any((self.ck_tex.isChecked(), self.ck_chart.isChecked(),
                    self.ck_audio.isChecked(), self.ck_stars.isChecked())):
            QMessageBox.warning(self, tr("Song Replacer"), tr("Nothing selected to replace."))
            return
        if self._is_gen3():
            self._build_gen3(sid)
        else:
            self._build_tja(sid)

    def _build_tja(self, sid):
        self.log.clear()
        tja_text = None
        tja_path = self.ed_tja.text().strip()
        if tja_path:
            if not Path(tja_path).exists():
                QMessageBox.warning(self, tr("Song Replacer"),
                                    tr("TJA not found:\n{path}").format(path=tja_path))
                return
            try:
                tja_text, tja_warn = song_builder._read_tja_text(tja_path)
                if tja_warn:
                    self._log("WARNING: " + tja_warn)
            except Exception as exc:
                QMessageBox.critical(self, tr("Song Replacer"),
                                     tr("Could not read TJA:\n{err}").format(err=exc))
                return
        audio_path = self.ed_audio.text().strip()
        if audio_path and not Path(audio_path).exists():
            QMessageBox.warning(self, tr("Song Replacer"),
                                tr("Audio not found:\n{path}").format(path=audio_path))
            return
        audio_path = audio_path or None

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

        self._run(task, tr("Generating song assets…"))

    def _build_gen3(self, sid):
        self.log.clear()
        folder = self.ed_gen3.text().strip()
        if not folder:
            QMessageBox.warning(self, tr("Gen3 song"), tr("Choose a Gen3 song folder."))
            return
        if not Path(folder).exists():
            QMessageBox.warning(self, tr("Gen3 song"),
                                tr("Folder not found:\n{path}").format(path=folder))
            return

        typed_title = self.ed_title.text().strip()
        lyr, comp, copy = self.ed_lyr.text(), self.ed_comp.text(), self.ed_copy.text()
        do_tex, do_chart = self.ck_tex.isChecked(), self.ck_chart.isChecked()
        do_audio, do_stars = self.ck_audio.isChecked(), self.ck_stars.isChecked()
        logger.info("replacing slot '%s' from Gen3 folder %r", sid, folder)

        def task(log):
            import os
            from taiko2x6.charts import gen3_song
            from taiko2x6.charts import gen3_convert
            # Reuse the preview if it is for this exact folder, else load fresh.
            song = self._gen3 if self._gen3_folder == folder else None
            if song is None:
                song = gen3_song.load_song(folder, log=log)
            charts = dict(song.get("sht") or {})
            if not charts:
                raise ValueError(tr("no charts found in this Gen3 folder"))
            stars = gen3_song.stars_list(song)      # None if musicinfo has none
            audio_vag = None
            if do_audio:
                ap = song.get("audio_path")
                if ap:
                    log("audio: decoding %s" % os.path.basename(ap))
                    audio_vag = gen3_convert.convert_audio(ap)
                    log("audio: %d bytes of VAG" % len(audio_vag))
                else:
                    log(tr("audio: none in this Gen3 folder — slot audio left as-is"))
            title = typed_title or song.get("title") or sid
            return song_builder.build_song(
                self.archive, sid, title=title, lyricist=lyr, composer=comp,
                copyright_=copy,
                charts=(charts if do_chart else None),
                audio_vag=audio_vag,
                stars=(stars if do_stars else None),
                do_textures=do_tex, do_charts=do_chart,
                do_audio=do_audio and audio_vag is not None,
                do_stars=do_stars, log=log)

        self._run(task, tr("Decoding Gen3 song → assets…"))

    def _run(self, task, wait_msg):
        self.b_build.setEnabled(False)
        self._prog = QProgressDialog(wait_msg, None, 0, 0, self)
        self._prog.setWindowModality(Qt.WindowModal)
        self._prog.setCancelButton(None)
        self._prog.setMinimumDuration(0)
        self._prog.show()
        self._worker = _BuildWorker(task)
        self._worker.log_sig.connect(self._log)
        self._worker.done_sig.connect(self._done)
        self._worker.start()

    def _done(self, res):
        self._prog.close()
        w, self._worker = self._worker, None
        if w is not None:
            w.deleteLater()
        self._validate()
        if isinstance(res, tuple) and res and res[0] == "ERROR":
            self._log("FAILED:\n" + res[2])
            QMessageBox.critical(self, tr("Replace failed"), str(res[1]))
            return
        summary = res or {}
        errs = summary.get("errors") or []
        msg = tr("Staged: {tex} textures, {charts} charts, audio={audio}, stars={stars}").format(
            tex=summary.get("textures", 0), charts=summary.get("charts", 0),
            audio=tr("yes") if summary.get("audio") else tr("no"),
            stars=summary.get("stars"))
        self._log(msg)
        for w in (summary.get("warnings") or []):
            self._log("WARNING: " + w)
        logger.info("replace staged — %s", msg)
        if errs:
            self._log(tr("ERRORS:") + "\n  " + "\n  ".join(errs))
        self.changed = True
        QMessageBox.information(
            self, tr("Staged"),
            msg + "\n\n" + tr("Close this dialog; the edits will be written to DATA.000 "
                              "with the boot-safe full rebuild."))

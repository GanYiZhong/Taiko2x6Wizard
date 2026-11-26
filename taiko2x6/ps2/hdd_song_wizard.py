#!/usr/bin/env python3
"""
HDD Song Wizard — one-click custom song for a PS2 arcade HDD (Taiko 14 etc).

End to end:
  1. extract DATA.000 + list.bin from a PFS partition of the .img,
  2. add a new song (textures + charts + VAG + DB stars) via song_builder,
  3. write the grown DATA.000 + list.bin back into the .img (PFS allocates the
     extra zones).

The .img is modified in place inside ps2hdd.write_session: everything it
overwrites is saved to an undo journal first, and a failed write is rolled back
automatically. Extraction caches to temp files so the song list / template
combo can be populated before building.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QSettings
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLineEdit,
    QPushButton, QFileDialog, QPlainTextEdit, QLabel, QMessageBox, QProgressDialog,
    QWidget,
)

from taiko2x6.app import appconfig
from taiko2x6.app.i18n import tr
from taiko2x6.ps2 import ps2hdd
from taiko2x6.ps2.hdd_browser import friendly_error, human_size
from taiko2x6.songs import song_builder
from taiko2x6.archive import archive_model as gui

# Child of the app's "taiko" logger — inherits its console handler under the GUI.
logger = logging.getLogger("taiko.hddwizard")


def input_problem(new_id: str, template: str, ids, tja: str = "", audio: str = "") -> str:
    """First reason the song inputs cannot be built yet ('' when they can).

    `ids` are the song ids already in the archive; an existing id would clash.
    The TJA and audio are optional, but if given they must exist."""
    new_id = (new_id or "").strip()
    if not new_id:
        return tr("Enter a new song id.")
    if any(c.isspace() for c in new_id) or "/" in new_id or "\\" in new_id:
        return tr("The song id must not contain spaces or slashes.")
    if new_id in set(ids or ()):
        return tr("Song id '{id}' already exists in this partition.").format(id=new_id)
    if not (template or "").strip():
        return tr("Pick a template song.")
    if (template or "").strip() not in set(ids or ()):
        return tr("Template '{id}' is not a song in this partition.").format(id=template.strip())
    if tja and not Path(tja).is_file():
        return tr("The chart .tja file was not found.")
    if audio and not Path(audio).is_file():
        return tr("The audio file was not found.")
    return ""


class _Worker(QThread):
    done = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn(lambda s: None))
        except Exception as exc:
            import traceback
            self.done.emit(("ERROR", exc, traceback.format_exc()))


class HddSongWizard(QDialog):
    def __init__(self, parent=None, default_img=""):
        super().__init__(parent)
        self.setWindowTitle(tr("HDD Song Wizard — add a song into a PS2 HDD"))
        self.resize(680, 600)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="taikohdd_"))
        self._list = self._tmpdir / "list.bin"
        self._data = self._tmpdir / "DATA.000"
        self.archive = None
        self._img = ""
        self._part = ""
        self._ids: list = []
        self._opened = ""
        self._busy_now = False
        self._build_ui()
        default_img = default_img or appconfig.last_existing("hddimg")
        if default_img and Path(default_img).exists():
            self._set_img(default_img)
        self._refresh_state()

    # -- ui -------------------------------------------------------------------
    def _build_ui(self):
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.ed_img = QLineEdit(); b_img = QPushButton("…")
        b_img.clicked.connect(self._pick_img)
        self.ed_img.editingFinished.connect(self._img_edited)
        form.addRow(tr("HDD .img:"), self._row(self.ed_img, b_img))
        self.cb_part = QComboBox(); self.cb_part.setEnabled(False)
        self.b_load = QPushButton(tr("Load partition"))
        self.b_load.clicked.connect(self._load_partition)
        self.cb_part.currentIndexChanged.connect(self._refresh_state)
        form.addRow(tr("partition:"), self._row(self.cb_part, self.b_load))

        self.cb_template = QComboBox(); self.cb_template.setEnabled(False)
        self.cb_template.setEditable(True)
        self.ed_newid = QLineEdit(); self.ed_title = QLineEdit()
        self.ed_lyr = QLineEdit(); self.ed_comp = QLineEdit()
        self.ed_copy = QLineEdit(appconfig.get("hddwizard", "copyright", "© 20XX"))
        self.ed_lyr.setText(appconfig.get("hddwizard", "lyricist"))
        self.ed_comp.setText(appconfig.get("hddwizard", "composer"))
        self.ed_tja = QLineEdit(); b_tja = QPushButton(tr("TJA…"))
        b_tja.clicked.connect(lambda: self._pick(self.ed_tja, "TJA (*.tja)"))
        self.ed_audio = QLineEdit(); b_aud = QPushButton(tr("Audio…"))
        b_aud.clicked.connect(lambda: self._pick(self.ed_audio, "Audio (*.wav *.ogg)"))
        form.addRow(tr("template song:"), self.cb_template)
        form.addRow(tr("new song id:"), self.ed_newid)
        form.addRow(tr("title 曲名:"), self.ed_title)
        form.addRow(tr("作詞 lyricist:"), self.ed_lyr)
        form.addRow(tr("作曲 composer:"), self.ed_comp)
        form.addRow(tr("© copyright:"), self.ed_copy)
        form.addRow(tr("chart .tja:"), self._row(self.ed_tja, b_tja))
        form.addRow(tr("audio wav/ogg:"), self._row(self.ed_audio, b_aud))
        lay.addLayout(form)
        for w in (self.ed_newid, self.ed_tja, self.ed_audio):
            w.textChanged.connect(self._refresh_state)
        self.cb_template.currentTextChanged.connect(self._refresh_state)

        self.lbl_state = QLabel(); self.lbl_state.setWordWrap(True)
        lay.addWidget(self.lbl_state)

        self.log = QPlainTextEdit(readOnly=True)
        lay.addWidget(QLabel(tr("log:")))
        lay.addWidget(self.log, 1)

        btns = QHBoxLayout(); btns.addStretch(1)
        self.b_run = QPushButton(tr("Build && write back to HDD"))
        self.b_run.setEnabled(False); self.b_run.clicked.connect(self._run)
        b_close = QPushButton(tr("Close")); b_close.clicked.connect(self.accept)
        btns.addWidget(self.b_run); btns.addWidget(b_close)
        lay.addLayout(btns)

    def _row(self, w, b):
        c = QWidget(); h = QHBoxLayout(c); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(w, 1); h.addWidget(b)
        return c

    def _pick(self, e, filt):
        # remembers the last-used path per file type in config.ini
        p = appconfig.pick_open(self, appconfig.key_for_filter(filt),
                                tr("Choose"), filt)
        if p:
            e.setText(p)

    @staticmethod
    def _read_tja_text(path: Path) -> str:
        """Decode a TJA file, trying the encodings Taiko charts actually use.

        TJA files are commonly Shift-JIS/CP932; forcing UTF-8 would turn Japanese
        metadata into U+FFFD. Try strict UTF-8/Shift-JIS first, only fall back to
        a lossy decode if none succeed."""
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "utf-8", "shift_jis", "cp932"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("cp932", errors="replace")

    def _pick_img(self):
        p = appconfig.pick_open(self, "hddimg", tr("PS2 HDD image"),
                                tr("HDD image (*.img *.raw *.bin);;All (*)"))
        if p:
            self._set_img(p)

    def _img_edited(self):
        p = self.ed_img.text().strip()
        if p and p != self._opened and Path(p).is_file() and not self._busy_now:
            self._set_img(p)

    def _set_img(self, path):
        self.ed_img.setText(path)
        # a different image invalidates whatever was extracted before
        self._ids = []
        self._img = self._part = ""
        self._opened = path
        self.cb_template.clear(); self.cb_template.setEnabled(False)
        try:
            h = ps2hdd.Ps2Hdd(path)
            parts = [p for p in h.partitions()
                     if p.get("is_pfs") and not p["name"].startswith("__")]
            h.close()
        except Exception as exc:
            QMessageBox.critical(self, tr("Open failed"), friendly_error(exc))
            self._refresh_state()
            return
        try:
            QSettings("TaikoTools", "SYSTEM256Explorer").setValue("recent_hdd_img", path)
        except Exception:
            pass
        appconfig.remember("hddimg", path)
        self.cb_part.blockSignals(True)
        self.cb_part.clear()
        for p in parts:
            self.cb_part.addItem(p["name"])
        saved = appconfig.get("hddwizard", "partition")
        i = self.cb_part.findText(saved)
        if i >= 0:
            self.cb_part.setCurrentIndex(i)
        self.cb_part.blockSignals(False)
        self.cb_part.setEnabled(bool(parts))
        self.log.appendPlainText(tr("opened {img}\npartitions: {parts}").format(
            img=path, parts=[self.cb_part.itemText(i) for i in range(self.cb_part.count())]))
        self._refresh_state()

    # -- load partition (extract DATA.000 + list.bin) -------------------------
    def _load_partition(self):
        img = self.ed_img.text(); part = self.cb_part.currentText()
        if not img or not part:
            return
        if not Path(img).is_file():
            QMessageBox.warning(self, tr("HDD Song Wizard"), tr("The HDD image file was not found."))
            return
        self._img, self._part = img, part
        appconfig.set_value("hddwizard", "partition", part)
        logger.info("extracting DATA.000 + list.bin from %s (%s)…", part, img)

        def task(log):
            t0 = time.perf_counter()
            h = ps2hdd.Ps2Hdd(img)
            self._list.write_bytes(h.pfs_read(part, "/list.bin"))
            self._data.write_bytes(h.pfs_read(part, "/DATA.000"))
            h.close()
            logger.info("extracted list.bin %d B + DATA.000 %d B in %.1fs",
                        self._list.stat().st_size, self._data.stat().st_size,
                        time.perf_counter() - t0)
            return True

        self._busy(tr("Extracting DATA.000 + list.bin from {part} (this is large, ~1 min)…")
                   .format(part=part), task, self._after_load)

    def _after_load(self, res):
        if self._err(res):
            return
        try:
            if self.archive:
                self.archive.close()
            self.archive = gui.Archive(self._list, self._data, fmt=2)
            ids = song_builder.song_ids(self.archive)
        except Exception as exc:
            QMessageBox.critical(self, tr("Parse failed"), str(exc))
            return
        self._ids = list(ids)
        self.cb_template.clear()
        self.cb_template.addItems(ids)
        self.cb_template.setEnabled(True)
        self.log.appendPlainText(
            tr("extracted: list.bin {lb}, DATA.000 {data}\n"
               "{n} songs in {part}; pick a template + fill the new song.").format(
                lb=human_size(self._list.stat().st_size),
                data=human_size(self._data.stat().st_size),
                n=len(ids), part=self._part))
        self._refresh_state()

    # -- build + write back ---------------------------------------------------
    def _run(self):
        if not self.archive:
            return
        new_id = self.ed_newid.text().strip()
        tmpl = self.cb_template.currentText().strip()
        problem = input_problem(new_id, tmpl, self._ids, self.ed_tja.text(), self.ed_audio.text())
        if problem:
            QMessageBox.warning(self, tr("HDD Song Wizard"), problem)
            return
        tja = None
        if self.ed_tja.text() and Path(self.ed_tja.text()).exists():
            tja = self._read_tja_text(Path(self.ed_tja.text()))
        if QMessageBox.warning(
                self, tr("Write back to HDD"),
                tr("This will add song '{id}' and write the grown DATA.000 + "
                   "list.bin back into:\n{img}\n(partition {part})\n\n"
                   "The image is modified in place. {note}\n\n"
                   "Close PCSX2 first. Continue?").format(
                    id=new_id, img=self._img, part=self._part, note=ps2hdd.UNDO_NOTE),
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        appconfig.set_value("hddwizard", "lyricist", self.ed_lyr.text())
        appconfig.set_value("hddwizard", "composer", self.ed_comp.text())
        appconfig.set_value("hddwizard", "copyright", self.ed_copy.text())
        # DB prep on the GUI thread (SongManager is a QWidget)
        try:
            db_pre, stars = song_builder.prepare_new_song_db(self.archive, new_id, tmpl, tja)
        except Exception as exc:
            QMessageBox.critical(self, tr("Add song"), str(exc))
            return

        img, part = self._img, self._part
        new_id_l = new_id
        title = self.ed_title.text() or new_id
        lyr, comp, copy = self.ed_lyr.text(), self.ed_comp.text(), self.ed_copy.text()
        audio = self.ed_audio.text() or None

        def task(log):
            t0 = time.perf_counter()
            logger.info("building song '%s' (template %s)…", new_id_l, tmpl)
            lb, data, summ = song_builder.add_new_song(
                self.archive, new_id_l, title=title, template_sid=tmpl,
                lyricist=lyr, composer=comp, copyright=copy, tja_text=tja,
                audio_path=audio, precomputed_db=db_pre, stars=stars, log=lambda s: None)
            self.archive.close(); self.archive = None      # release temp mmap
            logger.info("writing DATA.000 %d B + list.bin %d B back into %s…",
                        len(data), len(lb), part)
            # DATA.000 then list.bin inside one undo-journaled session: if
            # either write raises, the image is rolled back to its old pair.
            # A hard crash mid-write cannot roll back by itself; the journal
            # path below restores it (python -m taiko2x6.safeio rollback ...).
            with ps2hdd.write_session(img, log=logger.info) as h:
                h.pfs_write(part, "/DATA.000", data)
                h.pfs_write(part, "/list.bin", lb)
            logger.info("HDD write-back complete in %.1fs", time.perf_counter() - t0)
            return summ, len(lb), len(data), h.undo_path

        self._busy(tr("Adding song + writing back to the HDD image (large, please wait)…"),
                   task, self._after_write)

    def _after_write(self, res):
        if self._err(res):
            # try to reopen archive for another attempt
            try:
                self.archive = gui.Archive(self._list, self._data, fmt=2)
            except Exception:
                pass
            self._refresh_state()
            return
        summ, nl, nd, undo = res
        self.log.appendPlainText(
            tr("\nDONE — wrote into {img}\n"
               "  undo journal (restores the old DATA.000/list.bin): {undo}\n"
               "  list.bin {lb}, DATA.000 {data}\n"
               "  groups added: {groups}, select files: {files}, "
               "audio: {audio}, db: {db}").format(
                img=self._img, undo=undo, lb=human_size(nl), data=human_size(nd),
                groups=summ["groups"], files=summ["extra_files"],
                audio=tr("yes") if summ["audio"] else tr("no"), db=summ["db"]))
        if summ["errors"]:
            self.log.appendPlainText(tr("ERRORS:") + "\n  " + "\n  ".join(summ["errors"]))
        QMessageBox.information(self, tr("Done"),
                                tr("Song added and written back into the HDD image."))
        # re-extract so the wizard reflects the new state for further adds
        self._load_partition()

    # -- worker plumbing ------------------------------------------------------
    def _busy(self, msg, fn, on_done):
        self._busy_now = True
        self._refresh_state()
        self._prog = QProgressDialog(msg, None, 0, 0, self)
        self._prog.setWindowModality(Qt.WindowModal); self._prog.setCancelButton(None)
        self._prog.setMinimumDuration(0); self._prog.show()
        self._worker = _Worker(fn)

        def done(r):
            self._prog.close()
            self._busy_now = False
            self._refresh_state()
            on_done(r)
        self._worker.done.connect(done)
        self._worker.start()

    def _err(self, res):
        if isinstance(res, tuple) and res and res[0] == "ERROR":
            self.log.appendPlainText(tr("FAILED:") + "\n" + res[2])
            QMessageBox.critical(self, tr("Failed"), friendly_error(res[1]))
            return True
        return False

    # -- state ----------------------------------------------------------------
    def _refresh_state(self, *_):
        """Show which steps are done and enable the action only when valid."""
        img = self.ed_img.text().strip()
        have_img = bool(img) and Path(img).is_file()
        have_part = self.cb_part.count() > 0 and bool(self.cb_part.currentText())
        loaded = self.archive is not None and bool(self._ids) and not self._busy_now
        self.b_load.setEnabled(have_img and have_part and not self._busy_now)
        problem = input_problem(self.ed_newid.text(), self.cb_template.currentText(),
                                self._ids, self.ed_tja.text().strip(),
                                self.ed_audio.text().strip()) if loaded else ""
        self.b_run.setEnabled(loaded and not problem)
        mark = lambda ok: "✓" if ok else "○"
        lines = [f"{mark(have_img)} " + tr("1. Choose the HDD image"),
                 f"{mark(loaded)} " + tr("2. Load the partition"),
                 f"{mark(loaded and not problem)} " + tr("3. Fill in the new song")]
        text = "   ".join(lines)
        if self._busy_now:
            hint = tr("Working…")
        elif not have_img:
            hint = tr("Choose a PS2 HDD image (.img).")
        elif not loaded:
            hint = tr("Load the partition to read its songs.")
        else:
            hint = problem or tr("Ready: Build & write back to HDD.")
        self.lbl_state.setText(text + "\n" + hint)

    def closeEvent(self, ev):
        # A running worker may still be reading the temp DATA.000/list.bin via the
        # archive mmap (or building from them). Wait for it before closing the
        # archive and deleting the temp dir, or we free files still in use.
        w = getattr(self, "_worker", None)
        if w is not None and w.isRunning():
            w.wait()
        if self.archive:
            self.archive.close()
            self.archive = None
        # rmtree handles any extra scratch files song_builder may have written,
        # so the temp dir never leaks on a stray file (plain rmdir would raise).
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        super().closeEvent(ev)

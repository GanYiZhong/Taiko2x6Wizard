#!/usr/bin/env python3
"""
HDD Song Wizard — one-click custom song for a PS2 arcade HDD (Taiko 14 etc).

End to end:
  1. extract DATA.000 + list.bin from a PFS partition of the .img,
  2. add a new song (textures + charts + VAG + DB stars) via song_builder,
  3. write the grown DATA.000 + list.bin back into the .img (PFS allocates the
     extra zones).

The .img is modified in place; there is no automatic 80 GB backup, so the wizard
warns before writing. Extraction caches to temp files so the song list / template
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

import appconfig
import ps2hdd
import song_builder
import taiko256_explorer_gui6 as gui

# Child of the app's "taiko" logger — inherits its console handler under the GUI.
logger = logging.getLogger("taiko.hddwizard")


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
        self.setWindowTitle("HDD Song Wizard — add a song into a PS2 HDD")
        self.resize(680, 600)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="taikohdd_"))
        self._list = self._tmpdir / "list.bin"
        self._data = self._tmpdir / "DATA.000"
        self.archive = None
        self._img = ""
        self._part = ""
        self._build_ui()
        if default_img and Path(default_img).exists():
            self._set_img(default_img)

    # -- ui -------------------------------------------------------------------
    def _build_ui(self):
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.ed_img = QLineEdit(); b_img = QPushButton("…")
        b_img.clicked.connect(self._pick_img)
        form.addRow("HDD .img:", self._row(self.ed_img, b_img))
        self.cb_part = QComboBox(); self.cb_part.setEnabled(False)
        b_load = QPushButton("Load partition")
        b_load.clicked.connect(self._load_partition)
        form.addRow("partition:", self._row(self.cb_part, b_load))

        self.cb_template = QComboBox(); self.cb_template.setEnabled(False)
        self.cb_template.setEditable(True)
        self.ed_newid = QLineEdit(); self.ed_title = QLineEdit()
        self.ed_lyr = QLineEdit(); self.ed_comp = QLineEdit()
        self.ed_copy = QLineEdit("© 20XX")
        self.ed_tja = QLineEdit(); b_tja = QPushButton("TJA…")
        b_tja.clicked.connect(lambda: self._pick(self.ed_tja, "TJA (*.tja)"))
        self.ed_audio = QLineEdit(); b_aud = QPushButton("Audio…")
        b_aud.clicked.connect(lambda: self._pick(self.ed_audio, "Audio (*.wav *.ogg)"))
        form.addRow("template song:", self.cb_template)
        form.addRow("new song id:", self.ed_newid)
        form.addRow("title 曲名:", self.ed_title)
        form.addRow("作詞 lyricist:", self.ed_lyr)
        form.addRow("作曲 composer:", self.ed_comp)
        form.addRow("© copyright:", self.ed_copy)
        form.addRow("chart .tja:", self._row(self.ed_tja, b_tja))
        form.addRow("audio wav/ogg:", self._row(self.ed_audio, b_aud))
        lay.addLayout(form)

        self.log = QPlainTextEdit(readOnly=True)
        lay.addWidget(QLabel("log:"))
        lay.addWidget(self.log, 1)

        btns = QHBoxLayout(); btns.addStretch(1)
        self.b_run = QPushButton("Build && write back to HDD")
        self.b_run.setEnabled(False); self.b_run.clicked.connect(self._run)
        b_close = QPushButton("Close"); b_close.clicked.connect(self.accept)
        btns.addWidget(self.b_run); btns.addWidget(b_close)
        lay.addLayout(btns)

    def _row(self, w, b):
        c = QWidget(); h = QHBoxLayout(c); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(w, 1); h.addWidget(b)
        return c

    def _pick(self, e, filt):
        # remembers the last-used path per file type in config.ini
        p = appconfig.pick_open(self, appconfig.key_for_filter(filt),
                                "Choose", filt)
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
        p = appconfig.pick_open(self, "hddimg", "PS2 HDD image",
                                "HDD image (*.img *.raw *.bin);;All (*)")
        if p:
            self._set_img(p)

    def _set_img(self, path):
        self.ed_img.setText(path)
        try:
            h = ps2hdd.Ps2Hdd(path)
            parts = [p for p in h.partitions()
                     if p.get("is_pfs") and not p["name"].startswith("__")]
            h.close()
        except Exception as exc:
            QMessageBox.critical(self, "Open failed", str(exc))
            return
        try:
            QSettings("TaikoTools", "SYSTEM256Explorer").setValue("recent_hdd_img", path)
        except Exception:
            pass
        self.cb_part.clear()
        for p in parts:
            self.cb_part.addItem(p["name"])
        self.cb_part.setEnabled(True)
        self.log.appendPlainText(f"opened {path}\npartitions: {[self.cb_part.itemText(i) for i in range(self.cb_part.count())]}")

    # -- load partition (extract DATA.000 + list.bin) -------------------------
    def _load_partition(self):
        img = self.ed_img.text(); part = self.cb_part.currentText()
        if not img or not part:
            return
        self._img, self._part = img, part
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

        self._busy(f"Extracting DATA.000 + list.bin from {part} (this is large, ~1 min)…",
                   task, self._after_load)

    def _after_load(self, res):
        if self._err(res):
            return
        try:
            if self.archive:
                self.archive.close()
            self.archive = gui.Archive(self._list, self._data, fmt=2)
            ids = song_builder.song_ids(self.archive)
        except Exception as exc:
            QMessageBox.critical(self, "Parse failed", str(exc))
            return
        self.cb_template.clear()
        self.cb_template.addItems(ids)
        self.cb_template.setEnabled(True)
        self.b_run.setEnabled(True)
        self.log.appendPlainText(
            f"extracted: list.bin {self._list.stat().st_size:,} B, "
            f"DATA.000 {self._data.stat().st_size:,} B\n"
            f"{len(ids)} songs in {self._part}; pick a template + fill the new song.")

    # -- build + write back ---------------------------------------------------
    def _run(self):
        if not self.archive:
            return
        new_id = self.ed_newid.text().strip()
        tmpl = self.cb_template.currentText().strip()
        if not new_id or not tmpl:
            QMessageBox.warning(self, "Wizard", "Enter a new song id and pick a template.")
            return
        tja = None
        if self.ed_tja.text() and Path(self.ed_tja.text()).exists():
            tja = self._read_tja_text(Path(self.ed_tja.text()))
        if QMessageBox.warning(
                self, "Write back to HDD",
                f"This will add song '{new_id}' and write the grown DATA.000 + "
                f"list.bin back into:\n{self._img}\n(partition {self._part})\n\n"
                f"The 80 GB image is modified in place — NO automatic backup. "
                f"Make sure you have a backup of the .img. Continue?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        # DB prep on the GUI thread (SongManager is a QWidget)
        try:
            db_pre, stars = song_builder.prepare_new_song_db(self.archive, new_id, tmpl, tja)
        except Exception as exc:
            QMessageBox.critical(self, "Add song", str(exc))
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
            h = ps2hdd.Ps2Hdd(img, writable=True)
            try:
                # Write DATA.000 first, then list.bin. If the big DATA write
                # fails, the partition still has the OLD list.bin pointing at the
                # OLD data (self-consistent / bootable). Writing list.bin first
                # would leave a new index pointing at stale data on partial fail.
                h.pfs_write(part, "/DATA.000", data)
                h.pfs_write(part, "/list.bin", lb)
            finally:
                h.close()
            logger.info("HDD write-back complete in %.1fs", time.perf_counter() - t0)
            return summ, len(lb), len(data)

        self._busy("Adding song + writing back to the HDD image (large, please wait)…",
                   task, self._after_write)

    def _after_write(self, res):
        if self._err(res):
            # try to reopen archive for another attempt
            try:
                self.archive = gui.Archive(self._list, self._data, fmt=2)
            except Exception:
                pass
            return
        summ, nl, nd = res
        self.log.appendPlainText(
            f"\nDONE — wrote into {self._img}\n"
            f"  list.bin {nl:,} B, DATA.000 {nd:,} B\n"
            f"  groups added: {summ['groups']}, select files: {summ['extra_files']}, "
            f"audio: {'yes' if summ['audio'] else 'no'}, db: {summ['db']}")
        if summ["errors"]:
            self.log.appendPlainText("ERRORS:\n  " + "\n  ".join(summ["errors"]))
        QMessageBox.information(self, "Done",
                                "Song added and written back into the HDD image.")
        # re-extract so the wizard reflects the new state for further adds
        self._load_partition()

    # -- worker plumbing ------------------------------------------------------
    def _busy(self, msg, fn, on_done):
        self.b_run.setEnabled(False)
        self._prog = QProgressDialog(msg, None, 0, 0, self)
        self._prog.setWindowModality(Qt.WindowModal); self._prog.setCancelButton(None)
        self._prog.setMinimumDuration(0); self._prog.show()
        self._worker = _Worker(fn)

        def done(r):
            self._prog.close()
            self.b_run.setEnabled(self.archive is not None)
            on_done(r)
        self._worker.done.connect(done)
        self._worker.start()

    def _err(self, res):
        if isinstance(res, tuple) and res and res[0] == "ERROR":
            self.log.appendPlainText("FAILED:\n" + res[2])
            QMessageBox.critical(self, "Failed", str(res[1]))
            return True
        return False

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

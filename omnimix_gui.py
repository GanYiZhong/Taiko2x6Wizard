#!/usr/bin/env python3
"""
Omnimix Maker — GUI front-end for omnimix_maker.

Pick a TARGET (a Taiko 14+ HDD .img, or a loose DATA.000+list.bin folder) and a
list of SOURCES (Taiko 8…14 images/folders). It harvests every song the target
lacks — charts, name textures, audio, stars — appends them in one rebuild, and
(for an .img target) lifts the executable's song ceiling so they are reachable.

Pipeline, respecting the "SongManager is a QWidget → GUI thread only" rule:
  1. [worker]  extract each .img source + the .img target to temp DATA.000/list pairs
  2. [GUI]     plan the merge + precompute the merged DB bins (fast, small bins)
  3. [worker]  harvest every added song's groups + one build_archive
  4. [worker]  write the merged archive back / export it, then patch the song limit
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLineEdit,
    QPushButton, QFileDialog, QCheckBox, QPlainTextEdit, QLabel, QMessageBox,
    QWidget, QListWidget, QListWidgetItem, QInputDialog, QGroupBox,
)

import appconfig
import omnimix_maker as OM

logger = logging.getLogger("taiko.omnimix.gui")


class _Worker(QThread):
    log_sig = Signal(str)
    done_sig = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done_sig.emit(self._fn(lambda s: self.log_sig.emit(s)))
        except Exception as exc:
            import traceback
            self.done_sig.emit(("ERROR", exc, traceback.format_exc()))


def _pick_partition(parent, img):
    try:
        parts = OM.taiko_partitions(img)
    except Exception as exc:
        QMessageBox.critical(parent, "Open .img failed", str(exc))
        return None
    if not parts:
        QMessageBox.warning(parent, "No partitions", "No PFS partitions found.")
        return None
    # T14 song data lives in t14jp*.0001; default to it when present.
    default = next((p for p in parts if p.endswith(".0001")), parts[0])
    part, ok = QInputDialog.getItem(parent, "Choose partition",
                                    f"{Path(img).name}:", parts,
                                    parts.index(default), False)
    return part if ok else None


class OmnimixDialog(QDialog):
    """Assemble an omnimix into a target image."""

    def __init__(self, parent=None, default_img=""):
        super().__init__(parent)
        self.setWindowTitle("Omnimix Maker — fuse songs from many images into one")
        self.resize(760, 680)
        self._sources = []           # list of location dicts
        self._tmproot = None
        self._plan = None
        self._db = None
        self._target_pair = None     # (list_path, data_path) after extraction
        self._build_ui()
        if default_img:
            self.ed_timg.setText(default_img)

    # -- ui -------------------------------------------------------------------
    def _build_ui(self):
        lay = QVBoxLayout(self)
        note = QLabel(
            "Fuses the songs of several Taiko images into one. Only songs the "
            "TARGET does not already have are added (dedup by id; the target is "
            "the base). Charts, textures, audio and stars are copied as-is. "
            "⚠ Close PCSX2 first, and work on a COPY — the .img is written in place.")
        note.setWordWrap(True); note.setStyleSheet("color:#666;")
        lay.addWidget(note)

        # --- target ---
        gt = QGroupBox("Target (merge INTO this — the base)")
        tf = QFormLayout(gt)
        self.cb_tkind = QComboBox(); self.cb_tkind.addItems(
            ["HDD .img (Taiko 14+)", "Folder (DATA.000 + list.bin)"])
        self.cb_tkind.currentIndexChanged.connect(self._sync_target)
        tf.addRow("target type:", self.cb_tkind)
        self.ed_timg = QLineEdit(); bt = QPushButton("…"); bt.clicked.connect(self._pick_target)
        tf.addRow("target path:", self._row(self.ed_timg, bt))
        self.ed_tpart = QLineEdit(); self.ed_tpart.setPlaceholderText("(auto — chosen on Preview)")
        self.ed_tpart.setReadOnly(True)
        self.lbl_tpart = QLabel("partition:")
        tf.addRow(self.lbl_tpart, self.ed_tpart)
        lay.addWidget(gt)

        # --- sources ---
        gs = QGroupBox("Sources (harvest FROM these — first one wins on ties)")
        sv = QVBoxLayout(gs)
        self.lst = QListWidget(); self.lst.setMinimumHeight(120)
        sv.addWidget(self.lst)
        sb = QHBoxLayout()
        for txt, fn in (("Add .img…", self._add_img), ("Add ISO…", self._add_iso),
                        ("Add folder…", self._add_folder),
                        ("Up", lambda: self._move(-1)), ("Down", lambda: self._move(1)),
                        ("Remove", self._remove)):
            b = QPushButton(txt); b.clicked.connect(fn); sb.addWidget(b)
        sb.addStretch(1)
        sv.addLayout(sb)
        lay.addWidget(gs)

        # --- options ---
        go = QGroupBox("Options")
        of = QFormLayout(go)
        self.cb_over = QComboBox(); self.cb_over.addItems([
            "Fit everything, patch the ceiling (experimental > 214)",
            "Cap at the proven-safe 214 songs"])
        of.addRow("when songs exceed 214:", self.cb_over)
        self.ck_backup = QCheckBox("back up the target .img before writing"); self.ck_backup.setChecked(True)
        of.addRow("", self.ck_backup)
        self.ck_limit = QCheckBox("patch the song-count ceiling in the exe (.img target)")
        self.ck_limit.setChecked(True)
        of.addRow("", self.ck_limit)
        lay.addWidget(go)

        self.log = QPlainTextEdit(readOnly=True)
        lay.addWidget(QLabel("log:")); lay.addWidget(self.log, 1)

        btns = QHBoxLayout(); btns.addStretch(1)
        self.b_prev = QPushButton("Preview plan"); self.b_prev.clicked.connect(self._preview)
        self.b_build = QPushButton("Build Omnimix"); self.b_build.clicked.connect(self._build)
        self.b_build.setEnabled(False)
        b_close = QPushButton("Close"); b_close.clicked.connect(self.reject)
        btns.addWidget(self.b_prev); btns.addWidget(self.b_build); btns.addWidget(b_close)
        lay.addLayout(btns)
        self._sync_target()

    def _row(self, w, b):
        c = QWidget(); h = QHBoxLayout(c); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(w, 1); h.addWidget(b); return c

    # -- target helpers -------------------------------------------------------
    def _target_is_img(self):
        return self.cb_tkind.currentIndex() == 0

    def _sync_target(self):
        img = self._target_is_img()
        self.ed_tpart.setVisible(img); self.lbl_tpart.setVisible(img)
        self.ck_limit.setEnabled(img)
        self.ck_backup.setEnabled(img)

    def _pick_target(self):
        if self._target_is_img():
            p = appconfig.pick_open(self, "hddimg", "Target PS2 HDD image",
                                    "HDD image (*.img *.raw *.bin);;All (*)")
            if p:
                self.ed_timg.setText(p); self.ed_tpart.clear()
        else:
            d = QFileDialog.getExistingDirectory(self, "Target folder (DATA.000 + list.bin)")
            if d:
                self.ed_timg.setText(d)

    # -- sources --------------------------------------------------------------
    def _add_img(self):
        p = appconfig.pick_open(self, "hddimg", "Source PS2 HDD image",
                                "HDD image (*.img *.raw *.bin);;All (*)")
        if not p:
            return
        part = _pick_partition(self, p)
        if not part:
            return
        label, ok = QInputDialog.getText(self, "Label",
                                         "Name this source (e.g. Taiko 12):",
                                         text=Path(p).stem)
        if not ok:
            return
        self._sources.append({"kind": "img", "img": p, "partition": part,
                              "label": label or Path(p).stem})
        self._refresh_sources()

    def _add_iso(self):
        p = appconfig.pick_open(self, "gameiso", "Source game ISO (DATA.000 + LIST.BIN)",
                                "PS2 game ISO (*.iso *.bin);;All (*)")
        if not p:
            return
        # validate it holds an arcade pair before accepting it
        try:
            with open(p, "rb") as f:
                recs = OM._iso_root_records(f)
            if "DATA.000" not in recs or "LIST.BIN" not in recs:
                QMessageBox.warning(self, "Not a Taiko game ISO",
                                    "That ISO has no DATA.000 + LIST.BIN in its root.")
                return
        except Exception as exc:
            QMessageBox.critical(self, "Open ISO failed", str(exc))
            return
        label, ok = QInputDialog.getText(self, "Label",
                                         "Name this source (e.g. Taiko 9):",
                                         text=Path(p).stem)
        if not ok:
            return
        self._sources.append({"kind": "iso", "iso": p,
                              "label": label or Path(p).stem})
        self._refresh_sources()

    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Source folder (DATA.000 + list.bin)")
        if not d:
            return
        dp = Path(d) / "DATA.000"; lp = Path(d) / "list.bin"
        if not dp.exists() or not lp.exists():
            QMessageBox.warning(self, "Not a game folder",
                                "That folder has no DATA.000 + list.bin.")
            return
        label, ok = QInputDialog.getText(self, "Label",
                                         "Name this source (e.g. Taiko 8):",
                                         text=Path(d).name)
        if not ok:
            return
        self._sources.append({"kind": "pair", "list": str(lp), "data": str(dp),
                              "label": label or Path(d).name})
        self._refresh_sources()

    def _move(self, delta):
        r = self.lst.currentRow()
        if r < 0:
            return
        j = r + delta
        if 0 <= j < len(self._sources):
            self._sources[r], self._sources[j] = self._sources[j], self._sources[r]
            self._refresh_sources(); self.lst.setCurrentRow(j)

    def _remove(self):
        r = self.lst.currentRow()
        if r >= 0:
            del self._sources[r]; self._refresh_sources()

    def _refresh_sources(self):
        self.lst.clear()
        for i, s in enumerate(self._sources):
            if s["kind"] == "img":
                where = f"{Path(s['img']).name}:{s['partition']}"
            elif s["kind"] == "iso":
                where = Path(s["iso"]).name
            else:
                where = Path(s["data"]).parent.name
            self.lst.addItem(QListWidgetItem(f"{i+1}. {s['label']}  —  {where}"))
        self.b_build.setEnabled(False)     # plan is stale after edits

    # -- pipeline: extraction -------------------------------------------------
    def _validate(self):
        if not self.ed_timg.text().strip():
            QMessageBox.warning(self, "Omnimix", "Choose a target."); return False
        if not self._sources:
            QMessageBox.warning(self, "Omnimix", "Add at least one source."); return False
        return True

    def _extract_all(self, log):
        """Worker: extract every .img (sources + target) to temp pairs.
        Returns (resolved_source_specs, target_list, target_data, target_part)."""
        self._tmproot = Path(tempfile.mkdtemp(prefix="omnimix_"))
        specs = []
        for i, s in enumerate(self._sources):
            if s["kind"] == "img":
                lp, dp = OM.extract_partition(s["img"], s["partition"],
                                              self._tmproot / f"src{i}", log)
            elif s["kind"] == "iso":
                lp, dp = OM.iso_extract_pair(s["iso"], self._tmproot / f"src{i}", log)
            else:
                lp, dp = s["list"], s["data"]
            specs.append(OM.SourceSpec(s["label"], str(lp), str(dp)))
        tpart = None
        if self._target_is_img():
            tpart = self.ed_tpart.text().strip()
            tl, td = OM.extract_partition(self.ed_timg.text().strip(), tpart,
                                          self._tmproot / "target", log)
        else:
            base = Path(self.ed_timg.text().strip())
            tl, td = base / "list.bin", base / "DATA.000"
        return specs, str(tl), str(td), tpart

    def _preview(self):
        if not self._validate():
            return
        if self._target_is_img() and not self.ed_tpart.text().strip():
            part = _pick_partition(self, self.ed_timg.text().strip())
            if not part:
                return
            self.ed_tpart.setText(part)
        self.log.clear(); self.log.appendPlainText("Extracting images (large, ~1 min each)…")
        self._begin()
        self._worker = _Worker(self._extract_all)
        self._worker.log_sig.connect(self.log.appendPlainText)
        self._worker.done_sig.connect(self._after_extract)
        self._worker.start()

    def _after_extract(self, res):
        if self._is_err(res):
            return
        specs, tl, td, tpart = res
        self._specs = specs
        self._target_pair = (tl, td)
        self._tpart = tpart
        # GUI-thread: plan + precompute DB (SongManager is a QWidget)
        try:
            target = OM.open_pair(tl, td)
            try:
                self.log.appendPlainText("planning merge…")
                self._plan = OM.plan_merge(target, specs,
                                           log=self.log.appendPlainText)
                self._db = OM.precompute_db(target, self._plan)
            finally:
                target.close()
        except Exception as exc:
            import traceback
            self.log.appendPlainText("PLAN FAILED:\n" + traceback.format_exc())
            QMessageBox.critical(self, "Plan failed", str(exc))
            self._end(); return
        p = self._plan
        dupes = sum(len(v) for v in p.skipped_dupes.values())
        msg = (f"Target base: {len(p.target_ids)} songs\n"
               f"To add:      {len(p.items)} songs\n"
               f"TOTAL after: {p.total_after} songs"
               + (f"\nCross-source duplicate ids skipped: {dupes}" if dupes else ""))
        if p.total_after > OM.SAFE_LIMIT:
            msg += (f"\n\n⚠ {p.total_after} exceeds the proven-safe 214. "
                    "Above 214 is experimental — the wheel may not render/play "
                    "them all until the Stage-2 UI tables are patched. Test in-game.")
        self.log.appendPlainText("\n" + msg)
        self.b_build.setEnabled(len(p.items) > 0)
        self._end()
        QMessageBox.information(self, "Plan ready", msg
                                + "\n\nClick “Build Omnimix” to assemble.")

    # -- pipeline: build ------------------------------------------------------
    def _build(self):
        if not self._plan or not self._db:
            QMessageBox.warning(self, "Omnimix", "Run Preview plan first."); return
        cap = self.cb_over.currentIndex() == 1
        if self._plan.total_after > OM.SAFE_LIMIT and not cap:
            if QMessageBox.question(
                    self, "Experimental",
                    f"This builds {self._plan.total_after} songs — past the "
                    "proven-safe 214. The data will be correct, but whether all "
                    "of them show/play in-game is untested. Continue?") != \
                    QMessageBox.Yes:
                return
        tl, td = self._target_pair
        specs, db, plan = self._specs, self._db, self._plan
        is_img = self._target_is_img()
        img = self.ed_timg.text().strip(); tpart = self._tpart
        do_backup = self.ck_backup.isChecked() and is_img
        do_limit = self.ck_limit.isChecked() and is_img

        def task(log):
            target = OM.open_pair(tl, td)
            try:
                t0 = time.perf_counter()
                res = OM.assemble(target, specs, plan, db, log=log)
            finally:
                target.close()
            log("assembled %d songs (total %d) in %.1fs"
                % (res["added"], res["total"], time.perf_counter() - t0))
            if res["errors"]:
                log("errors: " + "; ".join(res["errors"]))
            if is_img:
                if do_backup:
                    bak = img + ".omnimix.bak"
                    log(f"backing up target → {Path(bak).name} (copying, large)…")
                    shutil.copy2(img, bak)
                OM.write_partition_growing(img, tpart, res["list"], res["data"], log)
                if do_limit:
                    cap_safe = (self.cb_over.currentIndex() == 1)
                    ceil = OM.patch_song_limit(img, res["total"],
                                               cap_safe=cap_safe, log=log)
                    res["ceiling"] = ceil
            else:
                Path(td).write_bytes(res["data"]); Path(tl).write_bytes(res["list"])
                log(f"wrote merged pair → {Path(td).parent}")
            return res

        if not self._confirm_write(is_img, img, tpart):
            return
        self.log.appendPlainText("\nBuilding — this rewrites the whole archive "
                                 "and (for .img) writes it back. Please wait…")
        self._begin()
        self._worker = _Worker(task)
        self._worker.log_sig.connect(self.log.appendPlainText)
        self._worker.done_sig.connect(self._after_build)
        self._worker.start()

    def _confirm_write(self, is_img, img, tpart):
        where = (f"partition {tpart} of\n{img}" if is_img
                 else f"{self.ed_timg.text().strip()}")
        return QMessageBox.question(
            self, "Write?",
            f"Add {len(self._plan.items)} songs → {self._plan.total_after} total, "
            f"then write into:\n{where}\n\nProceed?") == QMessageBox.Yes

    def _after_build(self, res):
        if self._is_err(res):
            return
        self.changed = True
        ceil = res.get("ceiling")
        msg = (f"Done — added {res['added']} songs, total {res['total']}."
               + (f"\nExe song ceiling patched to {ceil}." if ceil else "")
               + (f"\n\n{len(res['warnings'])} warnings (see log)." if res["warnings"] else ""))
        for w in res["warnings"]:
            self.log.appendPlainText("WARNING: " + w)
        self.log.appendPlainText("\n" + msg)
        self._cleanup_tmp()
        self._end()
        QMessageBox.information(self, "Omnimix built", msg)

    # -- worker plumbing ------------------------------------------------------
    def _begin(self):
        for b in (self.b_prev, self.b_build):
            b.setEnabled(False)

    def _end(self):
        self.b_prev.setEnabled(True)

    def _is_err(self, res):
        if isinstance(res, tuple) and res and res[0] == "ERROR":
            self.log.appendPlainText("FAILED:\n" + res[2])
            QMessageBox.critical(self, "Omnimix failed", str(res[1]))
            self._cleanup_tmp(); self._end()
            return True
        return False

    def _cleanup_tmp(self):
        if self._tmproot and self._tmproot.exists():
            shutil.rmtree(self._tmproot, ignore_errors=True)
        self._tmproot = None

    def reject(self):
        self._cleanup_tmp()
        super().reject()

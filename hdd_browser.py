#!/usr/bin/env python3
"""
PS2 HDD browser dialog — open a Taiko arcade HDD .img (APA + PFS), browse the
PFS partitions, extract files (e.g. DATA.000 / list.bin) to disk and inject
modified files back in place.

Backed by ps2hdd.py. Reads are non-destructive; replacing a file opens the image
writable and patches it in place (same-or-smaller size only — the toolkit's
conservative DATA.000 save keeps the size identical, so it fits).
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QSettings
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QComboBox, QListWidget,
    QLabel, QFileDialog, QMessageBox, QProgressDialog, QWidget, QListWidgetItem,
)

import appconfig
import ps2hdd


class _Worker(QThread):
    done = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
        except Exception as exc:
            import traceback
            self.done.emit(("ERROR", exc, traceback.format_exc()))


class HddBrowserDialog(QDialog):
    def __init__(self, parent=None, default_img=""):
        super().__init__(parent)
        self.setWindowTitle("PS2 HDD Browser (APA + PFS)")
        self.resize(620, 460)
        self.path = ""
        self.hdd = None
        self._build_ui()
        if default_img and Path(default_img).exists():
            self._open(default_img)

    def _build_ui(self):
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.lbl_img = QLabel("(no image open)")
        b_open = QPushButton("Open .img…"); b_open.clicked.connect(lambda: self._open())
        top.addWidget(b_open); top.addWidget(self.lbl_img, 1)
        lay.addLayout(top)

        prow = QHBoxLayout()
        prow.addWidget(QLabel("partition:"))
        self.cb_part = QComboBox()
        self.cb_part.currentIndexChanged.connect(self._refresh_files)
        prow.addWidget(self.cb_part, 1)
        lay.addLayout(prow)

        self.lst = QListWidget()
        lay.addWidget(self.lst, 1)

        btns = QHBoxLayout()
        self.b_extract = QPushButton("Extract selected…"); self.b_extract.clicked.connect(self._extract)
        self.b_replace = QPushButton("Replace selected…"); self.b_replace.clicked.connect(self._replace)
        b_close = QPushButton("Close"); b_close.clicked.connect(self.accept)
        for b in (self.b_extract, self.b_replace):
            b.setEnabled(False)
        btns.addWidget(self.b_extract); btns.addWidget(self.b_replace)
        btns.addStretch(1); btns.addWidget(b_close)
        lay.addLayout(btns)

        self.status = QLabel(""); self.status.setStyleSheet("color:#999;")
        lay.addWidget(self.status)

    # -- open / browse --------------------------------------------------------
    def _open(self, path=""):
        if not path:
            path = appconfig.pick_open(self, "hddimg", "Open PS2 HDD image",
                                       "HDD image (*.img *.raw *.bin);;All files (*)")
        if not path:
            return
        try:
            if self.hdd:
                self.hdd.close()
            self.hdd = ps2hdd.Ps2Hdd(path)        # read-only
            self.path = path
        except Exception as exc:
            QMessageBox.critical(self, "Open failed", str(exc))
            return
        try:
            QSettings("TaikoTools", "SYSTEM256Explorer").setValue("recent_hdd_img", path)
        except Exception:
            pass
        self.lbl_img.setText(path)
        self.cb_part.blockSignals(True)
        self.cb_part.clear()
        self._parts = [p for p in self.hdd.partitions() if p.get("is_pfs")]
        for p in self._parts:
            self.cb_part.addItem(f"{p['name']}  ({p['sectors']*512/1e9:.2f} GB)")
        self.cb_part.blockSignals(False)
        if self._parts:
            # default to the first game partition (not a __system one)
            default = next((i for i, p in enumerate(self._parts)
                            if not p["name"].startswith("__")), 0)
            self.cb_part.setCurrentIndex(default)
            self._refresh_files()

    def _cur_part(self):
        i = self.cb_part.currentIndex()
        return self._parts[i]["name"] if 0 <= i < len(self._parts) else None

    def _refresh_files(self):
        self.lst.clear()
        part = self._cur_part()
        if not part:
            return
        try:
            entries = self.hdd.pfs_listdir(part, "/")
        except Exception as exc:
            self.status.setText(f"list failed: {exc}")
            return
        for e in entries:
            tag = "[dir] " if e.get("is_dir") else ""
            it = QListWidgetItem(f"{tag}{e['name']}    {e['size']:,} B")
            it.setData(Qt.UserRole, e)
            self.lst.addItem(it)
        self.b_extract.setEnabled(True)
        self.b_replace.setEnabled(True)
        self.status.setText(f"{len(entries)} entries in {part}")

    def _selected(self):
        it = self.lst.currentItem()
        return it.data(Qt.UserRole) if it else None

    # -- extract --------------------------------------------------------------
    def _extract(self):
        e = self._selected()
        part = self._cur_part()
        if not e or e.get("is_dir"):
            QMessageBox.information(self, "Extract", "Select a file.")
            return
        dest = appconfig.pick_save(self, "hdd_extract", "Extract to", e["name"])
        if not dest:
            return
        self._run(lambda: self._do_extract(part, "/" + e["name"], dest),
                  f"Extracting {e['name']} ({e['size']/1e6:.0f} MB)…",
                  lambda r: self.status.setText(f"extracted → {dest}"))

    def _do_extract(self, part, fpath, dest):
        data = self.hdd.pfs_read(part, fpath)
        Path(dest).write_bytes(data)
        return len(data)

    # -- replace --------------------------------------------------------------
    def _replace(self):
        e = self._selected()
        part = self._cur_part()
        if not e or e.get("is_dir"):
            QMessageBox.information(self, "Replace", "Select a file.")
            return
        src = appconfig.pick_open(self, "hdd_replace", f"Replace {e['name']} with…")
        if not src:
            return
        new_size = Path(src).stat().st_size
        if QMessageBox.warning(
                self, "Replace in HDD image",
                f"This writes directly into:\n{self.path}\n\n"
                f"{e['name']}: {e['size']:,} → {new_size:,} bytes\n\n"
                f"The 80 GB image is modified IN PLACE, NON-ATOMICALLY, with no "
                f"automatic backup: if the write is interrupted (crash/power loss) "
                f"the image can be left corrupt. Make a backup of the .img first.\n\n"
                f"Larger files are supported (free PFS zones are allocated); make "
                f"sure the partition has enough free space. Continue?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self._run(lambda: self._do_replace(part, "/" + e["name"], src),
                  f"Writing {e['name']}…", self._after_replace)

    def _do_replace(self, part, fpath, src):
        # Runs on the worker thread. Operate on a LOCAL writable handle only —
        # never touch self.hdd here, so the GUI thread (closeEvent) is the sole
        # owner of self.hdd and there is no cross-thread handle race.
        data = Path(src).read_bytes()
        self.hdd.close()                          # GUI thread set this before start
        self.hdd = None                           # mark closed; GUI won't double-close
        wh = ps2hdd.Ps2Hdd(self.path, writable=True)
        try:
            wh.pfs_write(part, fpath, data)
        finally:
            wh.close()
        return len(data)

    def _after_replace(self, r):
        # Back on the GUI thread: reopen the read-only browse handle here so all
        # self.hdd assignments happen on one thread.
        try:
            self.hdd = ps2hdd.Ps2Hdd(self.path)
        except Exception as exc:
            QMessageBox.critical(self, "Reopen failed", str(exc))
            self.status.setText(f"write ok, reopen failed: {exc}")
            return
        self.status.setText("replaced in image ✓")
        self._refresh_files()

    # -- worker plumbing ------------------------------------------------------
    def _run(self, fn, msg, on_ok):
        self.b_extract.setEnabled(False); self.b_replace.setEnabled(False)
        prog = QProgressDialog(msg, None, 0, 0, self)
        prog.setWindowModality(Qt.WindowModal); prog.setCancelButton(None)
        prog.setMinimumDuration(0); prog.show()
        self._worker = _Worker(fn)

        def done(r):
            prog.close()
            self.b_extract.setEnabled(True); self.b_replace.setEnabled(True)
            if isinstance(r, tuple) and r and r[0] == "ERROR":
                if isinstance(r[1], NotImplementedError):
                    QMessageBox.warning(self, "Cannot grow file", str(r[1]))
                else:
                    QMessageBox.critical(self, "Failed", str(r[1]))
                # ensure we are back in a read-only browsable state. The replace
                # worker closes self.hdd (sets it None) before opening its own
                # writable handle; only reopen if we no longer hold one, and
                # close any stale handle first to avoid leaking it.
                if self.hdd is None:
                    try:
                        self.hdd = ps2hdd.Ps2Hdd(self.path)
                    except Exception:
                        self.hdd = None
                return
            on_ok(r)

        self._worker.done.connect(done)
        self._worker.start()

    def closeEvent(self, ev):
        # Wait for any in-flight write/extract worker before touching handles,
        # so the GUI thread and worker never race on the same HDD image.
        w = getattr(self, "_worker", None)
        if w is not None and w.isRunning():
            w.wait()
        if self.hdd:
            self.hdd.close()
            self.hdd = None
        super().closeEvent(ev)

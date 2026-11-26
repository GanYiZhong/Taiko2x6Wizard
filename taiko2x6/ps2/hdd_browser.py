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
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QComboBox, QLineEdit,
    QLabel, QMessageBox, QProgressDialog, QTreeWidget, QTreeWidgetItem,
)

from taiko2x6.app import appconfig
from taiko2x6.app.i18n import tr
from taiko2x6.ps2 import ps2hdd


def human_size(n: int) -> str:
    """1536 -> '1.5 KB'; exact bytes below 1 KB."""
    n = int(n)
    if n < 1024:
        return f"{n} B"
    v = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        v /= 1024.0
        if v < 1024 or unit == "TB":
            return f"{v:.1f} {unit}"


from taiko2x6.app.errors import friendly_error, is_locked_error  # noqa: E402,F401


class _SizeItem(QTreeWidgetItem):
    """Sorts folders first, then by the column's natural key (name / bytes)."""

    def __lt__(self, other):
        col = self.treeWidget().sortColumn() if self.treeWidget() else 0
        a, b = self.data(0, Qt.UserRole), other.data(0, Qt.UserRole)
        if col == 1:
            return (a["size"], a["name"].lower()) < (b["size"], b["name"].lower())
        return (not a["is_dir"], a["name"].lower()) < (not b["is_dir"], b["name"].lower())


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
        self.setWindowTitle(tr("PS2 HDD Browser (APA + PFS)"))
        self.resize(620, 460)
        self.path = ""
        self.hdd = None
        self._parts = []
        self._entry_count = 0
        self._working = False
        self._build_ui()
        default_img = default_img or appconfig.last_existing("hddimg")
        if default_img and Path(default_img).exists():
            self._open(default_img)

    def _build_ui(self):
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.lbl_img = QLabel(tr("(no image open)"))
        b_open = QPushButton(tr("Open .img…")); b_open.clicked.connect(lambda: self._open())
        top.addWidget(b_open); top.addWidget(self.lbl_img, 1)
        lay.addLayout(top)

        prow = QHBoxLayout()
        prow.addWidget(QLabel(tr("partition:")))
        self.cb_part = QComboBox()
        self.cb_part.currentIndexChanged.connect(self._refresh_files)
        prow.addWidget(self.cb_part, 1)
        lay.addLayout(prow)

        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText(tr("Filter files…"))
        self.ed_filter.setClearButtonEnabled(True)
        self.ed_filter.textChanged.connect(self._apply_filter)
        lay.addWidget(self.ed_filter)

        self.lst = QTreeWidget()
        self.lst.setColumnCount(3)
        self.lst.setHeaderLabels([tr("Name"), tr("Size"), tr("Type")])
        self.lst.setRootIsDecorated(False)
        self.lst.setSortingEnabled(True)
        self.lst.sortByColumn(0, Qt.AscendingOrder)
        self.lst.header().setStretchLastSection(False)
        self.lst.setColumnWidth(0, 320)
        self.lst.itemSelectionChanged.connect(self._update_buttons)
        self.lst.itemDoubleClicked.connect(lambda *_: self._extract())
        lay.addWidget(self.lst, 1)

        btns = QHBoxLayout()
        self.b_extract = QPushButton(tr("Extract selected…")); self.b_extract.clicked.connect(self._extract)
        self.b_replace = QPushButton(tr("Replace selected…")); self.b_replace.clicked.connect(self._replace)
        b_close = QPushButton(tr("Close")); b_close.clicked.connect(self.accept)
        for b in (self.b_extract, self.b_replace):
            b.setEnabled(False)
        btns.addWidget(self.b_extract); btns.addWidget(self.b_replace)
        btns.addStretch(1); btns.addWidget(b_close)
        lay.addLayout(btns)

        self.status = QLabel(tr("Open a PS2 HDD image to begin."))
        self.status.setStyleSheet("color:palette(mid);")
        lay.addWidget(self.status)

    # -- open / browse --------------------------------------------------------
    def _open(self, path=""):
        if not path:
            path = appconfig.pick_open(self, "hddimg", tr("Open PS2 HDD image"),
                                       tr("HDD image (*.img *.raw *.bin);;All files (*)"))
        if not path:
            return
        try:
            if self.hdd:
                self.hdd.close()
            self.hdd = ps2hdd.Ps2Hdd(path)        # read-only
            self.path = path
        except Exception as exc:
            QMessageBox.critical(self, tr("Open failed"), friendly_error(exc))
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
            self.cb_part.addItem(f"{p['name']}  ({human_size(p['sectors'] * 512)})")
        self.cb_part.blockSignals(False)
        if self._parts:
            # default to the first game partition (not a __system one)
            saved = appconfig.get("hdd", "partition")
            default = next((i for i, p in enumerate(self._parts)
                            if p["name"] == saved), None)
            if default is None:
                default = next((i for i, p in enumerate(self._parts)
                                if not p["name"].startswith("__")), 0)
            self.cb_part.setCurrentIndex(default)
            self._refresh_files()

    def _cur_part(self):
        i = self.cb_part.currentIndex()
        return self._parts[i]["name"] if 0 <= i < len(self._parts) else None

    def _refresh_files(self):
        self.lst.clear()
        self._update_buttons()
        part = self._cur_part()
        if not part:
            return
        appconfig.set_value("hdd", "partition", part)
        try:
            entries = self.hdd.pfs_listdir(part, "/")
        except Exception as exc:
            self.status.setText(tr("list failed: {err}").format(err=friendly_error(exc)))
            return
        self.lst.setSortingEnabled(False)
        for e in entries:
            it = _SizeItem([e["name"], "" if e.get("is_dir") else human_size(e["size"]),
                            tr("folder") if e.get("is_dir") else tr("file")])
            it.setData(0, Qt.UserRole, e)
            it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            it.setToolTip(1, f"{e['size']:,} B")
            self.lst.addTopLevelItem(it)
        self.lst.setSortingEnabled(True)
        self._entry_count = len(entries)
        self._apply_filter()

    def _apply_filter(self, *_):
        q = self.ed_filter.text().strip().lower()
        shown = 0
        for i in range(self.lst.topLevelItemCount()):
            it = self.lst.topLevelItem(i)
            hit = q in it.text(0).lower()
            it.setHidden(not hit)
            shown += hit
        part = self._cur_part()
        if part:
            total = self._entry_count
            if q:
                self.status.setText(tr("{shown} of {n} entries in {part}")
                                    .format(shown=shown, n=total, part=part))
            else:
                self.status.setText(tr("{n} entries in {part}").format(n=total, part=part))
        self._update_buttons()

    def _selected(self):
        sel = [i for i in self.lst.selectedItems() if not i.isHidden()]
        return sel[0].data(0, Qt.UserRole) if sel else None

    def _update_buttons(self):
        """Extract/Replace need a selected file and no running job."""
        e = self._selected()
        ok = bool(e) and not e.get("is_dir") and not self._working
        self.b_extract.setEnabled(ok)
        self.b_replace.setEnabled(ok)

    # -- extract --------------------------------------------------------------
    def _extract(self):
        e = self._selected()
        part = self._cur_part()
        if not e or e.get("is_dir"):
            QMessageBox.information(self, tr("Extract"), tr("Select a file."))
            return
        dest = appconfig.pick_save(self, "hdd_extract", tr("Extract to"), e["name"])
        if not dest:
            return
        self._run(lambda: self._do_extract(part, "/" + e["name"], dest),
                  tr("Extracting {name} ({size})…").format(
                      name=e["name"], size=human_size(e["size"])),
                  lambda r: self.status.setText(
                      tr("extracted → {dest} ({size})").format(dest=dest, size=human_size(r))))

    def _do_extract(self, part, fpath, dest):
        data = self.hdd.pfs_read(part, fpath)
        Path(dest).write_bytes(data)
        return len(data)

    # -- replace --------------------------------------------------------------
    def _replace(self):
        e = self._selected()
        part = self._cur_part()
        if not e or e.get("is_dir"):
            QMessageBox.information(self, tr("Replace"), tr("Select a file."))
            return
        src = appconfig.pick_open(self, "hdd_replace", tr("Replace {name} with…").format(name=e["name"]))
        if not src:
            return
        new_size = Path(src).stat().st_size
        if QMessageBox.warning(
                self, tr("Replace in HDD image"),
                tr("This writes directly into:\n{img}\n\n{name}: {old} → {new}\n\n"
                   "The image is modified IN PLACE. {note}\n\n"
                   "Larger files are supported (free PFS zones are allocated); make "
                   "sure the partition has enough free space. Close PCSX2 first. "
                   "Continue?").format(
                    img=self.path, name=e["name"],
                    old=f"{e['size']:,} B ({human_size(e['size'])})",
                    new=f"{new_size:,} B ({human_size(new_size)})",
                    note=ps2hdd.UNDO_NOTE),
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self._run(lambda: self._do_replace(part, "/" + e["name"], src),
                  tr("Writing {name}…").format(name=e["name"]), self._after_replace)

    def _do_replace(self, part, fpath, src):
        # Runs on the worker thread. Operate on a LOCAL writable handle only —
        # never touch self.hdd here, so the GUI thread (closeEvent) is the sole
        # owner of self.hdd and there is no cross-thread handle race.
        data = Path(src).read_bytes()
        self.hdd.close()                          # GUI thread set this before start
        self.hdd = None                           # mark closed; GUI won't double-close
        with ps2hdd.write_session(self.path) as wh:   # rolls back on error
            wh.pfs_write(part, fpath, data)
        self._last_undo = wh.undo_path
        return len(data)

    def _after_replace(self, r):
        # Back on the GUI thread: reopen the read-only browse handle here so all
        # self.hdd assignments happen on one thread.
        try:
            self.hdd = ps2hdd.Ps2Hdd(self.path)
        except Exception as exc:
            QMessageBox.critical(self, tr("Reopen failed"), friendly_error(exc))
            self.status.setText(tr("write ok, reopen failed: {err}").format(err=friendly_error(exc)))
            return
        undo = getattr(self, "_last_undo", None)
        self.status.setText(tr("replaced in image ✓")
                            + (tr(" — undo journal: {name}").format(name=Path(undo).name)
                               if undo else ""))
        self._refresh_files()

    # -- worker plumbing ------------------------------------------------------
    def _run(self, fn, msg, on_ok):
        self._working = True
        self._update_buttons()
        prog = QProgressDialog(msg, None, 0, 0, self)
        prog.setWindowModality(Qt.WindowModal); prog.setCancelButton(None)
        prog.setMinimumDuration(0); prog.show()
        self._worker = _Worker(fn)

        def done(r):
            prog.close()
            self._working = False
            self._update_buttons()
            if isinstance(r, tuple) and r and r[0] == "ERROR":
                if isinstance(r[1], NotImplementedError):
                    QMessageBox.warning(self, tr("Cannot grow file"), str(r[1]))
                else:
                    QMessageBox.critical(self, tr("Failed"), friendly_error(r[1]))
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

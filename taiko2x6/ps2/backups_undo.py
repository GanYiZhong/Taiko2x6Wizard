"""Backups & undo: roll a PS2 HDD image back from its undo journals, or restore
an archive (DATA.000 + list.bin) from its rolling .bak files.

Every in-place write to an .img leaves ``<img>.<date-time>.undo`` (see
``taiko2x6.safeio``); every archive save keeps ``DATA.000.bak``,
``DATA.000.bak.1``... next to the files. This module is the friendly front end
to both. The journal format is NOT parsed here: listing uses
``safeio.journal_ranges`` and rolling back uses ``safeio.rollback``.

Why a rollback also undoes every NEWER journal, newest first: each journal
holds the bytes as they were just before ITS session. Restoring session 2's
journal on an image that later went through session 3 would leave session 3's
changes (to other ranges) in place and put a pre-session-2 view on top of them;
walking back 3, then 2, brings the image through every state in reverse and
ends exactly at the chosen moment. A journal that has been applied is renamed
``.undone`` so it cannot be applied twice (a second pass would write old bytes
over a newer state).
"""
from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from taiko2x6 import safeio

_NAME = re.compile(r"^(?P<base>.+)\.(?P<stamp>\d{8}-\d{6})(?:-(?P<n>\d+))?"
                   r"\.(?P<ext>undone|undo)$")
_BAK = re.compile(r"^(?P<base>DATA\.000|list\.bin)\.bak(?:\.(?P<n>\d+))?$",
                  re.IGNORECASE)


# --------------------------------------------------------------------------- #
#  undo journals (no Qt)
# --------------------------------------------------------------------------- #
@dataclass
class JournalEntry:
    path: Path
    target: Path
    stamp: str
    n: int
    size: int
    used: bool                 # already rolled back (renamed .undone)
    covers: dict | None = field(default=None, compare=False)
    error: str = ""

    @property
    def key(self):
        return (self.stamp, self.n)

    @property
    def when(self) -> str:
        try:
            t = time.strptime(self.stamp, "%Y%m%d-%H%M%S")
            return time.strftime("%Y-%m-%d %H:%M:%S", t)
        except ValueError:
            return self.stamp


def find_journals(path, summarize: bool = False) -> list[JournalEntry]:
    """Undo journals for an image (file) or every image in a folder, newest
    first. ``summarize`` also reads what each one covers (read-only)."""
    p = Path(path)
    folder, only = (p, None) if p.is_dir() else (p.parent, p.name)
    out = []
    try:
        names = list(folder.iterdir())
    except OSError:
        return []
    for f in names:
        m = _NAME.match(f.name)
        if not m or not f.is_file():
            continue
        if only is not None and m["base"] != only:
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        e = JournalEntry(f, f.with_name(m["base"]), m["stamp"],
                         int(m["n"] or 0), size, m["ext"] == "undone")
        if summarize:
            try:
                e.covers = safeio.journal_ranges(f)
            except Exception as exc:                       # unreadable journal
                e.error = str(exc)
        out.append(e)
    out.sort(key=lambda e: (e.key, e.path.name), reverse=True)
    return out


def chain_for(entries, chosen: JournalEntry) -> list[JournalEntry]:
    """The journals to apply for 'roll back to before `chosen`': chosen and
    every newer one of the same image, not yet used, newest first."""
    ch = [e for e in entries
          if e.target == chosen.target and not e.used and e.key >= chosen.key]
    ch.sort(key=lambda e: e.key, reverse=True)
    return ch


def older_for_delete(entries, chosen: JournalEntry) -> list[JournalEntry]:
    """`chosen` and every older journal of the same image (any state)."""
    return [e for e in entries if e.target == chosen.target and e.key <= chosen.key]


def check_writable(target) -> None:
    """Raise OSError when the image cannot be opened for writing (PCSX2 holds
    it open while it runs)."""
    with open(target, "r+b"):
        pass


def rollback_chain(entries, chosen: JournalEntry, log=None) -> list[Path]:
    """Roll `chosen` and all newer journals back, newest first. Each applied
    journal is renamed ``.undone``. Returns the journals applied."""
    chain = chain_for(entries, chosen)
    if not chain or chosen.used:
        raise ValueError("this journal was already rolled back")
    target = chain[0].target
    if not target.exists():
        raise FileNotFoundError(str(target))
    check_writable(target)
    done = []
    for e in chain:
        if log:
            log(f"{e.when}: {e.path.name}")
        safeio.rollback(e.path, e.target, log=log)
        try:
            os.replace(e.path, e.path.with_name(e.path.name + "ne"))
        except OSError:
            if log:
                log(f"(could not rename {e.path.name}; do not apply it again)")
        done.append(e.path)
    return done


def delete_journals(entries) -> int:
    """Delete journal files only (names must look like journals). Returns the
    number removed; the image is never touched."""
    n = 0
    for e in entries:
        if not _NAME.match(e.path.name):
            continue
        try:
            e.path.unlink()
            n += 1
        except OSError:
            pass
    return n


# --------------------------------------------------------------------------- #
#  archive .bak pairs (no Qt)
# --------------------------------------------------------------------------- #
@dataclass
class BakPair:
    n: int                      # 0 = ".bak", k = ".bak.k"
    data_bak: Path
    list_bak: Path
    data: Path                  # the live files this pair restores
    lst: Path
    mtime: float
    size: int

    @property
    def label(self) -> str:
        return ".bak" if self.n == 0 else f".bak.{self.n}"

    @property
    def when(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.mtime))


def find_bak_pairs(folder) -> list[BakPair]:
    """DATA.000 / list.bin backup pairs in `folder`, newest first. Only a
    matching pair (same .bak number for both files) is offered."""
    folder = Path(folder)
    if folder.is_file():
        folder = folder.parent
    data, lst = {}, {}
    try:
        names = list(folder.iterdir())
    except OSError:
        return []
    for f in names:
        m = _BAK.match(f.name)
        if m and f.is_file():
            (data if m["base"].lower() == "data.000" else lst)[int(m["n"] or 0)] = f
    out = []
    for n in set(data) & set(lst):
        d, ls = data[n], lst[n]
        d_live = d.with_name(_BAK.match(d.name)["base"])
        l_live = ls.with_name(_BAK.match(ls.name)["base"])
        out.append(BakPair(n, d, ls, d_live, l_live, d.stat().st_mtime,
                           d.stat().st_size + ls.stat().st_size))
    out.sort(key=lambda p: (p.mtime, p.n), reverse=True)
    return out


def _copier(src: Path):
    def write(fobj):
        with open(src, "rb") as s:
            shutil.copyfileobj(s, fobj, 1 << 20)
    return write


def restore_pair(pair: BakPair, log=None) -> list[Path]:
    """Copy a backup pair over the live files as one all-or-nothing
    transaction. The current pair is first kept as the next free .bak(.N)."""
    return safeio.write_pair([(pair.data, _copier(pair.data_bak)),
                              (pair.lst, _copier(pair.list_bak))],
                             backup=True, log=log)


# --------------------------------------------------------------------------- #
#  dialog
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtCore import Qt, QThread, Signal
    from PySide6.QtWidgets import (QDialog, QFormLayout, QHBoxLayout, QLabel,
                                   QMessageBox, QProgressBar, QPushButton,
                                   QTabWidget, QTreeWidget, QTreeWidgetItem,
                                   QVBoxLayout, QWidget)
    from taiko2x6.app import appconfig
    from taiko2x6.app.dialog_polish import polish
    from taiko2x6.app.errors import friendly_error
    from taiko2x6.app.i18n import tr
    from taiko2x6.ps2 import dialog_kit as kit
    from taiko2x6.ps2.hdd_browser import human_size
except ImportError:  # CLI-only environment
    QDialog = None


def covers_text(e: JournalEntry) -> str:
    if e.error:
        return tr("Unreadable journal")
    c = e.covers
    if not c:
        return ""
    if not c["ranges"]:
        return tr("Nothing saved (image was {size})").format(
            size=human_size(c["original_size"]))
    lo = c["ranges"][0][0]
    hi = c["ranges"][-1][0] + c["ranges"][-1][1]
    return tr("{n} range(s), {saved} saved, offsets {lo} to {hi}; image was "
              "{size}").format(n=len(c["ranges"]), saved=human_size(c["bytes"]),
                               lo="0x%X" % lo, hi="0x%X" % hi,
                               size=human_size(c["original_size"]))


if QDialog is not None:
    class _Job(QThread):
        line = Signal(str)
        done = Signal(object)
        failed = Signal(str)

        def __init__(self, fn):
            super().__init__()
            self._fn = fn

        def run(self):
            try:
                self.done.emit(self._fn(self.line.emit))
            except Exception as exc:
                self.failed.emit(friendly_error(exc))

    class BackupsUndoDialog(QDialog):
        """List undo journals and archive backups; roll back or restore."""

        def __init__(self, parent=None, default_path=""):
            super().__init__(parent)
            self.setWindowTitle(tr("Backups & undo"))
            self.resize(860, 620)
            self._worker = None
            self._gen = 0
            self._entries: list[JournalEntry] = []
            self._pairs: list[BakPair] = []

            lay = QVBoxLayout(self)
            info = QLabel(tr(
                "Every write to a PS2 HDD image keeps an undo journal next to "
                "it; archive saves keep DATA.000 / list.bin as .bak files. "
                "Choose an image (or the folder holding them) to list them."))
            info.setWordWrap(True)
            lay.addWidget(info)

            form = QFormLayout()
            if not default_path:
                default_path = appconfig.last_existing("hddimg")
            self.row_path = kit.PathRow(
                "hddimg", tr("Choose PS2 HDD image"),
                tr("HDD image (*.img);;All files (*)"), text=default_path)
            self.btn_folder = QPushButton(tr("Folder…"))
            self.btn_folder.setAutoDefault(False)
            self.btn_folder.clicked.connect(self._pick_folder)
            hb = QHBoxLayout()
            hb.addWidget(self.row_path, 1)
            hb.addWidget(self.btn_folder)
            form.addRow(tr("Image or folder:"), hb)
            lay.addLayout(form)

            self.lbl_problem = kit.make_problem_label()
            lay.addWidget(self.lbl_problem)

            self.tabs = QTabWidget()
            self.tabs.setUsesScrollButtons(False)
            lay.addWidget(self.tabs, 1)

            # -- journals tab
            pj = QWidget()
            vj = QVBoxLayout(pj)
            order = QLabel(tr(
                "Rolling back to before a journal also rolls back every NEWER "
                "journal of the same image, newest first. That is the only "
                "correct order: each journal holds the bytes from just before "
                "its own session, so the newer changes must be undone first. "
                "Journals that were applied are renamed .undone."))
            order.setWordWrap(True)
            vj.addWidget(order)
            self.tree = QTreeWidget()
            self.tree.setHeaderLabels([tr("When"), tr("Image"), tr("Size"),
                                       tr("Covers"), tr("State")])
            self.tree.setRootIsDecorated(False)
            self.tree.setSelectionMode(QTreeWidget.SingleSelection)
            self.tree.itemSelectionChanged.connect(self._sync)
            vj.addWidget(self.tree, 1)
            row = QHBoxLayout()
            self.btn_rollback = QPushButton(tr("Roll back to before this…"))
            self.btn_rollback.clicked.connect(self._do_rollback)
            self.btn_delete = QPushButton(tr("Delete old journals…"))
            self.btn_delete.setAutoDefault(False)
            self.btn_delete.setToolTip(tr(
                "Deletes the selected journal and every older one of the same "
                "image. The image itself is never touched."))
            self.btn_delete.clicked.connect(self._do_delete)
            row.addWidget(self.btn_rollback)
            row.addWidget(self.btn_delete)
            row.addStretch(1)
            vj.addLayout(row)
            self.tabs.addTab(pj, tr("Image undo journals"))

            # -- archive tab
            pb = QWidget()
            vb = QVBoxLayout(pb)
            hint = QLabel(tr(
                "Restoring copies the chosen DATA.000 + list.bin pair back "
                "together. The current pair is kept as another .bak first."))
            hint.setWordWrap(True)
            vb.addWidget(hint)
            self.tree_bak = QTreeWidget()
            self.tree_bak.setHeaderLabels([tr("Backup"), tr("When"), tr("Size"),
                                           tr("Folder")])
            self.tree_bak.setRootIsDecorated(False)
            self.tree_bak.setSelectionMode(QTreeWidget.SingleSelection)
            self.tree_bak.itemSelectionChanged.connect(self._sync)
            vb.addWidget(self.tree_bak, 1)
            row = QHBoxLayout()
            self.btn_restore = QPushButton(tr("Restore this backup…"))
            self.btn_restore.clicked.connect(self._do_restore)
            row.addWidget(self.btn_restore)
            row.addStretch(1)
            vb.addLayout(row)
            self.tabs.addTab(pb, tr("Archive backups"))

            self.bar = QProgressBar()
            self.bar.setRange(0, 0)
            self.bar.setVisible(False)
            lay.addWidget(self.bar)
            self.log = kit.make_log()
            self.log.setMaximumHeight(110)
            lay.addWidget(self.log)

            row = QHBoxLayout()
            self.btn_refresh = QPushButton(tr("Refresh"))
            self.btn_refresh.setAutoDefault(False)
            self.btn_refresh.clicked.connect(self.refresh)
            self.btn_close = QPushButton(tr("Close"))
            self.btn_close.setAutoDefault(False)
            self.btn_close.clicked.connect(self.reject)
            row.addWidget(self.btn_refresh)
            row.addStretch(1)
            row.addWidget(self.btn_close)
            lay.addLayout(row)

            self.row_path.changed.connect(self.refresh)
            polish(self, primary=self.btn_rollback)
            self.refresh()

        # -- helpers -----------------------------------------------------------
        def _busy(self):
            return self._worker is not None and self._worker.isRunning()

        def _say(self, s):
            kit.log_line(self.log, s)

        def reject(self):
            if self._busy():
                return                  # never abandon a rollback half way
            super().reject()

        def _pick_folder(self):
            from PySide6.QtWidgets import QFileDialog
            d = QFileDialog.getExistingDirectory(self, tr("Choose a folder"))
            if d:
                self.row_path.setText(d)

        def _problem(self):
            p = self.row_path.text()
            if not p:
                return tr("Choose a PS2 HDD image (.img) or a folder first.")
            if not os.path.exists(p):
                return tr("That path does not exist.")
            return ""

        def _selected(self):
            it = self.tree.selectedItems()
            return self._entries[it[0].data(0, Qt.UserRole)] if it else None

        def _selected_pair(self):
            it = self.tree_bak.selectedItems()
            return self._pairs[it[0].data(0, Qt.UserRole)] if it else None

        def _sync(self):
            busy = self._busy()
            e = self._selected()
            self.btn_rollback.setEnabled(bool(e) and not e.used and not busy)
            self.btn_delete.setEnabled(bool(e) and not busy)
            self.btn_restore.setEnabled(self._selected_pair() is not None and not busy)
            self.btn_refresh.setEnabled(not busy)
            self.row_path.setEnabled(not busy)
            self.btn_folder.setEnabled(not busy)
            self.bar.setVisible(busy)

        def _start(self, fn, on_done):
            w = self._worker = _Job(fn)
            w.line.connect(self._say)
            # the result signal arrives while the thread is still winding
            # down; let it finish so the callback may start the next job
            w.done.connect(lambda res: (w.wait(), on_done(res)))
            w.failed.connect(lambda msg: (w.wait(), self._failed(msg)))
            w.finished.connect(self._sync)
            w.start()
            self._sync()

        def _failed(self, msg):
            self._say(tr("Failed: {err}").format(err=msg))
            QMessageBox.warning(self, tr("Backups & undo"), msg)
            self.refresh()

        def wait(self):
            """Block until the running job (if any) finished."""
            if self._worker is not None:
                self._worker.wait()

        # -- listing -----------------------------------------------------------
        def refresh(self):
            if self._busy():
                return
            prob = self._problem()
            kit.set_problem(self.lbl_problem, prob)
            self.tree.clear()
            self.tree_bak.clear()
            self._entries, self._pairs = [], []
            self._sync()
            if prob:
                return
            path = self.row_path.text()
            self._gen += 1
            gen = self._gen

            def scan(_log):
                return (find_journals(path, summarize=True), find_bak_pairs(path))

            def got(res):
                if gen != self._gen:
                    return
                self._fill(*res)
            self._start(scan, got)

        def _fill(self, entries, pairs):
            self._entries, self._pairs = entries, pairs
            for i, e in enumerate(entries):
                it = QTreeWidgetItem(self.tree, [
                    e.when, e.target.name, human_size(e.size), covers_text(e),
                    tr("Rolled back") if e.used else tr("Can roll back")])
                it.setData(0, Qt.UserRole, i)
            for i in range(5):
                self.tree.resizeColumnToContents(i)
            for i, p in enumerate(pairs):
                it = QTreeWidgetItem(self.tree_bak, [
                    p.label, p.when, human_size(p.size), str(p.data.parent)])
                it.setData(0, Qt.UserRole, i)
            for i in range(4):
                self.tree_bak.resizeColumnToContents(i)
            if not entries:
                self._say(tr("No undo journals found."))
            if not pairs:
                self._say(tr("No DATA.000 / list.bin backup pairs found."))
            self._sync()

        # -- actions -----------------------------------------------------------
        def _do_rollback(self):
            e = self._selected()
            if e is None or e.used or self._busy():
                return
            chain = chain_for(self._entries, e)
            names = "\n".join("  %s  (%s)" % (c.when, c.path.name) for c in chain)
            ok = QMessageBox.question(
                self, tr("Roll back"),
                tr("Roll back {img} to how it was before {when}?").format(
                    img=e.target.name, when=e.when)
                + "\n\n" + tr("This undoes {n} journal(s), newest first:").format(
                    n=len(chain)) + "\n" + names
                + "\n\n" + tr("Everything written to the image since then is "
                              "lost. Close PCSX2 first, or the image cannot be "
                              "written."),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ok != QMessageBox.Yes:
                return
            entries = list(self._entries)
            self._say(tr("Rolling back…"))

            def run(log):
                return rollback_chain(entries, e, log=log)

            def done(res):
                self._say(tr("Rolled back {n} journal(s).").format(n=len(res)))
                QMessageBox.information(
                    self, tr("Roll back"),
                    tr("{img} is back to how it was before {when}.").format(
                        img=e.target.name, when=e.when))
                self.refresh()
            self._start(run, done)

        def _do_delete(self):
            e = self._selected()
            if e is None or self._busy():
                return
            victims = older_for_delete(self._entries, e)
            size = sum(v.size for v in victims)
            ok = QMessageBox.question(
                self, tr("Delete old journals"),
                tr("Delete {n} undo journal(s) ({size}) of {img}: the selected "
                   "one and every older one?").format(
                    n=len(victims), size=human_size(size), img=e.target.name)
                + "\n\n" + tr("You will no longer be able to roll back to "
                              "those points. The image itself is not touched."),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ok != QMessageBox.Yes:
                return
            n = delete_journals(victims)
            self._say(tr("Deleted {n} journal(s).").format(n=n))
            self.refresh()

        def _do_restore(self):
            p = self._selected_pair()
            if p is None or self._busy():
                return
            ok = QMessageBox.question(
                self, tr("Restore backup"),
                tr("Restore {data} and {lst} from the {label} backup made "
                   "{when}?").format(data=p.data.name, lst=p.lst.name,
                                     label=p.label, when=p.when)
                + "\n\n" + tr("The current pair is kept as another .bak first, "
                              "so this can be undone."),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ok != QMessageBox.Yes:
                return
            self._say(tr("Restoring…"))

            def done(res):
                self._say(tr("Restored. The previous pair was kept as {names}.")
                          .format(names=", ".join(b.name for b in res)))
                self.refresh()
            self._start(lambda log: restore_pair(p, log=log), done)

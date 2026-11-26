"""Compare two archives (LIST.BIN + DATA.000 pairs), read-only.

Use it to see what an Omnimix build changed against the stock game, or what a
save changed. Files are matched by name inside a group of the same name; a pair
with different sizes is "changed" at once, a pair with the same size is hashed
(SHA-1, only then) to tell "changed" from "unchanged". Each group is decoded
once per side and dropped again, so memory stays small.

Nothing here ever writes to an archive. ``compare_archives`` is plain Python
(testable without Qt); ``ArchiveCompareDialog`` is Tools > Compare archives.
"""
from __future__ import annotations

import csv
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from taiko2x6.app import appconfig
from taiko2x6.app.dialog_polish import polish
from taiko2x6.app.i18n import tr
from taiko2x6.archive.archive_model import Archive
from taiko2x6.safeio import txn_path

log = logging.getLogger("taiko.archivecompare")

ADDED, REMOVED, CHANGED, UNCHANGED = "added", "removed", "changed", "unchanged"
STATUSES = (ADDED, REMOVED, CHANGED, UNCHANGED)
SONG_GROUP = "[songs]"
PREVIEW_LIMIT = 64 * 1024


class Cancelled(Exception):
    pass


@dataclass
class FileDiff:
    group: str
    name: str
    status: str
    size_a: int | None = None
    size_b: int | None = None
    hash_a: str = ""
    hash_b: str = ""
    ref_a: tuple | None = None        # (group index, file index) in A
    ref_b: tuple | None = None


@dataclass
class GroupDiff:
    name: str
    status: str
    files: list = field(default_factory=list)

    def counts(self) -> dict:
        c = dict.fromkeys(STATUSES, 0)
        for f in self.files:
            c[f.status] += 1
        return c


@dataclass
class CompareResult:
    groups: list = field(default_factory=list)
    songs: dict = field(default_factory=dict)

    def file_counts(self) -> dict:
        c = dict.fromkeys(STATUSES, 0)
        for g in self.groups:
            for f in g.files:
                c[f.status] += 1
        return c

    def group_counts(self) -> dict:
        c = dict.fromkeys(STATUSES, 0)
        for g in self.groups:
            c[g.status] += 1
        return c


# ------------------------------------------------------------------ core
def find_pair(folder) -> tuple[Path, Path]:
    """(LIST.BIN, DATA.000) inside `folder` (a file in it also works)."""
    p = Path(folder)
    if p.is_file():
        p = p.parent
    found = {}
    if p.is_dir():
        for child in p.iterdir():
            low = child.name.lower()
            if low in ("list.bin", "data.000") and child.is_file():
                found[low] = child
    if len(found) != 2:
        raise FileNotFoundError(tr("Could not find both LIST.BIN and DATA.000 in:\n{folder}")
                                .format(folder=folder))
    return found["list.bin"], found["data.000"]


def open_readonly(folder) -> Archive:
    list_path, data_path = find_pair(folder)
    # Archive() would finish/undo an interrupted write; a comparison must not
    # touch the files, so refuse instead.
    if txn_path(data_path).exists():
        raise RuntimeError(tr("An unfinished save was found next to {path}; open that "
                              "archive in the Explorer first so it can be recovered.")
                           .format(path=data_path))
    return Archive(list_path, data_path, fmt=2)


def _keyed(entries):
    """name -> [entries]; keeps duplicates in order."""
    out: dict = {}
    for e in entries:
        out.setdefault(e["name"], []).append(e)
    return out


def _sha(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def _slice(payload: bytes, e: dict) -> bytes:
    return payload[e["offset"]:e["offset"] + e["size"]]


def _read_named(arch: Archive, filename: str):
    low = filename.lower()
    for g in arch.layout.groups:
        for e in arch.layout.files_for_group(g):
            if e["name"].lower() == low:
                return _slice(arch.decode_group(g, use_cache=False), e)
    return None


def song_summary(a: Archive, b: Archive) -> dict:
    """Songs added / removed (tuning.bin ids, else musicinfo.bin ids) and
    songs whose genre changed. {} when neither side has the tables."""
    try:
        from taiko2x6.songs import omnimix_maker, song_builder
    except Exception:                                     # pragma: no cover
        return {}

    def side(arch):
        tuning = _read_named(arch, "tuning.bin")
        info = _read_named(arch, "musicinfo.bin")
        genres, ids = {}, None
        if info is not None:
            try:
                genres = omnimix_maker.musicinfo_genres(info)
            except Exception:
                genres = {}
        if tuning is not None:
            try:
                ids = list(song_builder.song_ids_from(tuning))
            except Exception:
                ids = None
        if ids is None and genres:
            ids = list(genres)
        return ids, genres
    ids_a, gen_a = side(a)
    ids_b, gen_b = side(b)
    if ids_a is None and ids_b is None:
        return {}
    sa, sb = set(ids_a or ()), set(ids_b or ())
    return {"count_a": len(sa), "count_b": len(sb),
            "added": sorted(sb - sa), "removed": sorted(sa - sb),
            "genre_changed": sorted(s for s in gen_a.keys() & gen_b.keys()
                                    if gen_a[s] != gen_b[s])}


def compare_archives(a: Archive, b: Archive, progress=None, cancelled=None) -> CompareResult:
    """Compare two open archives. ``progress(done, total)`` is called per
    group; ``cancelled()`` returning True raises Cancelled."""
    ga, gb = _keyed(a.layout.groups), _keyed(b.layout.groups)
    order = list(ga) + [n for n in gb if n not in ga]
    jobs = []                                   # (name, group in A | None, in B | None)
    for name in order:
        la, lb = ga.get(name, []), gb.get(name, [])
        for i in range(max(len(la), len(lb))):
            jobs.append((name, la[i] if i < len(la) else None,
                         lb[i] if i < len(lb) else None))
    res = CompareResult()
    total = len(jobs) + 1
    for n, (name, grp_a, grp_b) in enumerate(jobs):
        if cancelled and cancelled():
            raise Cancelled()
        if progress:
            progress(n, total)
        res.groups.append(_compare_group(a, b, name, grp_a, grp_b))
    if cancelled and cancelled():
        raise Cancelled()
    if progress:
        progress(len(jobs), total)
    res.songs = song_summary(a, b)
    if progress:
        progress(total, total)
    return res


def _compare_group(a, b, name, grp_a, grp_b) -> GroupDiff:
    files_a = a.layout.files_for_group(grp_a) if grp_a else []
    files_b = b.layout.files_for_group(grp_b) if grp_b else []
    pa = a.decode_group(grp_a, use_cache=False) if grp_a else b""
    pb = b.decode_group(grp_b, use_cache=False) if grp_b else b""
    ka, kb = _keyed(files_a), _keyed(files_b)
    gd = GroupDiff(name, CHANGED)
    for fname in list(ka) + [n for n in kb if n not in ka]:
        la, lb = ka.get(fname, []), kb.get(fname, [])
        for i in range(max(len(la), len(lb))):
            ea = la[i] if i < len(la) else None
            eb = lb[i] if i < len(lb) else None
            d = FileDiff(name, fname, UNCHANGED,
                         ea["size"] if ea else None, eb["size"] if eb else None,
                         ref_a=(grp_a["index"], ea["index"]) if ea else None,
                         ref_b=(grp_b["index"], eb["index"]) if eb else None)
            if ea is None:
                d.status = ADDED
            elif eb is None:
                d.status = REMOVED
            elif ea["size"] != eb["size"]:
                d.status = CHANGED                      # no hashing needed
            else:
                d.hash_a, d.hash_b = _sha(_slice(pa, ea)), _sha(_slice(pb, eb))
                d.status = UNCHANGED if d.hash_a == d.hash_b else CHANGED
            gd.files.append(d)
    if grp_a is None:
        gd.status = ADDED
    elif grp_b is None:
        gd.status = REMOVED
    else:
        gd.status = (UNCHANGED if all(f.status == UNCHANGED for f in gd.files)
                     else CHANGED)
    return gd


def status_label(s: str) -> str:
    return {ADDED: tr("Added"), REMOVED: tr("Removed"), CHANGED: tr("Changed"),
            UNCHANGED: tr("Unchanged")}[s]


def write_csv(result: CompareResult, path) -> int:
    """Write the report; returns the number of data rows. Status words are the
    fixed English ones so the file can be diffed or filtered by script."""
    n = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["group", "file", "status", "size_a", "size_b", "sha1_a", "sha1_b"])
        for g in result.groups:
            for f in g.files:
                w.writerow([f.group, f.name, f.status,
                            "" if f.size_a is None else f.size_a,
                            "" if f.size_b is None else f.size_b,
                            f.hash_a, f.hash_b])
                n += 1
        for key in ("added", "removed"):
            for sid in result.songs.get(key, []):
                w.writerow([SONG_GROUP, sid, key, "", "", "", ""])
                n += 1
        for sid in result.songs.get("genre_changed", []):
            w.writerow([SONG_GROUP, sid, "genre_changed", "", "", "", ""])
            n += 1
    return n


# ---------------------------------------------------------------- worker
class CompareWorker(QThread):
    progress = Signal(int, int)
    finished_ok = Signal(object)
    failed = Signal(str)
    cancelled_sig = Signal()

    def __init__(self, src_a, src_b, parent=None):
        """src_a / src_b: a folder path, or an already open Archive (left open)."""
        super().__init__(parent)
        self._a, self._b = src_a, src_b
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        opened = []
        try:
            arch = []
            for src in (self._a, self._b):
                if self._cancel:
                    raise Cancelled()
                if isinstance(src, Archive):
                    arch.append(src)
                else:
                    x = open_readonly(src)
                    opened.append(x)
                    arch.append(x)
            res = compare_archives(arch[0], arch[1], self.progress.emit,
                                   lambda: self._cancel)
            self.finished_ok.emit(res)
        except Cancelled:
            self.cancelled_sig.emit()
        except Exception as exc:
            log.exception("compare failed")
            self.failed.emit(str(exc))
        finally:
            for x in opened:
                x.close()


# --------------------------------------------------------------- preview
def _render(data: bytes) -> str:
    view = data[:PREVIEW_LIMIT]
    try:
        text = view.decode("utf-8")
        if text and sum(c.isprintable() or c in "\r\n\t" for c in text) / len(text) > 0.97:
            more = len(data) - len(view)
            return text + ("\n..." if more else "")
    except UnicodeDecodeError:
        pass
    out = []
    for off in range(0, len(view), 16):
        chunk = view[off:off + 16]
        hexs = " ".join("%02X" % b for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append("%08X  %-47s  %s" % (off, hexs, asc))
    if len(data) > len(view):
        out.append("... (%s more bytes)" % format(len(data) - len(view), ","))
    return "\n".join(out)


class ComparePreviewDialog(QDialog):
    """Both sides of one file next to each other (text, else hex)."""

    def __init__(self, title: str, data_a: bytes, data_b: bytes,
                 label_a: str = "", label_b: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Compare file: {name}").format(name=title))
        self.resize(900, 520)
        from taiko2x6.app import theme
        lay = QVBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        self.views = []
        for lab, data in ((label_a or tr("A"), data_a), (label_b or tr("B"), data_b)):
            box = QWidget()
            v = QVBoxLayout(box)
            v.setContentsMargins(0, 0, 0, 0)
            head = QLabel("%s - %s" % (lab, tr("{n:,} bytes").format(n=len(data))))
            head.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.addWidget(head)
            ed = QPlainTextEdit()
            ed.setReadOnly(True)
            ed.setLineWrapMode(QPlainTextEdit.NoWrap)
            ed.setFont(theme.mono_font())
            ed.setPlainText(_render(data))
            v.addWidget(ed, 1)
            split.addWidget(box)
            self.views.append(ed)
        lay.addWidget(split, 1)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        polish(self, primary=close)


# ---------------------------------------------------------------- dialog
class ArchiveCompareDialog(QDialog):
    def __init__(self, parent=None, current: Archive | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("Compare archives"))
        self.resize(900, 640)
        self.current = current
        self.result: CompareResult | None = None
        self._worker: CompareWorker | None = None
        self._readers: dict = {}                  # side -> Archive opened for previews

        lay = QVBoxLayout(self)
        lay.addWidget(self._hint(tr(
            "Compare two archives (a folder holding LIST.BIN + DATA.000), for "
            "example the stock game against an Omnimix build. Nothing is written.")))
        form = QFormLayout()
        self.edit_a, self.edit_b = QLineEdit(), QLineEdit()
        form.addRow(tr("Archive A (before):"), self._path_row(self.edit_a, "a"))
        form.addRow(tr("Archive B (after):"), self._path_row(self.edit_b, "b"))
        lay.addLayout(form)
        self.chk_open = QCheckBox(tr("Use the open archive as A"))
        self.chk_open.setToolTip(tr("Compares what is saved on disk; pending edits are ignored."))
        self.chk_open.toggled.connect(self._sync_open)
        self.chk_open.setVisible(current is not None)
        lay.addWidget(self.chk_open)

        row = QHBoxLayout()
        self.btn_compare = QPushButton(tr("Compare"))
        self.btn_compare.clicked.connect(self.start)
        self.btn_cancel = QPushButton(tr("Cancel"))
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setAutoDefault(False)
        self.btn_cancel.clicked.connect(self.cancel)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        row.addWidget(self.btn_compare)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.bar, 1)
        lay.addLayout(row)

        self.lbl_counts = QLabel("")
        self.lbl_counts.setWordWrap(True)
        lay.addWidget(self.lbl_counts)

        frow = QHBoxLayout()
        self.cb_status = QComboBox()
        self.cb_status.addItem(tr("All"), "")
        for s in STATUSES:
            self.cb_status.addItem(status_label(s), s)
        self.cb_status.currentIndexChanged.connect(self._apply_filter)
        self.edit_filter = QLineEdit()
        self.edit_filter.setPlaceholderText(tr("Filter by group or file name"))
        self.edit_filter.setClearButtonEnabled(True)
        self.edit_filter.textChanged.connect(self._apply_filter)
        frow.addWidget(QLabel(tr("Show:")))
        frow.addWidget(self.cb_status)
        frow.addWidget(self.edit_filter, 1)
        lay.addLayout(frow)

        split = QSplitter(Qt.Vertical)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels([tr("Group / file"), tr("Status"), tr("Size A"),
                                   tr("Size B"), tr("Counts")])
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setColumnWidth(0, 360)
        self.tree.itemDoubleClicked.connect(self._open_preview)
        split.addWidget(self.tree)
        self.songs_view = QPlainTextEdit()
        self.songs_view.setReadOnly(True)
        self.songs_view.setPlaceholderText(tr("Song summary appears here."))
        split.addWidget(self.songs_view)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        lay.addWidget(split, 1)

        brow = QHBoxLayout()
        self.btn_export = QPushButton(tr("Export CSV…"))
        self.btn_export.setEnabled(False)
        self.btn_export.setAutoDefault(False)
        self.btn_export.clicked.connect(self.export_csv)
        self.btn_close = QPushButton(tr("Close"))
        self.btn_close.setAutoDefault(False)
        self.btn_close.clicked.connect(self.reject)
        brow.addWidget(self.btn_export)
        brow.addStretch(1)
        brow.addWidget(self.btn_close)
        lay.addLayout(brow)
        polish(self, primary=self.btn_compare)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _hint(text):
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        return lbl

    def _path_row(self, edit, which):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton(tr("Browse…"))
        btn.setAutoDefault(False)
        btn.clicked.connect(lambda: self._browse(edit, which))
        h.addWidget(edit, 1)
        h.addWidget(btn)
        w.setFocusProxy(edit)
        return w

    def _browse(self, edit, which):
        p = appconfig.pick_dir(self, "compare_" + which,
                               tr("Choose folder containing LIST.BIN + DATA.000"))
        if p:
            edit.setText(p)

    def _sync_open(self, on):
        self.edit_a.setEnabled(not on)
        if on and self.current is not None:
            self.edit_a.setText(str(self.current.data_path.parent))

    @property
    def running(self) -> bool:
        return self._worker is not None

    # ------------------------------------------------------------ run
    def start(self):
        if self.running:
            return
        use_open = self.current is not None and self.chk_open.isChecked()
        src_a = self.current if use_open else self.edit_a.text().strip()
        src_b = self.edit_b.text().strip()
        if not src_a or not src_b:
            QMessageBox.information(self, tr("Compare archives"),
                                    tr("Choose both archives first."))
            return
        self._close_readers()
        self.result = None
        self.tree.clear()
        self.songs_view.clear()
        self.btn_export.setEnabled(False)
        self.lbl_counts.setText(tr("Comparing…"))
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self._set_busy(True)
        w = self._worker = CompareWorker(src_a, src_b, self)
        w.progress.connect(self._on_progress)
        w.finished_ok.connect(self._on_done)
        w.failed.connect(self._on_failed)
        w.cancelled_sig.connect(self._on_cancelled)
        w.finished.connect(self._on_thread_end)
        w.start()

    def cancel(self):
        if self._worker is not None:
            self._worker.cancel()
            self.btn_cancel.setEnabled(False)

    def _set_busy(self, busy):
        self.btn_compare.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)
        self.edit_b.setEnabled(not busy)
        self.edit_a.setEnabled(not busy and not self.chk_open.isChecked())

    def _on_progress(self, done, total):
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(done)

    def _on_thread_end(self):
        w, self._worker = self._worker, None
        if w is not None:
            w.deleteLater()
        self._set_busy(False)

    def _on_failed(self, msg):
        self.lbl_counts.setText(tr("Comparison failed: {e}").format(e=msg))

    def _on_cancelled(self):
        self.lbl_counts.setText(tr("Cancelled."))
        self.bar.setValue(0)

    def _on_done(self, res: CompareResult):
        self.result = res
        self.bar.setValue(self.bar.maximum())
        fc, gc = res.file_counts(), res.group_counts()
        self.lbl_counts.setText(
            tr("Files: {a} added, {r} removed, {c} changed, {u} unchanged.  "
               "Groups: {ga} added, {gr} removed, {gc} changed, {gu} unchanged.")
            .format(a=fc[ADDED], r=fc[REMOVED], c=fc[CHANGED], u=fc[UNCHANGED],
                    ga=gc[ADDED], gr=gc[REMOVED], gc=gc[CHANGED], gu=gc[UNCHANGED]))
        self._fill_tree()
        self._fill_songs()
        self.btn_export.setEnabled(True)

    # ------------------------------------------------------------ results
    def _fill_tree(self):
        self.tree.setUpdatesEnabled(False)
        try:
            for g in self.result.groups:
                c = g.counts()
                top = QTreeWidgetItem([g.name, status_label(g.status), "", "",
                                       tr("+{a} -{r} ~{c} ={u}").format(
                                           a=c[ADDED], r=c[REMOVED], c=c[CHANGED], u=c[UNCHANGED])])
                top.setData(0, Qt.UserRole, g.status)
                for f in g.files:
                    it = QTreeWidgetItem([
                        f.name, status_label(f.status),
                        "" if f.size_a is None else format(f.size_a, ","),
                        "" if f.size_b is None else format(f.size_b, ","), ""])
                    it.setData(0, Qt.UserRole, f.status)
                    it.setData(1, Qt.UserRole, f)
                    for col in (2, 3):
                        it.setTextAlignment(col, Qt.AlignRight | Qt.AlignVCenter)
                    top.addChild(it)
                self.tree.addTopLevelItem(top)
        finally:
            self.tree.setUpdatesEnabled(True)
        self._apply_filter()

    def _fill_songs(self):
        s = self.result.songs
        if not s:
            self.songs_view.setPlainText(tr("No song tables (tuning.bin / musicinfo.bin) to compare."))
            return
        lines = [tr("Songs: {a} in A, {b} in B.").format(a=s["count_a"], b=s["count_b"]),
                 tr("Added ({n}): {ids}").format(n=len(s["added"]), ids=", ".join(s["added"]) or "-"),
                 tr("Removed ({n}): {ids}").format(n=len(s["removed"]), ids=", ".join(s["removed"]) or "-")]
        if s["genre_changed"]:
            lines.append(tr("Genre changed ({n}): {ids}").format(
                n=len(s["genre_changed"]), ids=", ".join(s["genre_changed"])))
        self.songs_view.setPlainText("\n".join(lines))

    def _apply_filter(self, *_):
        want = self.cb_status.currentData() or ""
        text = self.edit_filter.text().strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            gmatch = bool(text) and text in top.text(0).lower()
            shown = 0
            for j in range(top.childCount()):
                ch = top.child(j)
                ok = (not want or ch.data(0, Qt.UserRole) == want) and \
                     (not text or gmatch or text in ch.text(0).lower())
                ch.setHidden(not ok)
                shown += ok
            top.setHidden(shown == 0 and (top.childCount() > 0 or bool(want) or bool(text)))

    def visible_files(self) -> list:
        """(group, file, status) of the rows the filter currently shows."""
        out = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top.isHidden():
                continue
            for j in range(top.childCount()):
                ch = top.child(j)
                if not ch.isHidden():
                    out.append((top.text(0), ch.text(0), ch.data(0, Qt.UserRole)))
        return out

    # ------------------------------------------------------------ preview
    def _reader(self, side):
        arch = self._readers.get(side)
        if arch is None:
            if side == "a" and self.current is not None and self.chk_open.isChecked():
                return self.current
            arch = open_readonly((self.edit_a if side == "a" else self.edit_b).text().strip())
            self._readers[side] = arch
        return arch

    def file_bytes(self, side, ref) -> bytes:
        if ref is None:
            return b""
        arch = self._reader(side)
        grp = arch.layout.groups[ref[0]]
        ent = arch.layout.files[ref[1]]
        return _slice(arch.decode_group(grp), ent)

    def _open_preview(self, item, _col=0):
        f = item.data(1, Qt.UserRole)
        if not isinstance(f, FileDiff):
            return
        try:
            a, b = self.file_bytes("a", f.ref_a), self.file_bytes("b", f.ref_b)
        except Exception as exc:
            QMessageBox.warning(self, tr("Compare archives"),
                                tr("Comparison failed: {e}").format(e=exc))
            return
        ComparePreviewDialog(f.group + "/" + f.name, a, b, tr("A"), tr("B"), self).exec()

    def _close_readers(self):
        for arch in self._readers.values():
            arch.close()
        self._readers.clear()

    # ------------------------------------------------------------ export
    def export_csv(self, path: str | None = None):
        if self.result is None:
            return
        if not path:
            path, _ = QFileDialog.getSaveFileName(
                self, tr("Export CSV…"), "archive_compare.csv", tr("CSV (*.csv);;All (*)"))
        if not path:
            return
        try:
            n = write_csv(self.result, path)
        except OSError as exc:
            QMessageBox.warning(self, tr("Compare archives"),
                                tr("Could not write the file: {e}").format(e=exc))
            return
        self.lbl_counts.setText(tr("Report saved: {n} rows.").format(n=n))

    # ------------------------------------------------------------ close
    def _stop_worker(self):
        w = self._worker
        if w is not None:
            w.cancel()
            w.wait(10000)

    def reject(self):
        self._stop_worker()
        self._close_readers()
        super().reject()

    def closeEvent(self, e):
        self._stop_worker()
        self._close_readers()
        super().closeEvent(e)

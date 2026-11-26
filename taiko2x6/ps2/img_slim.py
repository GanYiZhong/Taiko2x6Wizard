#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
img_slim.py -- reclaim disk space from a PS2 HDD (.img) without changing what
the emulator reads.

WHY THIS IS SAFE
----------------
A raw HDD image is mostly zeros: tnt14plus.img is 80 GB of file holding 6.44 GB
of APA partitions, and the partitions themselves are largely empty. Every byte
we reclaim is a byte that already reads as 0x00 and STILL reads as 0x00
afterwards -- so this needs no understanding of APA or PFS at all, and cannot
corrupt a filesystem it never interprets.

Two mechanisms, both lossless:

  sparse (default)  Punch NTFS holes over runs of zeros. The file's logical size
                    and every byte read from it are unchanged; only the physical
                    allocation drops. Reversible, and safe while the layout is
                    still being edited -- writes just re-allocate.

  truncate          Cut the trailing zeros off the end entirely. Produces a small
                    portable file, but changes the disk size the emulator sees.
                    Only offered when the whole tail is verified zero, and it is
                    reversible with --extend since the removed bytes were zeros.

Everything is verified before it is destroyed: a region is re-read and confirmed
zero immediately before it is punched, and --verify re-reads afterwards.

Windows/NTFS only (uses FSCTL_SET_SPARSE / FSCTL_SET_ZERO_DATA).

CLI:
    python -m taiko2x6.ps2.img_slim <image>                    analyze only (default; no writes)
    python -m taiko2x6.ps2.img_slim <image> --sparse           punch holes over zero runs
    python -m taiko2x6.ps2.img_slim <image> --truncate         cut the trailing zero tail
    python -m taiko2x6.ps2.img_slim <image> --extend <bytes>   undo a truncate
"""

import ctypes
import os
import struct
import sys
from ctypes import wintypes

SECTOR = 512
CLUSTER = 65536          # punch granularity; NTFS frees whole clusters only
CHUNK = 32 * 1024 * 1024

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
OPEN_EXISTING = 3
FSCTL_SET_SPARSE = 0x000900C4
FSCTL_SET_ZERO_DATA = 0x000980C8
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_ZEROS = bytes(CHUNK)


class _ZeroDataInfo(ctypes.Structure):
    _fields_ = [("FileOffset", ctypes.c_longlong),
                ("BeyondFinalZero", ctypes.c_longlong)]


def _win():
    if os.name != "nt":
        raise RuntimeError("img_slim needs Windows/NTFS (sparse-file support)")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _open_rw(path):
    """Open for read/write with no write-sharing.

    PCSX2 keeps the image open while it runs, so this fails fast with a clear
    message rather than half-punching a live disk.
    """
    k32 = _win()
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW(str(path), GENERIC_READ | GENERIC_WRITE,
                        FILE_SHARE_READ, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE or h is None:
        err = ctypes.get_last_error()
        if err in (32, 33):
            raise RuntimeError(
                "image is locked by another process -- close PCSX2 first")
        raise ctypes.WinError(err)
    return k32, h


def is_sparse(path):
    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    return attrs != 0xFFFFFFFF and bool(attrs & 0x200)


FILE_SUPPORTS_SPARSE_FILES = 0x00000040


def filesystem_of(path):
    """(fs_name, supports_sparse) for the volume holding ``path``.

    Sparse files are an NTFS feature -- exFAT and FAT volumes cannot do it, and
    plenty of big media drives are exFAT. Without this check the tool happily
    reports "reclaimable by --sparse" on a volume where the punch can only fail.
    """
    k32 = _win()
    root = os.path.splitdrive(os.path.abspath(str(path)))[0] + "\\"
    name = ctypes.create_unicode_buffer(256)
    flags = wintypes.DWORD()
    ok = k32.GetVolumeInformationW(root, None, 0, None, None,
                                   ctypes.byref(flags), name, 256)
    if not ok:
        return ("?", False)
    return (name.value, bool(flags.value & FILE_SUPPORTS_SPARSE_FILES))


def allocated_size(path):
    """Physical bytes the file occupies (differs from logical once sparse)."""
    k32 = _win()
    # restype matters: the default (signed int) makes the low DWORD go negative
    # above 2 GiB and silently corrupts the 64-bit recombination below.
    k32.GetCompressedFileSizeW.restype = wintypes.DWORD
    high = wintypes.DWORD(0)
    low = k32.GetCompressedFileSizeW(str(path), ctypes.byref(high))
    if low == 0xFFFFFFFF:
        err = ctypes.get_last_error()
        if err:
            raise ctypes.WinError(err)
    return (high.value << 32) | low


def apa_end(path):
    """Highest byte used by any APA partition, or None if not an APA image.

    Walks the on-disk partition chain rather than trusting any single header:
    each APA header carries its own start/length, so the end is the max over all
    of them.
    """
    try:
        from taiko2x6.ps2 import ps2hdd
    except ImportError:
        return None
    try:
        h = ps2hdd.Ps2Hdd(str(path))
        try:
            parts = h.partitions()
        finally:
            h.close()
    except Exception:
        return None
    if not parts:
        return None
    return max((p["start_lba"] + p["sectors"]) for p in parts) * SECTOR


def stamp(path):
    """(size, mtime_ns) -- identity of the file's content at a point in time.

    Taken BEFORE a scan and re-checked before we act on the scan's result. If it
    still matches, nothing has written to the file since, so the scan is still
    true and there is no reason to re-read 70+ GB to "confirm" it. This is both
    faster and stricter than re-verifying the runs: it notices a write anywhere
    in the file, not just inside the regions we were about to touch.
    """
    st = os.stat(path)
    return (st.st_size, st.st_mtime_ns)


def zero_runs(path, min_run=CLUSTER, progress=None):
    """Yield (start, end) of cluster-aligned runs that are entirely zero.

    Only whole clusters are reported: NTFS cannot free a partial one, so a run
    is trimmed inward to cluster boundaries and dropped if nothing survives.
    """
    size = os.path.getsize(path)
    runs = []
    run_start = None
    pos = 0
    with open(path, "rb") as f:
        while pos < size:
            n = min(CHUNK, size - pos)
            buf = f.read(n)
            if not buf:
                break
            if len(buf) == len(_ZEROS) and buf == _ZEROS:
                if run_start is None:
                    run_start = pos
            elif not buf.strip(b"\x00"):
                if run_start is None:
                    run_start = pos
            else:
                # Mixed chunk: walk it at cluster granularity so a single stray
                # byte doesn't discard the megabytes of zeros around it.
                for off in range(0, len(buf), CLUSTER):
                    piece = buf[off:off + CLUSTER]
                    if piece.strip(b"\x00"):
                        if run_start is not None:
                            _emit(runs, run_start, pos + off, min_run)
                            run_start = None
                    elif run_start is None:
                        run_start = pos + off
            pos += len(buf)
            if progress:
                progress(pos, size)
    if run_start is not None:
        _emit(runs, run_start, size, min_run)
    return runs


def _emit(runs, start, end, min_run):
    a = (start + CLUSTER - 1) // CLUSTER * CLUSTER   # round inward
    b = end // CLUSTER * CLUSTER
    if b - a >= max(min_run, CLUSTER):
        runs.append((a, b))


def _assert_zero(f, start, end):
    """Re-read a region and confirm it is zero. Nothing is punched without this."""
    f.seek(start)
    pos = start
    while pos < end:
        buf = f.read(min(CHUNK, end - pos))
        if not buf:
            raise IOError("short read verifying %d..%d" % (start, end))
        if buf.strip(b"\x00"):
            return False
        pos += len(buf)
    return True


def punch(path, runs, scanned=None, log=print):
    """Deallocate zero runs. Returns (bytes_freed, new_stamp).

    ``scanned`` is the stamp taken before the scan that produced ``runs``. When
    it still matches, the runs are known-good and are punched directly; only an
    unverifiable run gets re-read (which costs a full pass over it).
    """
    fs, sparse_ok = filesystem_of(path)
    if not sparse_ok:
        raise RuntimeError(
            "%s is on %s, which has no sparse-file support -- nothing can be "
            "reclaimed this way. Use truncation instead." % (path, fs))
    fresh = scanned is not None and scanned == stamp(path)
    if not fresh and scanned is not None:
        log("  file changed since the scan -- re-verifying before punching")
    k32, h = _open_rw(path)
    freed = 0
    try:
        ret = wintypes.DWORD()
        if not k32.DeviceIoControl(h, FSCTL_SET_SPARSE, None, 0, None, 0,
                                   ctypes.byref(ret), None):
            raise ctypes.WinError(ctypes.get_last_error())
        with open(path, "rb") as rf:
            for start, end in runs:
                if not fresh and not _assert_zero(rf, start, end):
                    log("  SKIP %d..%d -- not zero after all" % (start, end))
                    continue
                info = _ZeroDataInfo(start, end)
                if not k32.DeviceIoControl(h, FSCTL_SET_ZERO_DATA,
                                           ctypes.byref(info),
                                           ctypes.sizeof(info), None, 0,
                                           ctypes.byref(ret), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                freed += end - start
    finally:
        k32.CloseHandle(h)
    # Punching only replaced zeros with zeros, so the scan's runs still hold --
    # hand back a refreshed stamp so a follow-up truncate stays instant.
    return freed, stamp(path)


def truncate_tail(path, keep_from, scanned=None, log=print):
    """Cut everything at/after keep_from. Returns bytes removed.

    Truncation itself is O(1); the cost is proving the tail is zero first. If
    ``scanned`` still matches, that proof already happened during the scan and
    is not repeated -- otherwise the tail is read in full before anything is
    destroyed.
    """
    size = os.path.getsize(path)
    if keep_from >= size:
        return 0
    if scanned is None or scanned != stamp(path):
        if scanned is not None:
            log("  file changed since the scan -- re-verifying the tail")
        with open(path, "rb") as f:
            if not _assert_zero(f, keep_from, size):
                raise RuntimeError(
                    "refusing to truncate: bytes beyond %d are NOT all zero"
                    % keep_from)
    with open(path, "r+b") as f:
        f.truncate(keep_from)
    return size - keep_from


def extend(path, new_size, log=print):
    """Undo a truncate by re-extending with zeros (what was removed)."""
    size = os.path.getsize(path)
    if new_size <= size:
        raise RuntimeError("target size %d is not larger than current %d"
                           % (new_size, size))
    with open(path, "r+b") as f:
        f.truncate(new_size)
    return new_size - size


def _gb(n):
    return "%.2f GB" % (n / 1e9)


def analyze(path, log=print):
    size = os.path.getsize(path)
    alloc = allocated_size(path)
    end = apa_end(path)
    log("image      : %s" % path)
    log("logical    : %s" % _gb(size))
    log("on disk    : %s%s" % (_gb(alloc), "  (sparse)" if is_sparse(path) else ""))
    if end:
        log("APA end    : %s   (highest byte any partition uses)" % _gb(end))
        log("tail       : %s beyond the last partition" % _gb(size - end))
    log("")
    log("scanning for zero runs ...")

    before = stamp(path)          # taken BEFORE the scan; see stamp()
    state = {"last": -1}

    def prog(pos, total):
        pct = int(pos * 100 / total)
        if pct != state["last"] and pct % 10 == 0:
            log("  %3d%%  (%s)" % (pct, _gb(pos)))
            state["last"] = pct

    runs = zero_runs(path, progress=prog)
    total = sum(b - a for a, b in runs)
    log("")
    log("zero runs  : %d, totalling %s (%.1f%% of the file)"
        % (len(runs), _gb(total), 100.0 * total / size if size else 0))
    fs, sparse_ok = filesystem_of(path)
    if sparse_ok:
        log("reclaimable: %s by --sparse" % _gb(total))
    else:
        log("reclaimable: nothing by --sparse -- this volume is %s, which has "
            "no sparse-file support" % fs)
    if end and runs and runs[-1][1] >= size - CLUSTER and runs[-1][0] <= end:
        log("             %s by --truncate (tail is contiguous zeros from %s)"
            % (_gb(size - end), _gb(end)))
    return runs, total, end, before


# ===========================================================================
# GUI
# ===========================================================================
def partition_usage(path):
    """[(name, size, used, free, note)] for each APA partition, best effort.

    `used`/`free` are None when the partition has no readable PFS superblock --
    reported as-is rather than guessed at, since an unknown partition is exactly
    the kind we must never shrink.
    """
    try:
        from taiko2x6.ps2 import ps2hdd
    except ImportError:
        return []
    rows = []
    try:
        h = ps2hdd.Ps2Hdd(str(path))
    except Exception:
        return []
    try:
        for p in h.partitions():
            name = p["name"]
            size = p["sectors"] * SECTOR
            used = free = None
            note = ""
            if not p["is_pfs"]:
                note = "not PFS"
            else:
                try:
                    h._mount(name)
                    sb = h._supers[name]
                    zs = sb["zone_size"]
                    part = h._find_part(name)
                    total = sum(h._zones_per_subpart(part, s)
                                for s in range(sb["num_subs"] + 1))
                    fz = h._total_free_zones(name, {})
                    used = (total - fz) * zs
                    free = fz * zs
                except Exception as exc:
                    note = str(exc)[:40]
            rows.append((name, size, used, free, note))
    finally:
        h.close()
    return rows


def slim_problem(path) -> str:
    """Why `path` cannot be analysed ('' when it can)."""
    from taiko2x6.app.i18n import tr
    if not path:
        return tr("Choose a PS2 HDD image (.img) first.")
    if not os.path.exists(path):
        return tr("The image file does not exist.")
    if not os.path.isfile(path):
        return tr("The image path is a folder, not a file.")
    if os.name != "nt":
        return tr("Slimming uses NTFS sparse files and needs Windows.")
    return ""


try:
    from PySide6.QtCore import Qt, QThread, Signal, QTimer
    from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
                                   QLabel, QTreeWidget, QTreeWidgetItem,
                                   QMessageBox, QProgressBar, QGroupBox,
                                   QFormLayout)
    from taiko2x6.app.i18n import tr
    from taiko2x6.app.dialog_polish import polish
    from taiko2x6.app.errors import friendly_error
    from taiko2x6.ps2 import dialog_kit as kit
    from taiko2x6.ps2.hdd_browser import human_size
except ImportError:  # CLI-only environment
    QDialog = None


class _Cancelled(Exception):
    pass


if QDialog is not None:
    class _ScanWorker(QThread):
        progress = Signal(int, str)
        done = Signal(object, object, object, object)
        failed = Signal(str)

        def __init__(self, path):
            super().__init__()
            self.path = path
            self._stop = False

        def cancel(self):
            self._stop = True

        def run(self):
            try:
                before = stamp(self.path)     # BEFORE the scan; see stamp()
                state = {"pct": -1}

                def prog(pos, total):
                    if self._stop:
                        raise _Cancelled()
                    pct = int(pos * 100 / total) if total else 100
                    if pct != state["pct"]:
                        state["pct"] = pct
                        self.progress.emit(pct, "%s / %s" % (
                            human_size(pos), human_size(total)))

                runs = zero_runs(self.path, progress=prog)
                total = sum(b - a for a, b in runs)
                self.done.emit(runs, total, apa_end(self.path), before)
            except _Cancelled:
                self.failed.emit("")
            except Exception as exc:
                self.failed.emit(friendly_error(exc))

    class _TruncWorker(QThread):
        line = Signal(str)
        done = Signal(object)        # bytes removed
        failed = Signal(str)

        def __init__(self, path, end, scanned):
            super().__init__()
            self.args = (path, end, scanned)

        def run(self):
            path, end, scanned = self.args
            try:
                self.done.emit(truncate_tail(path, end, scanned=scanned,
                                             log=self.line.emit))
            except Exception as exc:
                self.failed.emit(friendly_error(exc))

    class ImgSlimDialog(QDialog):
        """Reclaim space from a PS2 HDD image.

        Offers one operation: cutting the tail past the last APA partition, which is
        read in full and proved to be zero before anything is removed (and is
        undoable via --extend, since what it removes is zeros).

        Resizing partitions is NOT offered: the whole APA region is 6.44 GB of an
        80 GB image, so the tail is where all the space is.
        """

        def __init__(self, parent=None, default_img=""):
            super().__init__(parent)
            from taiko2x6.app import appconfig
            self.setWindowTitle(tr("Slim PS2 HDD image"))
            self.resize(780, 620)
            self._runs = None
            self._end = None
            self._scanned = None
            self._worker = None
            self._shown = ""         # image whose partitions the tree shows

            lay = QVBoxLayout(self)
            info = QLabel(tr(
                "Cuts the empty tail past the last partition off a PS2 HDD "
                "image. Analyze first: the whole file is read and the tail is "
                "proved to be zero before anything is removed."))
            info.setWordWrap(True)
            lay.addWidget(info)

            box = QGroupBox(tr("Image"))
            form = QFormLayout(box)
            if not default_img:
                default_img = appconfig.last_existing("hddimg")
            self.row_img = kit.PathRow("hddimg", tr("Choose PS2 HDD image"),
                                       tr("HDD image (*.img);;All files (*)"),
                                       text=default_img)
            form.addRow(tr("Image:"), self.row_img)
            self.lbl_sizes = QLabel("")
            form.addRow("", self.lbl_sizes)
            lay.addWidget(box)

            self.tree = QTreeWidget()
            self.tree.setHeaderLabels([tr("Partition"), tr("Size"), tr("Used"),
                                       tr("Free"), tr("Note")])
            self.tree.setRootIsDecorated(False)
            lay.addWidget(self.tree, 1)

            self.lbl_problem = kit.make_problem_label()
            lay.addWidget(self.lbl_problem)
            self.bar = QProgressBar()
            self.bar.setVisible(False)
            lay.addWidget(self.bar)

            self.log = kit.make_log()
            lay.addWidget(self.log, 1)

            row2 = QHBoxLayout()
            self.btn_scan = QPushButton(tr("Analyze"))
            self.btn_scan.clicked.connect(self._scan)
            self.btn_cancel = QPushButton(tr("Cancel"))
            self.btn_cancel.setAutoDefault(False)
            self.btn_cancel.clicked.connect(self._cancel)
            self.btn_trunc = QPushButton(tr("Truncate tail"))
            self.btn_trunc.setAutoDefault(False)
            self.btn_trunc.setToolTip(tr(
                "Cut the verified-zero tail past the last partition. Makes a "
                "small portable file, but changes the disk size the emulator "
                "sees."))
            self.btn_trunc.clicked.connect(self._do_truncate)
            self.btn_close = QPushButton(tr("Close"))
            self.btn_close.setAutoDefault(False)
            self.btn_close.clicked.connect(self.reject)
            for b in (self.btn_scan, self.btn_cancel, self.btn_trunc):
                row2.addWidget(b)
            row2.addStretch(1)
            row2.addWidget(self.btn_close)
            lay.addLayout(row2)

            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.setInterval(300)
            self._timer.timeout.connect(self._path_settled)
            self.row_img.changed.connect(self._on_path_changed)
            self._path_settled()
            polish(self, primary=self.btn_scan)

        # -- helpers -----------------------------------------------------------
        def _img(self):
            p = self.row_img.text()
            return p if p and not slim_problem(p) else None

        def _busy(self):
            return self._worker is not None and self._worker.isRunning()

        def _say(self, s):
            kit.log_line(self.log, s)

        def _sync(self):
            p = slim_problem(self.row_img.text())
            kit.set_problem(self.lbl_problem, p)
            busy = self._busy()
            self.btn_scan.setEnabled(not p and not busy)
            self.btn_scan.setToolTip(p)
            self.btn_cancel.setEnabled(busy and isinstance(self._worker, _ScanWorker))
            self.btn_trunc.setEnabled(
                not p and not busy and bool(self._end) and self._runs is not None
                and self._end < os.path.getsize(self.row_img.text()))
            self.row_img.setEnabled(not busy)

        def _on_path_changed(self):
            self._runs = None
            self._end = None
            self._sync()
            self._timer.start()

        def _path_settled(self):
            self._timer.stop()
            self._sync()
            p = self._img()
            if p and p != self._shown:
                self._refresh_parts()
            elif not p:
                self.tree.clear()
                self.lbl_sizes.setText("")
                self._shown = ""

        def _refresh_parts(self):
            self.tree.clear()
            p = self._img()
            self._shown = p or ""
            if not p:
                return
            try:
                size = os.path.getsize(p)
                alloc = allocated_size(p)
                self.lbl_sizes.setText(tr(
                    "Logical size {size}, on disk {alloc}{sparse}").format(
                    size=human_size(size), alloc=human_size(alloc),
                    sparse=tr(" (already sparse)") if is_sparse(p) else ""))
                parts = partition_usage(p)
            except Exception as exc:
                self.lbl_sizes.setText("")
                self._say(tr("Could not read the image: {err}").format(
                    err=friendly_error(exc)))
                return
            for name, psize, used, free, note in parts:
                QTreeWidgetItem(self.tree, [
                    name, human_size(psize),
                    human_size(used) if used is not None else "-",
                    human_size(free) if free is not None else "-",
                    note])
            for i in range(5):
                self.tree.resizeColumnToContents(i)
            self._say(tr("Press Analyze to scan for reclaimable zero regions "
                         "(reads the whole file; takes a few minutes)."))

        # -- scan --------------------------------------------------------------
        def _scan(self):
            p = self._img()
            if not p:
                return
            self._runs = None
            self._end = None
            self.bar.setVisible(True)
            self.bar.setValue(0)
            self._say(tr("Scanning {path} …").format(path=p))
            self._worker = _ScanWorker(p)
            self._worker.progress.connect(self._on_progress)
            self._worker.done.connect(self._on_done)
            self._worker.failed.connect(self._on_failed)
            self._worker.start()
            self._sync()

        def _cancel(self):
            if isinstance(self._worker, _ScanWorker):
                self._worker.cancel()

        def _on_progress(self, pct, text):
            self.bar.setValue(pct)
            self.bar.setFormat("%s  (%%p%%)" % text)

        def _on_done(self, runs, total, end, scanned):
            self._worker.wait()
            self._runs, self._end, self._scanned = runs, end, scanned
            self.bar.setVisible(False)
            size = os.path.getsize(self._img())
            self._say(tr("Zero runs: {n}, totalling {total} ({pct:.1f}% of the file)")
                      .format(n=len(runs), total=human_size(total),
                              pct=100.0 * total / size if size else 0))
            if end:
                self._say(tr("Reclaimable: {size} by Truncate tail (partitions end at {end})")
                          .format(size=human_size(max(size - end, 0)),
                                  end=human_size(end)))
            else:
                self._say(tr("The partition table could not be read; "
                             "Truncate tail is unavailable."))
            self._sync()

        def _on_failed(self, msg):
            self._worker.wait()
            self.bar.setVisible(False)
            self._say(tr("Scan cancelled.") if not msg
                      else tr("Scan failed: {err}").format(err=msg))
            self._sync()

        # -- actions -----------------------------------------------------------
        # Sparse-punching is deliberately not offered here -- truncation covers the
        # need and a sparse file silently re-expands to full size when copied by
        # anything that does not preserve sparseness (Explorer, most backup tools).
        # `img_slim.py <image> --sparse` still does it from the CLI.

        def _do_truncate(self):
            p = self._img()
            if not p or not self._end:
                return
            size = os.path.getsize(p)
            if QMessageBox.question(
                    self, tr("Truncate tail"),
                    tr("Cut {cut} of zeros past the last partition?\n\n"
                       "The tail is verified zero before anything is removed, "
                       "and this is undoable (Extend back to {size} bytes). "
                       "But the emulator will see a {new} disk instead of "
                       "{old}.\n\nClose PCSX2 first.").format(
                        cut=human_size(size - self._end), size=size,
                        new=human_size(self._end), old=human_size(size))
                    ) != QMessageBox.Yes:
                return
            self._orig_size = size
            self.bar.setRange(0, 0)
            self.bar.setFormat(tr("Verifying and truncating…"))
            self.bar.setVisible(True)
            self._worker = _TruncWorker(p, self._end, self._scanned)
            self._worker.line.connect(self._say)
            self._worker.done.connect(self._trunc_done)
            self._worker.failed.connect(self._trunc_failed)
            self._worker.start()
            self._sync()

        def _trunc_reset_bar(self):
            self._worker.wait()
            self.bar.setRange(0, 100)
            self.bar.setFormat("%p%")
            self.bar.setVisible(False)

        def _trunc_done(self, cut):
            self._trunc_reset_bar()
            p = self._img() or self.row_img.text()
            self._say(tr("Removed {cut} -- the file is now {size}").format(
                cut=human_size(cut), size=human_size(os.path.getsize(p))))
            self._say("undo:  python -m taiko2x6.ps2.img_slim \"%s\" --extend %d"
                      % (p, self._orig_size))
            self._runs = None
            self._end = None
            self._shown = ""
            self._path_settled()

        def _trunc_failed(self, msg):
            self._trunc_reset_bar()
            self._say(tr("FAILED: {err}").format(err=msg))
            QMessageBox.critical(self, tr("Truncate tail"), msg)
            self._sync()

        def reject(self):
            if self._busy():
                if isinstance(self._worker, _ScanWorker):
                    self._worker.cancel()
                    self._worker.wait(5000)
                else:
                    QMessageBox.information(self, tr("Truncate tail"), tr(
                        "The image is being changed; let it finish."))
                    return
            super().reject()

        def closeEvent(self, ev):
            if self._busy() and not isinstance(self._worker, _ScanWorker):
                ev.ignore()
                return
            if self._busy():
                self._worker.cancel()
                self._worker.wait(5000)
            super().closeEvent(ev)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    path = argv[0]
    if not os.path.exists(path):
        print("no such image: %s" % path)
        return 1
    do_sparse = "--sparse" in argv
    do_trunc = "--truncate" in argv
    if "--extend" in argv:
        n = int(argv[argv.index("--extend") + 1])
        added = extend(path, n)
        print("extended by %s -> %s" % (_gb(added), _gb(os.path.getsize(path))))
        return 0

    runs, total, end, sc = analyze(path)

    if do_sparse:
        print("\npunching %d zero runs ..." % len(runs))
        freed, sc = punch(path, runs, scanned=sc)
        print("freed %s" % _gb(freed))
        print("on disk now: %s (logical unchanged at %s)"
              % (_gb(allocated_size(path)), _gb(os.path.getsize(path))))
    if do_trunc:
        if not end:
            print("\nrefusing to truncate: could not read the APA partition table")
            return 1
        size = os.path.getsize(path)
        print("\ntruncating to APA end (%s) ..." % _gb(end))
        cut = truncate_tail(path, end, scanned=sc)
        print("removed %s -> file is now %s" % (_gb(cut), _gb(os.path.getsize(path))))
        print("undo with:  python -m taiko2x6.ps2.img_slim %s --extend %d" % (path, size))
    if not (do_sparse or do_trunc):
        print("\n(analysis only -- pass --sparse or --truncate to act)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

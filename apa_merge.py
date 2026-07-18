#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
apa_merge.py -- combine several System246/256 game HDD images into ONE APA disk,
so a cabinet can hold every game and the memory card alone selects which runs.

WHY THIS WORKS
--------------
Each game's card carries its own loader (T12LOAD/T13LOAD/T14LOAD/TECLOAD/TEDLOAD)
and each loader builds its partition name at runtime from the format string
`hdd0:%s%04d.%04d,%s` plus its own baked-in game code ("taiko", "t13jp", "t14jp",
"t12tr", "t12sm"). It then opens that partition BY NAME. Names are unique per
game, so extra partitions belonging to other games are simply invisible to it.
The five system partitions (__mbr/__net/__system/__sysconf/__common) are byte-for
-byte identically laid out in every image, so one copy serves all games.

WHAT IS ASSUMED (and checked)
-----------------------------
Merging forces every game partition except one to a NEW start LBA, so PFS must be
position-independent. It appears to be -- the superblock is found by computing
`part.start + 0x400000` rather than being stored, and inodes address zones as
(subpart, relative number). `--verify` proves it per merge by re-reading every
file out of the merged image and comparing hashes against the source, so this is
never taken on faith.

APA layout rules honoured here, read off real images rather than assumed:
  * a partition header (1024 B) lives at the partition's own start LBA
  * `lba == start`; `next` = next partition's LBA, 0 on the last one
  * `prev` = previous partition's LBA, and `__mbr.prev` is the tail pointer
  * every partition is aligned to a multiple of its own length
  * checksum = sum of u32 words 1..255 of the header (word 0 excluded)

Nothing here writes to a source image; the output is always a new file.

CLI:
    python apa_merge.py --list <img> [<img> ...]
    python apa_merge.py -o merged.img <img> [<img> ...] [--verify]
"""

import hashlib
import os
import struct
import sys

import ps2hdd

SECTOR = 512
HDR = 1024
CHUNK = 32 * 1024 * 1024
SYS_PREFIX = "__"


class MergeError(Exception):
    pass


# --------------------------------------------------------------------------- #
#  reading sources
# --------------------------------------------------------------------------- #
def read_layout(img):
    """[(ApaPartition, is_system)] for an image, in chain order."""
    h = ps2hdd.Ps2Hdd(str(img))
    try:
        parts = h._read_apa_chain()
    finally:
        h.close()
    out = []
    for p in parts:
        if not p.checksum_valid:
            raise MergeError("%s: partition %r has a bad checksum -- refusing to "
                             "merge a damaged image" % (img, p.id))
        if p.nsub:
            raise MergeError("%s: partition %r has %d sub-partitions; this tool "
                             "only handles simple partitions"
                             % (img, p.id, p.nsub))
        out.append((p, p.id.startswith(SYS_PREFIX)))
    return out


def system_signature(layout):
    """Identity of the system partitions -- used to reject mismatched sources."""
    return tuple((p.id, p.start, p.length, p.type) for p, sysp in layout if sysp)


def game_parts(layout):
    return [p for p, sysp in layout if not sysp]


# --------------------------------------------------------------------------- #
#  planning
# --------------------------------------------------------------------------- #
def _align_up(v, a):
    return (v + a - 1) // a * a


def plan(sources):
    """Decide where every partition lands in the merged image.

    Returns (entries, total_sectors) where an entry is
    (src_img, src_lba, new_lba, length, name, is_system).
    """
    layouts = [(s, read_layout(s)) for s in sources]

    ref_img, ref_layout = layouts[0]
    ref_sig = system_signature(ref_layout)
    for img, lay in layouts[1:]:
        if system_signature(lay) != ref_sig:
            raise MergeError(
                "%s has different system partitions than %s -- merging them "
                "would give some games a system area they were not shipped with"
                % (img, ref_img))

    entries = []
    pos = 0
    for p, sysp in ref_layout:
        if not sysp:
            continue
        if p.start != pos:
            raise MergeError("unexpected gap before system partition %r" % p.id)
        entries.append((ref_img, p.start, p.start, p.length, p.id, True))
        pos = p.start + p.length

    seen = {}
    todo = []
    for img, lay in layouts:
        for p in game_parts(lay):
            if p.id in seen:
                raise MergeError(
                    "partition name collision: %r is in both %s and %s. Two "
                    "builds of the same game+version cannot share one disk."
                    % (p.id, seen[p.id], img))
            seen[p.id] = img
            todo.append((img, p))
    # Biggest first. Each partition must start at a multiple of its own length,
    # so a small one placed early forces a gap in front of the next big one
    # (1 GB of waste for these images). Sorting is stable, so a game's own
    # partitions keep their relative order.
    todo.sort(key=lambda ip: -ip[1].length)
    for img, p in todo:
        pos = _align_up(pos, p.length)
        entries.append((img, p.start, pos, p.length, p.id, False))
        pos += p.length
    return entries, pos


# --------------------------------------------------------------------------- #
#  writing
# --------------------------------------------------------------------------- #
def _patch_header(raw, new_start, nxt, prv):
    b = bytearray(raw[:HDR])
    struct.pack_into("<I", b, 0x40, new_start)      # start
    struct.pack_into("<I", b, 0x08, nxt)            # next
    struct.pack_into("<I", b, 0x0C, prv)            # prev
    struct.pack_into("<I", b, 0x00, 0)              # checksum excludes word 0
    ck = ps2hdd.ApaPartition.checksum_of(bytes(b))
    struct.pack_into("<I", b, 0x00, ck)
    return bytes(b)


def merge(sources, out_path, log=print, progress=None):
    entries, total = plan(sources)
    log("merged layout: %d partitions, %.2f GB" % (len(entries), total * SECTOR / 1e9))
    for img, src, dst, length, name, sysp in entries:
        moved = "" if src == dst else "  (moved from %d)" % src
        log("  %-18s @LBA %-9d %6.2f GB  %s%s"
            % (name, dst, length * SECTOR / 1e9,
               "system" if sysp else os.path.basename(str(img)), moved))

    if os.path.exists(out_path):
        raise MergeError("%s already exists -- refusing to overwrite" % out_path)

    done = 0
    grand = sum(e[3] for e in entries)
    with open(out_path, "wb") as out:
        out.truncate(total * SECTOR)
        for i, (img, src, dst, length, name, sysp) in enumerate(entries):
            nxt = entries[i + 1][2] if i + 1 < len(entries) else 0
            prv = entries[i - 1][2] if i > 0 else entries[-1][2]
            with open(str(img), "rb") as f:
                f.seek(src * SECTOR)
                hdr_raw = f.read(HDR)
                out.seek(dst * SECTOR)
                out.write(_patch_header(hdr_raw, dst, nxt, prv))
                # the rest of the partition is copied verbatim
                f.seek(src * SECTOR + HDR)
                out.seek(dst * SECTOR + HDR)
                left = length * SECTOR - HDR
                while left > 0:
                    buf = f.read(min(CHUNK, left))
                    if not buf:
                        break
                    out.write(buf)
                    left -= len(buf)
                    done += len(buf) // SECTOR
                    if progress:
                        progress(done, grand)
    return entries, total


# --------------------------------------------------------------------------- #
#  verification
# --------------------------------------------------------------------------- #
def _hash_tree(hdd, part):
    """{path: sha256} for every file in a PFS partition."""
    out = {}

    def walk(d):
        for e in hdd.pfs_listdir(part, d):
            name = e["name"] if isinstance(e, dict) else e
            if name in (".", ".."):
                continue
            p = (d.rstrip("/") + "/" + name) if d != "/" else "/" + name
            is_dir = isinstance(e, dict) and e.get("is_dir")
            if is_dir:
                walk(p)
            else:
                out[p] = hashlib.sha256(hdd.pfs_read(part, p)).hexdigest()
    walk("/")
    return out


def verify(sources, out_path, log=print):
    """Re-read every file from the merged image and compare to its source.

    This is what turns "PFS looks position-independent" into a checked fact for
    the partitions actually moved.
    """
    ok = True
    for img in sources:
        for p in game_parts(read_layout(img)):
            src_h = ps2hdd.Ps2Hdd(str(img))
            dst_h = ps2hdd.Ps2Hdd(str(out_path))
            try:
                a = _hash_tree(src_h, p.id)
                b = _hash_tree(dst_h, p.id)
            finally:
                src_h.close()
                dst_h.close()
            same = a == b
            ok = ok and same
            log("  %-18s %3d files  %s" % (p.id, len(a),
                                           "IDENTICAL" if same else "*** MISMATCH ***"))
            if not same:
                for k in sorted(set(a) | set(b)):
                    if a.get(k) != b.get(k):
                        log("      differs: %s" % k)
    return ok


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtCore import Qt, QThread, Signal, QObject
    from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
                                   QLabel, QPlainTextEdit, QFileDialog, QCheckBox,
                                   QTreeWidget, QTreeWidgetItem, QMessageBox,
                                   QProgressBar, QLineEdit)
except ImportError:  # CLI-only environment
    QDialog = None


class _MergeWorker(QObject):
    progress = Signal(int, str)
    line = Signal(str)
    done = Signal(bool)

    def __init__(self, sources, out, do_verify):
        super().__init__()
        self.sources = sources
        self.out = out
        self.do_verify = do_verify

    def run(self):
        try:
            def prog(done, grand):
                pct = int(done * 100 / grand) if grand else 100
                self.progress.emit(pct, "%.2f / %.2f GB"
                                   % (done * SECTOR / 1e9, grand * SECTOR / 1e9))
            merge(self.sources, self.out, log=self.line.emit, progress=prog)
            if self.do_verify:
                self.line.emit("")
                self.line.emit("verifying every file re-reads identically ...")
                if not verify(self.sources, self.out, log=self.line.emit):
                    self.line.emit("VERIFY FAILED -- do not use this image")
                    self.done.emit(False)
                    return
                self.line.emit("VERIFY OK")
            self.done.emit(True)
        except Exception as exc:
            self.line.emit("FAILED: %s" % exc)
            self.done.emit(False)


class ApaMergeDialog(QDialog):
    """Combine several game HDD images into one multi-game APA disk."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Merge PS2 HDD images (multi-game disk)")
        self.resize(860, 620)
        self._sources = []
        self._thread = None
        self._worker = None

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "Each game's memory card looks up its own partition by name, so one "
            "disk can hold them all and the card alone picks the game."))

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Source", "Partition", "Size", "Kind"])
        self.tree.setRootIsDecorated(False)
        lay.addWidget(self.tree, 1)

        row = QHBoxLayout()
        b_add = QPushButton("Add images…")
        b_add.clicked.connect(self._add)
        b_del = QPushButton("Remove selected")
        b_del.clicked.connect(self._remove)
        row.addWidget(b_add)
        row.addWidget(b_del)
        row.addStretch(1)
        self.lbl_plan = QLabel("")
        row.addWidget(self.lbl_plan)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Output:"))
        self.ed_out = QLineEdit()
        b_out = QPushButton("Browse…")
        b_out.clicked.connect(self._browse_out)
        row2.addWidget(self.ed_out, 1)
        row2.addWidget(b_out)
        lay.addLayout(row2)

        self.ck_verify = QCheckBox(
            "Verify after merging (re-reads every file and compares hashes -- "
            "slow, but it is what proves the moved partitions survived)")
        self.ck_verify.setChecked(True)
        lay.addWidget(self.ck_verify)

        self.bar = QProgressBar()
        self.bar.setVisible(False)
        lay.addWidget(self.bar)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)

        row3 = QHBoxLayout()
        self.btn_merge = QPushButton("Merge")
        self.btn_merge.clicked.connect(self._merge)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.reject)
        row3.addWidget(self.btn_merge)
        row3.addStretch(1)
        row3.addWidget(self.btn_close)
        lay.addLayout(row3)
        self._replan()

    def _say(self, s):
        self.log.appendPlainText(s)

    def _add(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add PS2 HDD images", "", "HDD image (*.img);;All files (*)")
        for p in paths:
            if p not in self._sources:
                self._sources.append(p)
        self._replan()

    def _remove(self):
        for it in self.tree.selectedItems():
            src = it.data(0, Qt.UserRole)
            if src in self._sources:
                self._sources.remove(src)
        self._replan()

    def _browse_out(self):
        p, _ = QFileDialog.getSaveFileName(
            self, "Merged image", "", "HDD image (*.img)")
        if p:
            self.ed_out.setText(p)

    def _replan(self):
        self.tree.clear()
        for s in self._sources:
            try:
                lay = read_layout(s)
            except Exception as exc:
                it = QTreeWidgetItem(self.tree,
                                     [os.path.basename(s), "", "", str(exc)[:60]])
                it.setData(0, Qt.UserRole, s)
                continue
            for p, sysp in lay:
                if sysp:
                    continue        # the system area is shared; not worth listing
                it = QTreeWidgetItem(self.tree, [os.path.basename(s), p.id,
                                                 "%.2f GB" % (p.length * SECTOR / 1e9),
                                                 "game"])
                it.setData(0, Qt.UserRole, s)
        for i in range(4):
            self.tree.resizeColumnToContents(i)
        if not self._sources:
            self.lbl_plan.setText("")
            self.btn_merge.setEnabled(False)
            return
        try:
            entries, total = plan(self._sources)
        except Exception as exc:
            # Not just MergeError: a source can vanish or be renamed between
            # being added and being planned, and an unreadable one must grey the
            # button out rather than take the dialog down.
            self.lbl_plan.setText("cannot merge: %s" % str(exc)[:70])
            self.btn_merge.setEnabled(False)
            return
        ngames = sum(1 for e in entries if not e[5])
        self.lbl_plan.setText("merged: %d game partitions, %.2f GB"
                              % (ngames, total * SECTOR / 1e9))
        self.btn_merge.setEnabled(True)

    def _merge(self):
        out = self.ed_out.text().strip()
        if not out:
            QMessageBox.warning(self, "Merge", "Choose an output path.")
            return
        if os.path.exists(out):
            QMessageBox.warning(self, "Merge",
                                "%s already exists. Choose a new file." % out)
            return
        try:
            _, total = plan(self._sources)
        except MergeError as exc:
            QMessageBox.critical(self, "Merge", str(exc))
            return
        need = total * SECTOR
        free = _free_space(out)
        if free is not None and free < need:
            QMessageBox.warning(
                self, "Not enough space",
                "The merged image needs %.2f GB but only %.2f GB is free on that "
                "drive." % (need / 1e9, free / 1e9))
            return
        self.btn_merge.setEnabled(False)
        self.bar.setVisible(True)
        self.bar.setValue(0)
        # Hold refs: a worker whose QThread outlives the local scope gets
        # destroyed mid-run ("QThread: Destroyed while thread is running").
        self._thread = QThread(self)
        self._worker = _MergeWorker(list(self._sources), out,
                                    self.ck_verify.isChecked())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.line.connect(self._say)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_done)
        self._worker.done.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def _on_progress(self, pct, text):
        self.bar.setValue(pct)
        self.bar.setFormat("%s  (%%p%%)" % text)

    def _on_done(self, ok):
        self.bar.setVisible(False)
        self.btn_merge.setEnabled(True)
        if ok:
            self._say("")
            self._say("Done. Point the game's .acgame `mediasrc` at this image "
                      "(or write it to the cabinet's HDD) and boot with each "
                      "game's own memory card.")
        else:
            QMessageBox.critical(self, "Merge", "Merge failed -- see the log.")

    def reject(self):
        if self._thread and self._thread.isRunning():
            QMessageBox.information(
                self, "Merge", "A merge is still running; let it finish.")
            return
        super().reject()


def _free_space(path):
    try:
        import shutil
        d = os.path.dirname(os.path.abspath(path)) or "."
        return shutil.disk_usage(d).free
    except Exception:
        return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--list":
        for img in argv[1:]:
            print("%s:" % img)
            for p, sysp in read_layout(img):
                print("  %-18s @%-9d %6.2f GB  %s"
                      % (p.id, p.start, p.length * SECTOR / 1e9,
                         "system" if sysp else "game"))
        return 0
    if "-o" not in argv:
        print("need -o <out.img>")
        return 2
    i = argv.index("-o")
    out = argv[i + 1]
    do_verify = "--verify" in argv
    srcs = [a for j, a in enumerate(argv)
            if j not in (i, i + 1) and not a.startswith("--")]
    if not srcs:
        print("need at least one source image")
        return 2
    merge(srcs, out)
    if do_verify:
        print("\nverifying every file re-reads identically from the merged image ...")
        if not verify(srcs, out):
            print("VERIFY FAILED")
            return 1
        print("VERIFY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

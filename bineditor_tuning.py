#!/usr/bin/env python3
"""
Binary editor for ``tuning.bin`` — Taiko no Tatsujin (SYSTEM256 / PS2-era arcade)
per-song / per-chart gameplay tuning table.

Reverse-engineered structure (little-endian)
--------------------------------------------
The file is one big region of fixed-size *chart records* followed by an ASCII
string pool of song ids.

* The pre-pool region [0, POOL) is tiled by exactly **180 song-player blocks**
  (90 songs x 2 players: 1P / 2P). Blocks alternate in size: 972 bytes for a
  1P block, 984 bytes for a 2P block (the final block is larger — 994 in the
  sample — to absorb the pool alignment slack).
* Each block begins with **4 chart records** (one per difficulty: easy / normal
  / hard / master-oni). A record is **108 bytes = 27 little-endian int32**.
  Empty difficulty slots are filled with -1 (0xFFFFFFFF).
* After the 4 records, each block has a *tail* (mostly 0xFF filler, with a few
  structured bytes) that is **kept verbatim** so the round-trip is byte-exact.
* ``record[0]`` of the very first block holds the global **song count (90)**;
  every other block stores -1 there.
* Within a record the signed value at int32 index 15 (byte +0x3C) is the marker
  that anchored the structure (a per-chart tuning delta); other indices are the
  scoring / gauge / timing tuning parameters (gogo, score init, score diff,
  0x10000 fixed-point ratios, etc.).
* The string pool at 0x2AFAC..EOF lists, per song: ``<id>``, ``music_<id>`` and
  the 8 chart-file stems ``<id>{1p,2p}_{e,n,h,m}``. It is kept raw; strings are
  surfaced read-only for context.

Editing model
-------------
The original bytes are retained whole. Only the 27 int32 fields of each chart
record are exposed as editable; on serialize each field is patched in place.
Anything untouched re-serialises identically, so ``serialize(parse(d)) == d``.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

FILENAME = "tuning.bin"

RECORD_INTS = 27                 # int32 per chart record
RECORD_SIZE = RECORD_INTS * 4    # 108 bytes
RECORDS_PER_BLOCK = 4            # 4 difficulty slots at the head of every block
NEG_MARKER_INDEX = 15            # int32 index of the small-negative anchor field
DIFF_NAMES = ["easy", "normal", "hard", "oni"]


@dataclass
class ChartRecord:
    """One 108-byte chart tuning record (27 int32)."""
    offset: int                  # absolute file offset of the record
    values: list                 # 27 signed int32


@dataclass
class Block:
    """One song-player block: 4 chart records + a raw tail."""
    index: int                   # 0..179
    offset: int                  # absolute file offset of the block
    size: int                    # total block size in bytes
    records: list                # list[ChartRecord] (len 4)
    tail_offset: int             # absolute offset where the raw tail begins
    tail: bytes                  # verbatim bytes after the 4 records

    @property
    def song_index(self) -> int:
        return self.index // 2

    @property
    def player(self) -> str:
        return "1P" if (self.index % 2 == 0) else "2P"


@dataclass
class PoolString:
    offset: int
    text: str


EXPECTED_BLOCKS = 180            # 90 songs x 2 players (1P/2P)
# Per-block byte sizes observed on the real sample: 972 (1P) / 984 (2P), with
# the final block carrying extra pool-alignment slack (994 in the sample).
EXPECTED_BLOCK_SIZES = {972, 984}
LAST_BLOCK_MIN_SIZE = 984        # final block >= a normal block (absorbs slack)


@dataclass
class TuningModel:
    raw: bytearray                       # full original file bytes
    pool_offset: int                     # start of the ASCII string pool
    song_count: int                      # global song count (record[0] of block0)
    blocks: list = field(default_factory=list)        # list[Block]
    strings: list = field(default_factory=list)       # list[PoolString]
    # True only when block discovery produced the expected, validated layout.
    # When False the editable grid is suppressed and the user is warned, rather
    # than being shown an empty/garbled table. serialize() is always byte-exact
    # because it patches the original raw buffer in place.
    structured: bool = False
    warning: str = ""                    # human-readable reason if unstructured


# --------------------------------------------------------------------------- #
# structure discovery helpers
# --------------------------------------------------------------------------- #
def _find_pool_offset(data: bytes) -> int:
    """Locate the start of the ASCII song-id pool.

    The pool is the trailing region; it begins at the first byte of the run of
    null-terminated printable ASCII strings that continues (with only NUL gaps)
    to EOF. Empirically this is the offset of the first ``music_`` group's id.
    We anchor on the *earliest* ``music_`` occurrence and walk back over the id
    that precedes it.
    """
    idx = data.find(b"music_")
    if idx < 0:
        return len(data)
    # walk back to the start of the preceding ascii id (the song id token)
    i = idx
    while i > 0 and 0x20 <= data[i - 1] < 0x7F:
        i -= 1
    return i


def _pool_is_printable_to_eof(data: bytes, pool_offset: int) -> bool:
    """Sanity-check that [pool_offset, EOF) is a plausible string pool: NUL
    separators plus printable/text bytes (ASCII >=0x20 or UTF-8 multibyte bytes
    >=0x80; the ids may be Japanese). Rejects ASCII control bytes other than NUL
    and tab, which a real binary record region would contain. This guards
    against ``music_`` matching inside the record region and mislocating the
    pool."""
    if pool_offset >= len(data):
        return False
    region = data[pool_offset:]
    if not region:
        return False
    for b in region:
        if b == 0x00 or b == 0x09:        # NUL separator / tab
            continue
        if 0x20 <= b <= 0xFF:             # printable ASCII or UTF-8 byte
            continue
        return False                       # ASCII control byte -> not a pool
    return True


def _scan_blocks(data: bytes, pool_offset: int):
    """Tile [0, pool_offset) into song-player blocks.

    Blocks are delimited by the small-negative marker (-300 < v < -1) that sits
    at int32 index 15 of each chart record. Four such markers (one per
    difficulty record) start every block; the gap to the next group of four
    marks the block boundary.
    """
    negs = []
    end = pool_offset - 3
    off = 0
    while off < end:
        v = struct.unpack_from("<i", data, off)[0]
        if -300 < v < -1:
            negs.append(off)
        off += 4
    if not negs:
        return []

    # cluster markers: a delta > 200 bytes starts a new block
    groups = []
    cur = [negs[0]]
    for k in range(1, len(negs)):
        if negs[k] - negs[k - 1] > 200:
            groups.append(cur)
            cur = [negs[k]]
        else:
            cur.append(negs[k])
    groups.append(cur)

    marker_byte = NEG_MARKER_INDEX * 4
    starts = [g[0] - marker_byte for g in groups]
    bounds = starts + [pool_offset]
    return starts, bounds


# --------------------------------------------------------------------------- #
# parse / serialize
# --------------------------------------------------------------------------- #
def parse(data: bytes) -> TuningModel:
    raw = bytearray(data)
    pool_offset = _find_pool_offset(data)

    model = TuningModel(raw=raw, pool_offset=pool_offset, song_count=0)

    # Validate the discovered pool offset before trusting the block scan.
    if not _pool_is_printable_to_eof(data, pool_offset):
        model.warning = (
            "could not locate a contiguous ASCII string pool; the record grid "
            "is hidden to avoid editing at wrong offsets. The file still saves "
            "byte-exact.")
        # strings extraction is best-effort even when unstructured
        model.strings = _extract_pool_strings(data, pool_offset)
        return model

    scan = _scan_blocks(data, pool_offset)
    if scan:
        starts, bounds = scan
        for bi, bstart in enumerate(starts):
            bend = bounds[bi + 1]
            records = []
            for r in range(RECORDS_PER_BLOCK):
                roff = bstart + r * RECORD_SIZE
                vals = list(struct.unpack_from("<%di" % RECORD_INTS, data, roff))
                records.append(ChartRecord(offset=roff, values=vals))
            tail_off = bstart + RECORDS_PER_BLOCK * RECORD_SIZE
            model.blocks.append(Block(
                index=bi, offset=bstart, size=bend - bstart,
                records=records, tail_offset=tail_off,
                tail=bytes(data[tail_off:bend]),
            ))
        if model.blocks:
            model.song_count = model.blocks[0].records[0].values[0]

    # ---- validate the discovered block layout ----
    model.structured = _validate_blocks(model)
    if not model.structured and not model.warning:
        model.warning = (
            f"chart-block discovery did not yield the expected "
            f"{EXPECTED_BLOCKS} blocks / song-block sizes; the record grid is "
            f"hidden to avoid presenting garbled data. The file still saves "
            f"byte-exact.")

    # strings (read-only context)
    model.strings = _extract_pool_strings(data, pool_offset)
    return model


def _validate_blocks(model: TuningModel) -> bool:
    """Return True only when the block layout matches the documented shape:
    exactly EXPECTED_BLOCKS contiguous blocks tiling [0, pool_offset) with each
    size in EXPECTED_BLOCK_SIZES."""
    blocks = model.blocks
    if len(blocks) != EXPECTED_BLOCKS:
        return False
    # blocks must tile contiguously from 0 to pool_offset
    if blocks[0].offset != 0:
        return False
    pos = 0
    last = len(blocks) - 1
    for i, b in enumerate(blocks):
        if b.offset != pos:
            return False
        # all but the final block must be a normal 1P/2P size; the final block
        # may be larger to absorb pool-alignment slack.
        if i == last:
            if b.size < LAST_BLOCK_MIN_SIZE:
                return False
        elif b.size not in EXPECTED_BLOCK_SIZES:
            return False
        # each block must hold 4 whole 108-byte records before its tail
        if b.size < RECORDS_PER_BLOCK * RECORD_SIZE:
            return False
        pos += b.size
    if pos != model.pool_offset:
        return False
    return True


def _extract_pool_strings(data: bytes, pool_offset: int):
    out = []
    n = len(data)
    i = pool_offset
    while i < n:
        b = data[i]
        if 0x20 <= b < 0x7F:
            j = i
            while j < n and 0x20 <= data[j] < 0x7F:
                j += 1
            if (j - i) >= 2:
                out.append(PoolString(offset=i, text=data[i:j].decode("ascii", "replace")))
            i = j
        else:
            i += 1
    return out


def serialize(model: TuningModel) -> bytes:
    """Patch the edited int32 fields back into a copy of the original bytes.

    Everything not represented as an editable field (block tails, the string
    pool, alignment slack) is preserved verbatim, guaranteeing a byte-exact
    round-trip when nothing was changed.
    """
    out = bytearray(model.raw)
    for blk in model.blocks:
        for rec in blk.records:
            struct.pack_into("<%di" % RECORD_INTS, out, rec.offset, *rec.values)
    return bytes(out)


# --------------------------------------------------------------------------- #
# editor dialog
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QTableWidget,
        QTableWidgetItem, QPushButton, QLabel, QHeaderView, QMessageBox,
        QLineEdit, QWidget,
    )

    class Editor(QDialog):
        """Modal editor. After exec(), ``result_bytes`` holds new bytes if saved."""

        def __init__(self, data: bytes, title: str = "", parent=None):
            super().__init__(parent)
            self.setWindowTitle(f"tuning.bin Editor — {title}" if title else "tuning.bin Editor")
            self.resize(1180, 720)
            self.model = parse(data)
            self.result_bytes: bytes | None = None
            self._build_ui()

        # -- ui ------------------------------------------------------------- #
        def _build_ui(self):
            lay = QVBoxLayout(self)

            m = self.model
            info = QLabel(
                f"songs: {m.song_count}   blocks: {len(m.blocks)} "
                f"(90 songs x 2 players)   record: {RECORD_INTS} int32   "
                f"pool @ 0x{m.pool_offset:X}   strings: {len(m.strings)}"
            )
            lay.addWidget(info)

            if not m.structured:
                warn = QLabel("⚠ " + (m.warning or "unrecognized layout"))
                warn.setWordWrap(True)
                warn.setStyleSheet("color:#c33;")
                lay.addWidget(warn)
                # read-only string pool view only
                self.tbl_str = QTableWidget(len(m.strings), 2)
                self.tbl_str.setHorizontalHeaderLabels(["offset", "text"])
                for r, s in enumerate(m.strings):
                    o = QTableWidgetItem(f"0x{s.offset:X}")
                    o.setFlags(o.flags() & ~Qt.ItemIsEditable)
                    t = QTableWidgetItem(s.text)
                    t.setFlags(t.flags() & ~Qt.ItemIsEditable)
                    self.tbl_str.setItem(r, 0, o)
                    self.tbl_str.setItem(r, 1, t)
                self.tbl_str.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
                lay.addWidget(self.tbl_str, 1)
                self.tbl = None  # no editable grid
                self._rows = []
                btns = QHBoxLayout(); btns.addStretch(1)
                b_save = QPushButton("Save (unchanged)"); b_save.clicked.connect(self._save)
                b_cancel = QPushButton("Cancel"); b_cancel.clicked.connect(self.reject)
                btns.addWidget(b_save); btns.addWidget(b_cancel)
                lay.addLayout(btns)
                return

            # search box for filtering the record table
            top = QHBoxLayout()
            top.addWidget(QLabel("filter:"))
            self.ed_filter = QLineEdit()
            self.ed_filter.setPlaceholderText("song index, player (1P/2P) or difficulty…")
            self.ed_filter.textChanged.connect(self._apply_filter)
            top.addWidget(self.ed_filter, 1)
            lay.addLayout(top)

            tabs = QTabWidget()

            # ---- records tab ----
            _field_lbl = {5: "★star", 17: "scoreInit"}
            cols = ["block", "song", "player", "difficulty"] + [
                _field_lbl.get(i, f"f{i:02d}") for i in range(RECORD_INTS)]
            rows = []
            for blk in self.model.blocks:
                for di, rec in enumerate(blk.records):
                    rows.append((blk, di, rec))
            self._rows = rows
            self.tbl = QTableWidget(len(rows), len(cols))
            self.tbl.setHorizontalHeaderLabels(cols)
            self.tbl.verticalHeader().setDefaultSectionSize(20)
            for r, (blk, di, rec) in enumerate(rows):
                meta = [str(blk.index), str(blk.song_index), blk.player, DIFF_NAMES[di]]
                for c, val in enumerate(meta):
                    it = QTableWidgetItem(val)
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    self.tbl.setItem(r, c, it)
                for c, val in enumerate(rec.values):
                    self.tbl.setItem(r, 4 + c, QTableWidgetItem(str(val)))
            self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
            tabs.addTab(self.tbl, f"Chart records ({len(rows)})")

            # ---- strings tab (read-only context) ----
            self.tbl_str = QTableWidget(len(self.model.strings), 2)
            self.tbl_str.setHorizontalHeaderLabels(["offset", "text"])
            for r, s in enumerate(self.model.strings):
                o = QTableWidgetItem(f"0x{s.offset:X}")
                o.setFlags(o.flags() & ~Qt.ItemIsEditable)
                t = QTableWidgetItem(s.text)
                t.setFlags(t.flags() & ~Qt.ItemIsEditable)
                self.tbl_str.setItem(r, 0, o)
                self.tbl_str.setItem(r, 1, t)
            self.tbl_str.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            tabs.addTab(self.tbl_str, f"Song-id pool ({len(self.model.strings)})")

            lay.addWidget(tabs, 1)

            # ---- buttons ----
            btns = QHBoxLayout()
            btns.addStretch(1)
            b_save = QPushButton("Save"); b_save.clicked.connect(self._save)
            b_cancel = QPushButton("Cancel"); b_cancel.clicked.connect(self.reject)
            btns.addWidget(b_save); btns.addWidget(b_cancel)
            lay.addLayout(btns)

        def _apply_filter(self, text: str):
            text = text.strip().lower()
            for r, (blk, di, rec) in enumerate(self._rows):
                hay = f"{blk.song_index} {blk.player.lower()} {DIFF_NAMES[di]} {blk.index}"
                self.tbl.setRowHidden(r, bool(text) and text not in hay)

        # -- save ----------------------------------------------------------- #
        def _save(self):
            if not self.model.structured or self.tbl is None:
                # Unstructured view: nothing editable; re-emit original bytes.
                self.result_bytes = serialize(self.model)
                self.accept()
                return
            try:
                for r, (blk, di, rec) in enumerate(self._rows):
                    for c in range(RECORD_INTS):
                        item = self.tbl.item(r, 4 + c)
                        if item is None:
                            continue
                        v = int(item.text().strip())
                        if not (-2147483648 <= v <= 4294967295):
                            raise ValueError(f"row {r} field f{c:02d}: {v} out of int32 range")
                        if v > 2147483647:
                            v -= 4294967296   # accept unsigned spelling
                        rec.values[c] = v
            except ValueError as exc:
                QMessageBox.critical(self, "Edit error", str(exc))
                return
            self.result_bytes = serialize(self.model)
            self.accept()

except ImportError:  # PySide6 not available — parse/serialize still usable
    Editor = None  # type: ignore


# --------------------------------------------------------------------------- #
# self-test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import os
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, FILENAME),
        r"C:\Users\User\AppData\Local\Temp\claude\D--\ee6b327c-e139-4924-9604-a691103f5eaa\scratchpad\bins\tuning.bin",
    ]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if path is None:
        print(f"FAIL: could not locate {FILENAME}")
        sys.exit(1)

    data = open(path, "rb").read()
    model = parse(data)
    out = serialize(model)
    ok = out == data

    n_records = sum(len(b.records) for b in model.blocks)
    print(
        f"tuning.bin: {len(data)} bytes | songs={model.song_count} | "
        f"blocks={len(model.blocks)} | chart_records={n_records} | "
        f"pool@0x{model.pool_offset:X} strings={len(model.strings)} | "
        f"round-trip {'PASS' if ok else 'FAIL'}"
    )
    sys.exit(0 if ok else 1)

#!/usr/bin/env python3
"""
Binary editor for ``enso_parts.bin`` — a Taiko no Tatsujin (SYSTEM256 / PS2-era
arcade) enso (gameplay) screen part/asset table.

Format (reverse-engineered empirically, little-endian):

  0x00  u32[16]   header
                    [0..7]  counts  : (n0, n1, n2, n3, n4, n5, n6, n7)
                    [8..14] offsets : start offset of each section
                    [15]    0       : reserved / always zero
  0x40  bytes      header tail (up to offsets[0]); contains count[0]=3 small
                   records that reference the string pool. Kept as raw bytes —
                   round-trip exact, not field-edited.

  Six record sections tile contiguously from offsets[0] to the string pool.
  Each section i is counts[i+1] fixed-size records:

    sec 0  off[0]  count=n1   rec=16 bytes   (4 x u32)
    sec 1  off[1]  count=n2   rec=36 bytes   (9 x u32; some fields are floats)
    sec 2  off[2]  count=n3   rec=12 bytes   (3 x u32)
    sec 3  off[3]  count=n4   rec=12 bytes   (3 x u32)
    sec 4  off[4]  count=n5   rec=32 bytes   (8 x u32; some fields are floats)
    sec 5  off[5]  count=n6   rec= 4 bytes   (1 x u32) -> STRING OFFSET TABLE:
                                              each entry is a byte offset into
                                              the string pool.

  String pool  off[6] .. EOF : a blob of null-terminated ASCII strings. The
  offset table (sec 5) and the header tail point into this pool. The pool holds
  more strings (~1244) than are referenced by the table (399); unreferenced
  strings are preserved verbatim.

Round-trip guarantee: ``serialize(parse(data)) == data`` byte-for-byte. Every
region that is not explicitly modeled as an editable field is preserved as raw
bytes, so nothing can be lost. When a pooled string is edited and changes
length, the pool is rebuilt and the offset table is fixed up so the references
stay valid (and unchanged edits reproduce the original bytes exactly).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

FILENAME = "enso_parts.bin"

HEADER_U32 = 16          # 16 leading u32 = 64 bytes
HEADER_SIZE = HEADER_U32 * 4

# section index -> record size in bytes (the 6 record sections, in file order)
SECTION_RECSIZE = [16, 36, 12, 12, 32, 4]
# which header count drives each section (counts[1..6])
SECTION_COUNT_IDX = [1, 2, 3, 4, 5, 6]
# section 5 (zero-based) is the string offset table
STR_TABLE_SECTION = 5


# --------------------------------------------------------------------------- #
#  Model
# --------------------------------------------------------------------------- #
@dataclass
class Section:
    index: int
    offset: int               # start offset in original file
    rec_size: int
    count: int
    # records: list of tuples of u32 ints (one tuple per record)
    records: list = field(default_factory=list)

    @property
    def n_fields(self) -> int:
        return self.rec_size // 4


@dataclass
class EnsoParts:
    raw: bytearray            # full original bytes (fallback / source of truth)
    header: list              # 16 u32 ints
    header_tail: bytes        # bytes[HEADER_SIZE : offsets[0]]
    sections: list            # list[Section]
    pool_offset: int          # absolute offset of string pool start
    pool_parts: list          # list[bytes] null-separated parts of the pool
    # Inter-region padding captured at parse time so it survives a rebuild.
    # section_gaps[i] = raw bytes between section i's end and the next region
    # start (section i+1 for i<5, or the pool for i==5). Normally all empty
    # because the sample tiles exactly, but preserved if a file has alignment.
    section_gaps: list = field(default_factory=list)   # list[bytes], len 6
    # When raw_fallback is not None the file did not match the modeled shape;
    # the whole file is re-emitted verbatim (still a byte-exact round-trip).
    raw_fallback: bytes | None = None
    # str_offsets: the parsed offset-table entries (== sections[5].records flat)

    # -- derived helpers --------------------------------------------------- #
    @property
    def counts(self):
        return self.header[0:8]

    @property
    def offsets(self):
        return self.header[8:15]

    def pool_blob(self) -> bytes:
        """Rebuild the raw pool bytes from the ordered parts (null-joined)."""
        return b"\x00".join(self.pool_parts)

    def part_start_offsets(self) -> list:
        """Byte offset (relative to pool start) where each pool part begins."""
        out = []
        pos = 0
        for p in self.pool_parts:
            out.append(pos)
            pos += len(p) + 1
        return out


# --------------------------------------------------------------------------- #
#  Parse
# --------------------------------------------------------------------------- #
def parse(data: bytes) -> EnsoParts:
    raw = bytearray(data)
    try:
        return _parse_strict(raw)
    except (ValueError, struct.error):
        # Malformed / unexpected shape: keep the whole file verbatim so the
        # round-trip is still byte-exact (mirrors lamp.bin's raw fallback).
        return EnsoParts(raw=raw, header=[], header_tail=b"", sections=[],
                         pool_offset=0, pool_parts=[], section_gaps=[],
                         raw_fallback=bytes(raw))


def _parse_strict(raw: bytearray) -> EnsoParts:
    n = len(raw)
    if n < HEADER_SIZE:
        raise ValueError("file too small for header")
    header = list(struct.unpack_from("<16I", raw, 0))
    offsets = header[8:15]
    counts = header[0:8]

    # validate offsets: monotonic-enough and within EOF
    if offsets[0] < HEADER_SIZE:
        raise ValueError("section 0 overlaps header")
    for i in range(7):
        if not (0 <= offsets[i] <= n):
            raise ValueError("offset out of range")

    header_tail = bytes(raw[HEADER_SIZE:offsets[0]])

    sections = []
    for i in range(6):
        start = offsets[i]
        rec = SECTION_RECSIZE[i]
        cnt = counts[SECTION_COUNT_IDX[i]]
        nf = rec // 4
        end = start + cnt * rec
        if start < HEADER_SIZE or end > n:
            raise ValueError("section %d overruns file" % i)
        recs = []
        for r in range(cnt):
            o = start + r * rec
            recs.append(list(struct.unpack_from("<%dI" % nf, raw, o)))
        sections.append(Section(index=i, offset=start, rec_size=rec,
                                 count=cnt, records=recs))

    pool_offset = offsets[6]
    if not (HEADER_SIZE <= pool_offset <= n):
        raise ValueError("pool offset out of range")

    # Capture inter-region padding so a pool rebuild preserves alignment. The
    # region after section i starts at offsets[i+1] (for i<5) or pool_offset.
    section_gaps = []
    for i in range(6):
        sec_end = sections[i].offset + sections[i].count * sections[i].rec_size
        next_start = offsets[i + 1] if i < 5 else pool_offset
        if next_start < sec_end:
            raise ValueError("section %d overlaps next region" % i)
        section_gaps.append(bytes(raw[sec_end:next_start]))

    pool_blob = bytes(raw[pool_offset:])
    pool_parts = pool_blob.split(b"\x00")

    model = EnsoParts(raw=raw, header=header, header_tail=header_tail,
                      sections=sections, pool_offset=pool_offset,
                      pool_parts=pool_parts, section_gaps=section_gaps)

    # Modeled spans must tile exactly to the pool and to EOF; otherwise fall
    # back to raw so we never silently drop bytes.
    if serialize(model) != bytes(raw):
        raise ValueError("round-trip self-check failed during parse")
    return model


# --------------------------------------------------------------------------- #
#  Serialize  (must satisfy serialize(parse(d)) == d)
# --------------------------------------------------------------------------- #
def serialize(model: EnsoParts) -> bytes:
    if model.raw_fallback is not None:
        return bytes(model.raw_fallback)

    out = bytearray()
    # header copy we can fix up with recomputed offsets
    header = list(model.header)

    # header tail (raw); section 0 starts right after it
    body = bytearray()
    body += model.header_tail
    off0 = HEADER_SIZE + len(model.header_tail)

    # record sections, in order, recomputing each section's start offset and
    # re-emitting any captured inter-region padding so layout is preserved.
    sec_starts = []
    for i, sec in enumerate(model.sections):
        sec_starts.append(HEADER_SIZE + len(body))
        nf = sec.n_fields
        for rec in sec.records:
            body += struct.pack("<%dI" % nf, *rec)
        # gap after this section (between it and the next region)
        if i < len(model.section_gaps):
            body += model.section_gaps[i]

    pool_start = HEADER_SIZE + len(body)

    # write recomputed offsets back into header words 8..14
    header[8:15] = sec_starts + [pool_start]

    out += struct.pack("<16I", *header)
    out += body
    # string pool (rebuilt from ordered parts)
    out += model.pool_blob()
    return bytes(out)


# --------------------------------------------------------------------------- #
#  Editing helpers (used by the GUI)
# --------------------------------------------------------------------------- #
def referenced_strings(model: EnsoParts) -> list:
    """Return list of (table_index, pool_part_index, text) for each entry in the
    string offset table (section 5). The pool part index lets us track a string
    across a pool rebuild even when its byte offset changes."""
    table = model.sections[STR_TABLE_SECTION].records  # list of [u32]
    starts = model.part_start_offsets()
    start_to_part = {s: i for i, s in enumerate(starts)}
    out = []
    for ti, rec in enumerate(table):
        off = rec[0]
        pi = start_to_part.get(off)
        if pi is None:
            # offset not at a part boundary (shouldn't happen) -> decode raw
            text = "<unaligned 0x%X>" % off
        else:
            # latin1 is a lossless 1:1 byte<->codepoint mapping, so decode and
            # re-encode are symmetric (no ascii/"replace" corruption).
            text = model.pool_parts[pi].decode("latin1")
        out.append((ti, pi, text))
    return out


def apply_string_edits(model: EnsoParts, edits: dict):
    """edits: {table_index: new_text}. Rewrites the affected pool parts, then
    rebuilds the offset table so every reference still points to the right
    string. Unreferenced pool strings are preserved. If a part is shared by
    multiple table entries they all follow it.

    Strategy: edit the pool part(s) in place (changing their bytes/length),
    then recompute part start offsets and write them back into the table.
    """
    if not edits:
        return
    table = model.sections[STR_TABLE_SECTION].records
    # map table index -> pool part index (from current state)
    ref = {ti: pi for (ti, pi, _t) in referenced_strings(model)}

    # apply text changes to pool parts
    for ti, new_text in edits.items():
        pi = ref.get(ti)
        if pi is None:
            continue
        # latin1 round-trips the decode in referenced_strings; characters
        # outside 0x00..0xFF cannot be stored in this byte pool.
        try:
            model.pool_parts[pi] = new_text.encode("latin1")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "string %d contains a character that cannot be stored in this "
                "latin1 pool: %s" % (ti, exc)) from exc

    # recompute offsets and rewrite the whole table from part indices
    starts = model.part_start_offsets()
    for ti, pi in ref.items():
        table[ti][0] = starts[pi]


def set_record_field(model: EnsoParts, section_index: int, row: int,
                     col: int, value: int):
    model.sections[section_index].records[row][col] = value & 0xFFFFFFFF


# --------------------------------------------------------------------------- #
#  GUI editor
# --------------------------------------------------------------------------- #
def _float_view(u: int) -> str:
    """Best-effort float interpretation of a u32 for display hints."""
    f = struct.unpack("<f", struct.pack("<I", u))[0]
    if f == 0.0:
        return ""
    if abs(f) >= 1e-4 and abs(f) < 1e9:
        return "%g" % f
    return ""


try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QTableWidget,
        QTableWidgetItem, QPushButton, QLabel, QHeaderView, QMessageBox, QWidget,
    )
    _HAVE_QT = True
except Exception:  # pragma: no cover - Qt optional for headless import
    _HAVE_QT = False
    QDialog = object  # type: ignore


class Editor(QDialog):
    """Modal editor for enso_parts.bin.

    After exec(): if the user saved, ``self.result_bytes`` holds the new
    re-serialized bytes; if cancelled it is ``None``.
    """

    def __init__(self, data: bytes, title: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"enso_parts.bin editor — {title}" if title
                            else "enso_parts.bin editor")
        self.resize(1000, 680)
        self.result_bytes: bytes | None = None
        self._orig = bytes(data)
        self.model = parse(data)

        self._build_ui()

    # -- ui ---------------------------------------------------------------- #
    def _build_ui(self):
        lay = QVBoxLayout(self)

        if self.model.raw_fallback is not None:
            lay.addWidget(QLabel(
                "enso_parts.bin did not match the known structure; showing a "
                "read-only hex view. Saving re-writes the original bytes "
                "unchanged."))
            blob = self.model.raw_fallback
            hexv = QTableWidget(0, 1)
            hexv.setHorizontalHeaderLabels(["raw bytes (read-only)"])
            hexv.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            rows = [blob[i:i + 16].hex(" ") for i in range(0, len(blob), 16)]
            hexv.setRowCount(len(rows))
            for r, h in enumerate(rows):
                it = QTableWidgetItem(h)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                hexv.setItem(r, 0, it)
            lay.addWidget(hexv, 1)
            btns = QHBoxLayout(); btns.addStretch(1)
            b_save = QPushButton("Save"); b_save.clicked.connect(self._save)
            b_cancel = QPushButton("Cancel"); b_cancel.clicked.connect(self.reject)
            btns.addWidget(b_save); btns.addWidget(b_cancel)
            lay.addLayout(btns)
            return

        c = self.model.counts
        o = self.model.offsets
        info = QLabel(
            "counts=%s  offsets=%s  pool@0x%X  size=%d" %
            (tuple(c), tuple(o), self.model.pool_offset, len(self._orig))
        )
        info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(info)

        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)

        # ---- record-section tabs ----
        self._sec_tables = []
        sec_names = ["s0 (16B)", "s1 (36B)", "s2 (12B)", "s3 (12B)",
                     "s4 (32B)", "strtab (4B)"]
        for si, sec in enumerate(self.model.sections):
            tbl = QTableWidget(sec.count, sec.n_fields)
            tbl.setHorizontalHeaderLabels(
                ["u%d" % k for k in range(sec.n_fields)])
            tbl.setVerticalHeaderLabels([str(r) for r in range(sec.count)])
            for r, rec in enumerate(sec.records):
                for k, v in enumerate(rec):
                    it = QTableWidgetItem(str(v))
                    fv = _float_view(v)
                    if fv:
                        it.setToolTip("float=%s" % fv)
                    tbl.setItem(r, k, it)
            tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            self.tabs.addTab(tbl, "%s x%d" % (sec_names[si], sec.count))
            self._sec_tables.append(tbl)

        # ---- referenced-strings tab (editable) ----
        refs = referenced_strings(self.model)
        self.tbl_str = QTableWidget(len(refs), 3)
        self.tbl_str.setHorizontalHeaderLabels(
            ["idx", "pool part", "text (editable)"])
        for r, (ti, pi, text) in enumerate(refs):
            a = QTableWidgetItem(str(ti)); a.setFlags(a.flags() & ~Qt.ItemIsEditable)
            b = QTableWidgetItem(str(pi)); b.setFlags(b.flags() & ~Qt.ItemIsEditable)
            self.tbl_str.setItem(r, 0, a)
            self.tbl_str.setItem(r, 1, b)
            self.tbl_str.setItem(r, 2, QTableWidgetItem(text))
        self.tbl_str.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.tabs.addTab(self.tbl_str, "strings (%d)" % len(refs))
        self._ref_rows = refs  # keep table index mapping

        # ---- all-pool-strings tab (read-only context) ----
        parts = [p for p in self.model.pool_parts]
        self.tbl_pool = QTableWidget(len(parts), 2)
        self.tbl_pool.setHorizontalHeaderLabels(["part #", "text"])
        for r, p in enumerate(parts):
            a = QTableWidgetItem(str(r)); a.setFlags(a.flags() & ~Qt.ItemIsEditable)
            t = QTableWidgetItem(p.decode("latin1"))
            t.setFlags(t.flags() & ~Qt.ItemIsEditable)
            self.tbl_pool.setItem(r, 0, a)
            self.tbl_pool.setItem(r, 1, t)
        self.tbl_pool.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tabs.addTab(self.tbl_pool, "pool (%d)" % len(parts))

        # ---- buttons ----
        btns = QHBoxLayout()
        btns.addStretch(1)
        b_save = QPushButton("Save"); b_save.clicked.connect(self._save)
        b_cancel = QPushButton("Cancel"); b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_save); btns.addWidget(b_cancel)
        lay.addLayout(btns)

    # -- save -------------------------------------------------------------- #
    def _save(self):
        if self.model.raw_fallback is not None:
            # Unstructured view: re-emit the original bytes unchanged.
            self.result_bytes = serialize(self.model)
            self.accept()
            return
        try:
            # 1) write back record fields
            for si, tbl in enumerate(self._sec_tables):
                sec = self.model.sections[si]
                for r in range(sec.count):
                    for k in range(sec.n_fields):
                        item = tbl.item(r, k)
                        if item is None:
                            continue
                        txt = item.text().strip()
                        val = _parse_int(txt)
                        sec.records[r][k] = val & 0xFFFFFFFF

            # 2) collect string edits (only those that changed)
            edits = {}
            for r, (ti, pi, old_text) in enumerate(self._ref_rows):
                item = self.tbl_str.item(r, 2)
                if item is None:
                    continue
                new = item.text()
                if new != old_text:
                    edits[ti] = new
            # NOTE: string edits below also rewrite the offset table (section 5),
            # which overwrites the raw field edits made to that table above. That
            # is intended: when strings move, the table MUST follow them.
            if edits:
                apply_string_edits(self.model, edits)

            new_bytes = serialize(self.model)
        except (ValueError, struct.error) as exc:
            QMessageBox.critical(self, "Save error", str(exc))
            return

        # Refresh the read-only pool tab so it reflects any string edits, and
        # re-sync the strings tab's stored "old" mapping for repeat saves.
        if edits:
            self.tbl_pool.setRowCount(len(self.model.pool_parts))
            for r, p in enumerate(self.model.pool_parts):
                a = QTableWidgetItem(str(r)); a.setFlags(a.flags() & ~Qt.ItemIsEditable)
                t = QTableWidgetItem(p.decode("latin1")); t.setFlags(t.flags() & ~Qt.ItemIsEditable)
                self.tbl_pool.setItem(r, 0, a)
                self.tbl_pool.setItem(r, 1, t)
            self._ref_rows = referenced_strings(self.model)

        self.result_bytes = new_bytes
        self.accept()


def _parse_int(txt: str) -> int:
    txt = txt.strip()
    if not txt:
        return 0
    if txt.lower().startswith("0x"):
        return int(txt, 16)
    return int(txt)


# --------------------------------------------------------------------------- #
#  Self test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import os

    target = os.environ.get(
        "ENSO_PARTS_BIN",
        r"C:\Users\User\AppData\Local\Temp\claude\D--"
        r"\ee6b327c-e139-4924-9604-a691103f5eaa\scratchpad\bins\enso_parts.bin",
    )
    with open(target, "rb") as fh:
        data = fh.read()

    model = parse(data)
    out = serialize(model)
    ok = out == data

    c = model.counts
    nref = len(model.sections[STR_TABLE_SECTION].records)
    print(
        "enso_parts.bin: %d bytes | header counts=%s | 6 record sections "
        "(rec sizes %s) | string table=%d entries | pool=%d parts @0x%X | "
        "round-trip %s"
        % (len(data), tuple(c), SECTION_RECSIZE, nref,
           len(model.pool_parts), model.pool_offset,
           "OK" if ok else "MISMATCH")
    )

    # extra: prove an idempotent string-edit round-trips too (no-op edit)
    m2 = parse(data)
    apply_string_edits(m2, {0: referenced_strings(m2)[0][2]})  # rewrite same text
    ok2 = serialize(m2) == data

    print("PASS" if (ok and ok2) else "FAIL")

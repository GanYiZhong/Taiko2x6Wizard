#!/usr/bin/env python3
"""
musicinfo.bin editor for Taiko no Tatsujin (SYSTEM256 / PS2-era arcade).

This file is the per-song *visual /演出* (performance) configuration database that
the game cross-references by song. It is NOT the chart-difficulty table; instead
it binds songs to fever-gallery backgrounds, dance backgrounds (odori), dance
characters and score / crown thresholds.

------------------------------------------------------------------------------
Empirically reverse-engineered layout (all little-endian):

  0x00  HEADER  (13 x u32)
        [0..6]  seven *count* fields  : c0..c6
        [7..12] six   *offset* fields : o0..o5  (absolute file offsets)

        c0 = 90  -> SEC0 record count (main per-entry table)
        c1 = 10  -> SEC1 entry count
        c2 = 7   -> SEC2 entry count
        c3 = 80  -> SEC3 entry count (background / gallery name pointers)
        c4 = 33  -> SEC4 entry count (named asset groups)
        c5 = 5   -> SEC5 entry count (category groups)
        c6 = 52  -> auxiliary count (referenced by group records)

  SEC0  0x34 .. o0   : c0 records, 84 bytes each (21 x i32)
        Per-entry fields. col17..col20 are score / crown thresholds:
            col17 == col19 == base score
            col18 == base + 3000
            col20 == base + 5000
        col0/col1 ascending running indices, col8..col11 small ints.
        Kept as raw int columns and edited as-is (no pool refs here).

  SEC1  o0 .. o1     : c1 (=10) x u32   running index table
  SEC2  o1 .. o2     : c2 (=7)  x (u32,u32) pairs (group -> count mapping)
  SEC3  o2 .. o3     : c3 (=80) x u32   POOL-RELATIVE string pointers
                       (FI_BA_GYARARI_* fever galleries, ODORIHAIKEI_* dance bgs)
  SEC4  o3 .. o4     : c4 (=33) x (ptr,u32,u32)  named asset groups
                       ptr is POOL-RELATIVE; e.g. AIDORU, MOJIPITTAN, DORIRA_...
  SEC5  o4 .. o5     : c5 (=5)  x (ptr,u32,u32)  category groups
                       NIGIYAKASHIENSHUTSU, ODORIHAIKEI, DAIBG, ODORIKYARA,
                       CHIBIKYARA  (ptr POOL-RELATIVE)
  POOL  o5 .. EOF    : null-terminated ASCII string pool

------------------------------------------------------------------------------
Round-trip strategy
-------------------
parse() keeps the *entire* file as raw bytes plus a structured view. All section
records are decoded to ints and the pool to (offset -> string). serialize()
rebuilds the file from the structured view. If nothing is edited the bytes are
byte-identical to the input (verified by the self-test).

String edits: the pool is fully rebuilt from a deduplicated ordered set of the
referenced strings, and every pool-relative pointer (SEC3/SEC4/SEC5) is
recomputed. To preserve byte-exactness on an *unedited* file we keep the
original pool bytes and the original (offset -> ptr) mapping and only rebuild
when a string actually changes. This guarantees serialize(parse(d)) == d.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

FILENAME = "musicinfo.bin"

HEADER_FMT = "<13I"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 52 = 0x34
SEC0_RECSZ = 84          # 21 x i32
SEC0_NCOLS = 21

# --- Taiko 14+ variant ------------------------------------------------------ #
# Header is 17 x u32 (68 B): 8 count fields (c0..c7) + the SEC0 start offset
# (== 68) + 8 absolute section-end offsets (o0..o7). SEC0 records are 104 B
# (26 x i32) and there are two extra u32 index sections (SEC6, SEC7) between
# SEC5 and the string pool. Everything else (SEC1..SEC5, pool) matches T8.
HEADER_FMT_T14 = "<17I"
HEADER_SIZE_T14 = struct.calcsize(HEADER_FMT_T14)  # 68 = 0x44
SEC0_RECSZ_T14 = 104     # 26 x i32
SEC0_NCOLS_T14 = 26


# --------------------------------------------------------------------------- #
#  Model
# --------------------------------------------------------------------------- #
@dataclass
class Model:
    raw: bytes                       # original bytes (fallback / verification)
    counts: list                     # 7 header count fields
    offs: list                       # 6 header offset fields
    sec0: list                       # list[list[int]]  (90 x 21)
    sec1: list                       # list[int]
    sec2: list                       # list[(int,int)]
    sec3_ptrs: list                  # list[int]  pool-relative
    sec4: list                       # list[(ptr,int,int)]
    sec5: list                       # list[(ptr,int,int)]
    pool: bytes                      # original pool bytes
    # Captured inter-section gaps (raw bytes between each fixed section's
    # computed end and the next header offset). Normally empty for the sample
    # but preserved so any alignment padding survives a save. Indexed
    # 'sec0'..'sec5' for the gap that follows that section.
    gaps: dict = field(default_factory=dict)
    # string view: every pool-relative pointer resolved to its string,
    # together with its source so edits can be written back.
    strings: list = field(default_factory=list)  # list[StrRef]
    dirty_strings: bool = False
    # When set the file did not match the modeled shape; serialize re-emits the
    # original bytes verbatim (still a byte-exact round-trip).
    raw_fallback: bytes | None = None
    # --- format variant --------------------------------------------------- #
    # "t8"  = Taiko 8 arcade: 13-u32 header (7 counts + 6 offsets), SEC0 84 B,
    #         sections SEC0..SEC5.
    # "t14" = Taiko 14+: 17-u32 header (8 counts + SEC0-start + 8 offsets),
    #         SEC0 104 B, two extra u32 sections SEC6/SEC7 before the pool.
    variant: str = "t8"
    sec0_recsz: int = SEC0_RECSZ
    sec0_ncols: int = SEC0_NCOLS
    header_size: int = HEADER_SIZE
    sec6: list = field(default_factory=list)   # t14 only: list[int] (u32)
    sec7: list = field(default_factory=list)   # t14 only: list[int] (u32)


@dataclass
class StrRef:
    section: str          # 'sec3' | 'sec4' | 'sec5'
    index: int            # record index within section
    pool_off: int         # original pool-relative offset
    text: str             # current text


# --------------------------------------------------------------------------- #
#  Pool helpers
# --------------------------------------------------------------------------- #
def _read_cstr(pool: bytes, off: int) -> str:
    end = pool.find(b"\x00", off)
    if end < 0:
        end = len(pool)
    # latin1 is a lossless 1:1 byte<->codepoint mapping so decode/encode are
    # symmetric; this avoids the ascii/"replace" -> "?" corruption on save.
    return pool[off:end].decode("latin1")


# --------------------------------------------------------------------------- #
#  parse
# --------------------------------------------------------------------------- #
def parse(data: bytes) -> Model:
    raw = bytes(data)
    # Try the Taiko 8 layout first, then the Taiko 14+ layout. Each strict parser
    # validates its own header arithmetic and self-checks the round-trip, so a
    # file of the wrong variant raises and we fall through to the next.
    for fn in (_parse_strict, _parse_strict_t14):
        try:
            return fn(raw)
        except (ValueError, struct.error):
            continue
    # Unexpected shape: keep the whole file verbatim so the round-trip is still
    # byte-exact, rather than throwing out of Editor.__init__.
    return Model(
        raw=raw, counts=[], offs=[], sec0=[], sec1=[], sec2=[],
        sec3_ptrs=[], sec4=[], sec5=[], pool=b"",
        raw_fallback=raw,
    )


def _parse_strict(raw: bytes) -> Model:
    if len(raw) < HEADER_SIZE:
        raise ValueError("file too small for musicinfo header")

    hdr = struct.unpack_from(HEADER_FMT, raw, 0)
    counts = list(hdr[:7])
    offs = list(hdr[7:13])

    c0 = counts[0]
    o0, o1, o2, o3, o4, o5 = offs
    n = len(raw)

    # validate header offsets: monotonic non-decreasing and within EOF
    prev = HEADER_SIZE
    for o in offs:
        if not (prev <= o <= n):
            raise ValueError("header offset out of range / not monotonic")
        prev = o

    # ---- SEC0 : main table -------------------------------------------------
    sec0 = []
    p = HEADER_SIZE
    if c0 * SEC0_RECSZ != o0 - HEADER_SIZE:
        raise ValueError("SEC0 size does not match o0")
    for _ in range(c0):
        rec = list(struct.unpack_from("<%di" % SEC0_NCOLS, raw, p))
        sec0.append(rec)
        p += SEC0_RECSZ
    # p now equals o0 (asserted by the size check above)

    def _check_multiple(length, elem, label):
        if length % elem != 0:
            raise ValueError("%s length %d not a multiple of %d" %
                             (label, length, elem))

    # ---- SEC1 : u32 index table -------------------------------------------
    sec1_len = o1 - o0
    _check_multiple(sec1_len, 4, "SEC1")
    sec1 = list(struct.unpack_from("<%dI" % (sec1_len // 4), raw, o0)) if sec1_len > 0 else []

    # ---- SEC2 : (u32,u32) pairs -------------------------------------------
    sec2_len = o2 - o1
    _check_multiple(sec2_len, 8, "SEC2")
    npairs = sec2_len // 8
    sec2 = [tuple(struct.unpack_from("<2I", raw, o1 + i * 8)) for i in range(npairs)]

    # ---- SEC3 : pool-relative string pointers -----------------------------
    sec3_len = o3 - o2
    _check_multiple(sec3_len, 4, "SEC3")
    sec3_ptrs = list(struct.unpack_from("<%dI" % (sec3_len // 4), raw, o2)) if sec3_len > 0 else []

    # ---- SEC4 : (ptr,u32,u32) named groups --------------------------------
    sec4_len = o4 - o3
    _check_multiple(sec4_len, 12, "SEC4")
    n4 = sec4_len // 12
    sec4 = [tuple(struct.unpack_from("<3I", raw, o3 + i * 12)) for i in range(n4)]

    # ---- SEC5 : (ptr,u32,u32) category groups -----------------------------
    sec5_len = o5 - o4
    _check_multiple(sec5_len, 12, "SEC5")
    n5 = sec5_len // 12
    sec5 = [tuple(struct.unpack_from("<3I", raw, o4 + i * 12)) for i in range(n5)]

    # ---- POOL --------------------------------------------------------------
    pool = raw[o5:n]

    # Capture any inter-section padding so the rebuild preserves it. With the
    # clean-multiple checks above each computed section end equals the next
    # offset, so these are normally empty; kept for robustness.
    gaps = {
        "sec0": raw[HEADER_SIZE + len(sec0) * SEC0_RECSZ:o0],
        "sec1": raw[o0 + len(sec1) * 4:o1],
        "sec2": raw[o1 + len(sec2) * 8:o2],
        "sec3": raw[o2 + len(sec3_ptrs) * 4:o3],
        "sec4": raw[o3 + len(sec4) * 12:o4],
        "sec5": raw[o4 + len(sec5) * 12:o5],
    }

    model = Model(
        raw=raw, counts=counts, offs=offs,
        sec0=sec0, sec1=sec1, sec2=sec2,
        sec3_ptrs=list(sec3_ptrs), sec4=sec4, sec5=sec5, pool=pool,
        gaps=gaps,
    )

    # ---- resolve string references ----------------------------------------
    strings: list[StrRef] = []
    for i, ptr in enumerate(sec3_ptrs):
        strings.append(StrRef("sec3", i, ptr, _read_cstr(pool, ptr)))
    for i, (ptr, _a, _b) in enumerate(sec4):
        strings.append(StrRef("sec4", i, ptr, _read_cstr(pool, ptr)))
    for i, (ptr, _a, _b) in enumerate(sec5):
        strings.append(StrRef("sec5", i, ptr, _read_cstr(pool, ptr)))
    model.strings = strings

    # Modeled spans must tile exactly back to the original bytes.
    if serialize(model) != raw:
        raise ValueError("round-trip self-check failed during parse")
    return model


def _parse_strict_t14(raw: bytes) -> Model:
    """Parse the Taiko 14+ musicinfo layout (17-u32 header, 104-B SEC0 records,
    plus the extra SEC6/SEC7 u32 sections)."""
    if len(raw) < HEADER_SIZE_T14:
        raise ValueError("file too small for T14 musicinfo header")

    hdr = struct.unpack_from(HEADER_FMT_T14, raw, 0)
    counts = list(hdr[0:8])          # c0..c7 (one per section SEC0..SEC7)
    sec0_start = hdr[8]              # == HEADER_SIZE_T14 (68)
    offs = list(hdr[9:17])          # o0..o7 : absolute ends of SEC0..SEC7
    c0, c1, c2, c3, c4, c5, c6, c7 = counts
    o0, o1, o2, o3, o4, o5, o6, o7 = offs
    n = len(raw)

    if sec0_start != HEADER_SIZE_T14:
        raise ValueError("T14 SEC0 start is not 68")
    # offsets monotonic, starting at SEC0's end, within EOF
    prev = sec0_start
    for o in offs:
        if not (prev <= o <= n):
            raise ValueError("T14 header offset out of range / not monotonic")
        prev = o
    if c0 * SEC0_RECSZ_T14 != o0 - sec0_start:
        raise ValueError("T14 SEC0 size does not match o0")

    def _check_multiple(length, elem, label):
        if length % elem != 0:
            raise ValueError("%s length %d not a multiple of %d" % (label, length, elem))

    # SEC0 : 104-byte records (26 i32)
    sec0 = []
    p = sec0_start
    for _ in range(c0):
        sec0.append(list(struct.unpack_from("<%di" % SEC0_NCOLS_T14, raw, p)))
        p += SEC0_RECSZ_T14
    # SEC1 : u32 index table
    _check_multiple(o1 - o0, 4, "SEC1")
    sec1 = list(struct.unpack_from("<%dI" % ((o1 - o0) // 4), raw, o0)) if o1 > o0 else []
    # SEC2 : (u32,u32) pairs
    _check_multiple(o2 - o1, 8, "SEC2")
    sec2 = [tuple(struct.unpack_from("<2I", raw, o1 + i * 8)) for i in range((o2 - o1) // 8)]
    # SEC3 : pool-relative string pointers
    _check_multiple(o3 - o2, 4, "SEC3")
    sec3_ptrs = list(struct.unpack_from("<%dI" % ((o3 - o2) // 4), raw, o2)) if o3 > o2 else []
    # SEC4 : (ptr,u32,u32) named groups
    _check_multiple(o4 - o3, 12, "SEC4")
    sec4 = [tuple(struct.unpack_from("<3I", raw, o3 + i * 12)) for i in range((o4 - o3) // 12)]
    # SEC5 : (ptr,u32,u32) category groups
    _check_multiple(o5 - o4, 12, "SEC5")
    sec5 = [tuple(struct.unpack_from("<3I", raw, o4 + i * 12)) for i in range((o5 - o4) // 12)]
    # SEC6 / SEC7 : u32 index tables (T14 only)
    _check_multiple(o6 - o5, 4, "SEC6")
    sec6 = list(struct.unpack_from("<%dI" % ((o6 - o5) // 4), raw, o5)) if o6 > o5 else []
    _check_multiple(o7 - o6, 4, "SEC7")
    sec7 = list(struct.unpack_from("<%dI" % ((o7 - o6) // 4), raw, o6)) if o7 > o6 else []
    # POOL
    pool = raw[o7:n]

    gaps = {
        "sec0": raw[sec0_start + len(sec0) * SEC0_RECSZ_T14:o0],
        "sec1": raw[o0 + len(sec1) * 4:o1],
        "sec2": raw[o1 + len(sec2) * 8:o2],
        "sec3": raw[o2 + len(sec3_ptrs) * 4:o3],
        "sec4": raw[o3 + len(sec4) * 12:o4],
        "sec5": raw[o4 + len(sec5) * 12:o5],
        "sec6": raw[o5 + len(sec6) * 4:o6],
        "sec7": raw[o6 + len(sec7) * 4:o7],
    }

    model = Model(
        raw=raw, counts=counts, offs=offs,
        sec0=sec0, sec1=sec1, sec2=sec2,
        sec3_ptrs=list(sec3_ptrs), sec4=sec4, sec5=sec5, pool=pool,
        gaps=gaps,
        variant="t14", sec0_recsz=SEC0_RECSZ_T14, sec0_ncols=SEC0_NCOLS_T14,
        header_size=HEADER_SIZE_T14, sec6=sec6, sec7=sec7,
    )

    strings: list[StrRef] = []
    for i, ptr in enumerate(sec3_ptrs):
        strings.append(StrRef("sec3", i, ptr, _read_cstr(pool, ptr)))
    for i, (ptr, _a, _b) in enumerate(sec4):
        strings.append(StrRef("sec4", i, ptr, _read_cstr(pool, ptr)))
    for i, (ptr, _a, _b) in enumerate(sec5):
        strings.append(StrRef("sec5", i, ptr, _read_cstr(pool, ptr)))
    model.strings = strings

    if serialize(model) != raw:
        raise ValueError("T14 round-trip self-check failed during parse")
    return model


# --------------------------------------------------------------------------- #
#  serialize
# --------------------------------------------------------------------------- #
def serialize(model: Model) -> bytes:
    m = model
    if m.raw_fallback is not None:
        return bytes(m.raw_fallback)

    g = m.gaps

    # 1) Decide the pool + per-StrRef pool offsets.
    if not m.dirty_strings:
        # untouched: reuse the exact original pool and original pointers.
        pool = m.pool
        sec3_ptrs = list(m.sec3_ptrs)
        sec4 = list(m.sec4)
        sec5 = list(m.sec5)
    else:
        # Rebuild pool from the (possibly edited) string view, preserving the
        # original layout order so identical content still reproduces exactly.
        # Build an ordered, deduplicated list of (orig_off, text) keyed by the
        # original offset so multiple refs to the same original string share a
        # slot when their text is still equal.
        #
        # Strategy: emit strings in ascending original pool offset order; for
        # each distinct original offset emit its (current) text once. Refs that
        # shared an original offset keep sharing iff their texts are still equal;
        # otherwise the changed ref gets its own appended slot.
        by_off: dict[int, list[StrRef]] = {}
        for s in m.strings:
            by_off.setdefault(s.pool_off, []).append(s)

        new_pool = bytearray()
        ref_to_newoff: dict[int, int] = {}   # id(StrRef) -> new offset

        for off in sorted(by_off):
            group = by_off[off]
            # group refs by their current text
            texts: dict[str, list[StrRef]] = {}
            order = []
            for s in group:
                if s.text not in texts:
                    texts[s.text] = []
                    order.append(s.text)
                texts[s.text].append(s)
            for t in order:
                slot = len(new_pool)
                try:
                    encoded = t.encode("latin1")
                except UnicodeEncodeError as exc:
                    raise ValueError(
                        "string %r contains a character that cannot be stored "
                        "in this latin1 pool: %s" % (t, exc)) from exc
                new_pool += encoded + b"\x00"
                for s in texts[t]:
                    ref_to_newoff[id(s)] = slot

        pool = bytes(new_pool)

        # Re-derive section pointer arrays from the StrRefs.
        sec3_ptrs = list(m.sec3_ptrs)
        sec4 = list(m.sec4)
        sec5 = list(m.sec5)
        for s in m.strings:
            no = ref_to_newoff[id(s)]
            if s.section == "sec3":
                sec3_ptrs[s.index] = no
            elif s.section == "sec4":
                ptr, a, b = sec4[s.index]
                sec4[s.index] = (no, a, b)
            elif s.section == "sec5":
                ptr, a, b = sec5[s.index]
                sec5[s.index] = (no, a, b)

    # 2) Serialize sections, re-emitting any captured inter-section gap after
    #    each section (normally empty). Everything before the pool keeps its
    #    original size; only the pool length can change on a string edit.
    body = bytearray()
    recsz = m.sec0_recsz          # 84 (t8) / 104 (t14)
    ncols = m.sec0_ncols          # 21 (t8) / 26 (t14)

    # SEC0
    for rec in m.sec0:
        body += struct.pack("<%di" % ncols, *rec)
    body += g.get("sec0", b"")
    # SEC1
    if m.sec1:
        body += struct.pack("<%dI" % len(m.sec1), *m.sec1)
    body += g.get("sec1", b"")
    # SEC2
    for a, b in m.sec2:
        body += struct.pack("<2I", a, b)
    body += g.get("sec2", b"")
    # SEC3
    if sec3_ptrs:
        body += struct.pack("<%dI" % len(sec3_ptrs), *sec3_ptrs)
    body += g.get("sec3", b"")
    # SEC4
    for ptr, a, b in sec4:
        body += struct.pack("<3I", ptr, a, b)
    body += g.get("sec4", b"")
    # SEC5
    for ptr, a, b in sec5:
        body += struct.pack("<3I", ptr, a, b)
    body += g.get("sec5", b"")

    if m.variant == "t14":
        # SEC6 / SEC7 : extra u32 index tables, then the 17-u32 header.
        if m.sec6:
            body += struct.pack("<%dI" % len(m.sec6), *m.sec6)
        body += g.get("sec6", b"")
        if m.sec7:
            body += struct.pack("<%dI" % len(m.sec7), *m.sec7)
        body += g.get("sec7", b"")

        hs = HEADER_SIZE_T14
        o0 = hs + len(m.sec0) * recsz + len(g.get("sec0", b""))
        o1 = o0 + len(m.sec1) * 4 + len(g.get("sec1", b""))
        o2 = o1 + len(m.sec2) * 8 + len(g.get("sec2", b""))
        o3 = o2 + len(sec3_ptrs) * 4 + len(g.get("sec3", b""))
        o4 = o3 + len(sec4) * 12 + len(g.get("sec4", b""))
        o5 = o4 + len(sec5) * 12 + len(g.get("sec5", b""))
        o6 = o5 + len(m.sec6) * 4 + len(g.get("sec6", b""))
        o7 = o6 + len(m.sec7) * 4 + len(g.get("sec7", b""))
        header = struct.pack(HEADER_FMT_T14, *m.counts, hs,
                             o0, o1, o2, o3, o4, o5, o6, o7)
        return header + bytes(body) + pool

    # 3) Recompute header offsets from actual emitted sizes (including any
    #    preserved gaps). Offsets are absolute file offsets. (Taiko 8 layout.)
    o0 = HEADER_SIZE + len(m.sec0) * recsz + len(g.get("sec0", b""))
    o1 = o0 + len(m.sec1) * 4 + len(g.get("sec1", b""))
    o2 = o1 + len(m.sec2) * 8 + len(g.get("sec2", b""))
    o3 = o2 + len(sec3_ptrs) * 4 + len(g.get("sec3", b""))
    o4 = o3 + len(sec4) * 12 + len(g.get("sec4", b""))
    o5 = o4 + len(sec5) * 12 + len(g.get("sec5", b""))

    header = struct.pack(
        HEADER_FMT,
        *m.counts,
        o0, o1, o2, o3, o4, o5,
    )

    out = header + bytes(body) + pool
    return out


# --------------------------------------------------------------------------- #
#  Editor dialog
# --------------------------------------------------------------------------- #
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


_SEC0_HEADERS = [f"f{i}" for i in range(SEC0_NCOLS)]
# semantic hints for the columns identified by cross-bin analysis
_SEC0_HEADERS[2] = "genre"
_SEC0_HEADERS[3] = "flag1"
_SEC0_HEADERS[4] = "flag2"
_SEC0_HEADERS[7] = "score"
_SEC0_HEADERS[11] = "sortIdx"
_SEC0_HEADERS[17] = "score(a)"
_SEC0_HEADERS[18] = "score+3k"
_SEC0_HEADERS[19] = "score(b)"
_SEC0_HEADERS[20] = "score+5k"


class Editor(QDialog):
    """Modal editor. After exec(), result_bytes holds new bytes if saved."""

    def __init__(self, data: bytes, title: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"musicinfo.bin — {title}" if title else "musicinfo.bin")
        self.resize(1100, 680)
        self.model = parse(data)
        self._orig = bytes(data)
        self.result_bytes: bytes | None = None
        self._build_ui()

    # -- UI ------------------------------------------------------------------
    def _build_ui(self):
        lay = QVBoxLayout(self)

        m = self.model
        if m.raw_fallback is not None:
            lay.addWidget(QLabel(
                "musicinfo.bin did not match the known structure; showing a "
                "read-only hex view. Saving re-writes the original bytes "
                "unchanged."))
            blob = m.raw_fallback
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

        info = QLabel(
            f"counts={m.counts}   offsets={m.offs}   "
            f"songs={len(m.sec0)}  strings={len(m.strings)}  pool={len(m.pool)}B"
        )
        info.setStyleSheet("color:#aaa;font-family:Consolas;")
        lay.addWidget(info)

        tabs = QTabWidget()
        lay.addWidget(tabs, 1)

        # ---- main records (SEC0) ----
        self.tbl0 = QTableWidget(len(m.sec0), SEC0_NCOLS)
        self.tbl0.setHorizontalHeaderLabels(_SEC0_HEADERS)
        self.tbl0.setVerticalHeaderLabels([str(i) for i in range(len(m.sec0))])
        for r, rec in enumerate(m.sec0):
            for c, v in enumerate(rec):
                self.tbl0.setItem(r, c, QTableWidgetItem(str(v)))
        self.tbl0.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        tabs.addTab(self.tbl0, f"Main records ({len(m.sec0)}×{SEC0_NCOLS})")

        # ---- strings (SEC3/4/5 pool refs) ----
        self.tbl_str = QTableWidget(len(m.strings), 4)
        self.tbl_str.setHorizontalHeaderLabels(["section", "idx", "pool_off", "text (editable)"])
        for r, s in enumerate(m.strings):
            for c, v in enumerate((s.section, str(s.index), str(s.pool_off))):
                it = QTableWidgetItem(v)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.tbl_str.setItem(r, c, it)
            self.tbl_str.setItem(r, 3, QTableWidgetItem(s.text))
        self.tbl_str.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        tabs.addTab(self.tbl_str, f"Strings ({len(m.strings)})")

        # ---- group tables (SEC4 / SEC5) shown as info via the string tab ----
        # (group index/count columns are editable in the raw int tab below)
        self.tbl_grp = QTableWidget(len(m.sec4) + len(m.sec5), 4)
        self.tbl_grp.setHorizontalHeaderLabels(["section", "name", "start", "count"])
        row = 0
        for i, (ptr, a, b) in enumerate(m.sec4):
            name = next((s.text for s in m.strings if s.section == "sec4" and s.index == i), "")
            self._grp_row(row, "sec4", name, a, b); row += 1
        for i, (ptr, a, b) in enumerate(m.sec5):
            name = next((s.text for s in m.strings if s.section == "sec5" and s.index == i), "")
            self._grp_row(row, "sec5", name, a, b); row += 1
        self.tbl_grp.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        tabs.addTab(self.tbl_grp, f"Groups ({len(m.sec4)+len(m.sec5)})")

        # buttons
        btns = QHBoxLayout()
        btns.addStretch(1)
        b_save = QPushButton("Save"); b_save.clicked.connect(self._save)
        b_cancel = QPushButton("Cancel"); b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_save); btns.addWidget(b_cancel)
        lay.addLayout(btns)

    def _grp_row(self, row, section, name, a, b):
        for c, v in enumerate((section, name, str(a), str(b))):
            it = QTableWidgetItem(v)
            if c in (0,):  # section read-only; name/start/count editable
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.tbl_grp.setItem(row, c, it)

    # -- save ----------------------------------------------------------------
    def _save(self):
        m = self.model
        if m.raw_fallback is not None:
            self.result_bytes = serialize(m)
            self.accept()
            return
        try:
            # main records
            for r in range(self.tbl0.rowCount()):
                for c in range(SEC0_NCOLS):
                    m.sec0[r][c] = int(self.tbl0.item(r, c).text())

            # strings
            dirty = False
            for r, s in enumerate(m.strings):
                new = self.tbl_str.item(r, 3).text()
                if new != s.text:
                    s.text = new
                    dirty = True

            # group start/count + names (mirror into sec4/sec5 and strings)
            row = 0
            for i in range(len(m.sec4)):
                name = self.tbl_grp.item(row, 1).text()
                a = int(self.tbl_grp.item(row, 2).text())
                b = int(self.tbl_grp.item(row, 3).text())
                ptr, _a, _b = m.sec4[i]
                if (a, b) != (_a, _b):
                    m.sec4[i] = (ptr, a, b)
                sref = next((s for s in m.strings if s.section == "sec4" and s.index == i), None)
                if sref and sref.text != name:
                    sref.text = name; dirty = True
                row += 1
            for i in range(len(m.sec5)):
                name = self.tbl_grp.item(row, 1).text()
                a = int(self.tbl_grp.item(row, 2).text())
                b = int(self.tbl_grp.item(row, 3).text())
                ptr, _a, _b = m.sec5[i]
                if (a, b) != (_a, _b):
                    m.sec5[i] = (ptr, a, b)
                sref = next((s for s in m.strings if s.section == "sec5" and s.index == i), None)
                if sref and sref.text != name:
                    sref.text = name; dirty = True
                row += 1

            if dirty:
                m.dirty_strings = True

            out = serialize(m)
        except (ValueError, struct.error) as exc:
            QMessageBox.critical(self, "Edit error", str(exc))
            return

        self.result_bytes = out
        self.accept()


# --------------------------------------------------------------------------- #
#  self-test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import os, sys

    path = os.environ.get(
        "MUSICINFO_BIN",
        r"C:\Users\User\AppData\Local\Temp\claude\D--\ee6b327c-e139-4924-9604-a691103f5eaa\scratchpad\bins\musicinfo.bin",
    )
    with open(path, "rb") as f:
        data = f.read()

    model = parse(data)
    out = serialize(model)
    ok = (out == data)

    # also exercise an edit -> re-serialize integrity (length-changing string)
    edit_ok = True
    try:
        m2 = parse(data)
        if m2.strings:
            m2.strings[0].text = m2.strings[0].text + "_X"   # longer string
            m2.dirty_strings = True
            o2 = serialize(m2)
            m3 = parse(o2)
            edit_ok = (m3.strings[0].text == m2.strings[0].text)
            # and an unedited reparse of o2 must round-trip too
            edit_ok = edit_ok and (serialize(m3) == o2)
    except Exception as exc:
        edit_ok = False
        print("edit-test exception:", exc)

    nsongs = len(model.sec0)
    nstr = len(model.strings)
    print(
        f"musicinfo.bin: {len(data)}B, header counts={model.counts}, "
        f"{nsongs} main records (84B/21cols), {nstr} pooled strings, "
        f"score-threshold cols=17..20; edit-roundtrip={'ok' if edit_ok else 'FAIL'} | "
        f"{'PASS' if ok and edit_ok else 'FAIL'}"
    )
    sys.exit(0 if (ok and edit_ok) else 1)

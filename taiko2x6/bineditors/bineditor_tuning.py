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

import re
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
    # every byte must be NUL, tab or >= 0x20: deleting those leaves any
    # ASCII control byte, which marks a record region rather than a pool
    return not region.translate(None, _POOL_OK_BYTES)


_POOL_OK_BYTES = bytes([0x00, 0x09]) + bytes(range(0x20, 0x100))


# --------------------------------------------------------------------------- #
# Taiko 7 variant (read-only)
# --------------------------------------------------------------------------- #
# Taiko 7's chart record is 26 int32: the T8 record without its index-9 field,
# so the small-negative anchor sits at index 14. The star rating stays at [5].
# Blocks are fixed-size and start at offset 0 (record[0] = song count):
# 1P = 936 bytes, 2P = 948, i.e. 1884 per song; the last block absorbs the
# pool's alignment slack. Tiled directly - the marker scan used for T8 breaks
# on songs whose easy slot is empty (all -1, no marker).
T7_RECORD_INTS = 26
T7_MARKER_INDEX = 14
T7_BLOCK_1P = 936
T7_BLOCK_2P = 948


def is_t7_layout(data: bytes) -> bool:
    """True for a Taiko 7 tuning.bin (26-int records, anchor at index 14)."""
    if len(data) < 4 * (NEG_MARKER_INDEX + 1):
        return False
    w = struct.unpack_from("<%di" % (NEG_MARKER_INDEX + 1), data, 0)
    return -300 < w[T7_MARKER_INDEX] < -1 and not (-300 < w[NEG_MARKER_INDEX] < -1)


def t7_stars(data: bytes) -> list:
    """[[easy, normal, hard, oni], ...] per song, in block (song) order.

    Taiko 7 has no oni on some songs: that slot is -1 there, as in T8.
    """
    if not is_t7_layout(data):
        raise ValueError("not a Taiko 7 tuning.bin")
    song = T7_BLOCK_1P + T7_BLOCK_2P
    pool = _find_pool_offset(data)
    count = struct.unpack_from("<i", data, 0)[0]
    if count <= 0 or not (0 <= pool - count * song < T7_RECORD_INTS * 4):
        raise ValueError(
            "Taiko 7 tuning.bin: %d songs x %d bytes does not reach the string "
            "pool at 0x%x" % (count, song, pool))
    rs = T7_RECORD_INTS * 4
    out = []
    for k in range(count):                           # 1P block; 2P mirrors it
        base = k * song
        out.append([struct.unpack_from("<i", data, base + r * rs + 5 * 4)[0]
                    for r in range(RECORDS_PER_BLOCK)])
    return out


# Taiko 14 (and Omnimix images built on it): u32 song count, then one fixed
# 2028-byte record per song, sorted by id, then the string pool. Each chart's
# star rating follows its chart-file offset: columns 4 / 32 / 60 / 88 for
# easy / normal / hard / oni (column 0 = id offset into the pool).
T14_REC = 2028
T14_STAR_COLS = (4, 32, 60, 88)


def is_t14_layout(data: bytes) -> bool:
    """True for a Taiko 14 tuning.bin (count + 2028-byte records + pool)."""
    if len(data) < 4 + T14_REC:
        return False
    cnt = struct.unpack_from("<I", data, 0)[0]
    ps = 4 + cnt * T14_REC
    if not 0 < cnt < 10000 or ps >= len(data):
        return False
    e = data.find(b"\0", ps)
    return e > ps and data[ps:e].isascii() and data[e + 1:e + 7] == b"music_"


def t14_stars(data: bytes) -> dict:
    """{song id: [easy, normal, hard, oni]} for a Taiko 14 tuning.bin."""
    if not is_t14_layout(data):
        raise ValueError("not a Taiko 14 tuning.bin")
    cnt = struct.unpack_from("<I", data, 0)[0]
    ps = 4 + cnt * T14_REC
    out = {}
    for i in range(cnt):
        rec = 4 + i * T14_REC
        off = ps + struct.unpack_from("<i", data, rec)[0]
        sid = data[off:data.find(b"\0", off)].decode("ascii", "replace")
        out[sid] = [struct.unpack_from("<i", data, rec + 4 * c)[0] for c in T14_STAR_COLS]
    return out


T8_SONG = 972 + 984                  # 1P + 2P block (T8..T13, T11T/T12T)


def t8_stars(data: bytes) -> dict:
    """{song id: [easy, normal, hard, oni]} for a T8-layout tuning.bin.

    Song k's 1P block starts at k * 1956; record 0 holds the song's pool
    offsets (col 1 = id, col 2 = music_<id>) and every record has its star at
    int index 5. Pool offsets are relative to a base just before the pool
    (song 0's bare id sits there), recovered from song 0's music_ offset.
    """
    if len(data) < T8_SONG:
        raise ValueError("tuning.bin too small")
    pool = _find_pool_offset(data)
    count = struct.unpack_from("<i", data, 0)[0]
    if count <= 0 or not 0 <= pool - count * T8_SONG < RECORD_SIZE:
        raise ValueError("not a T8-layout tuning.bin")
    base = pool - struct.unpack_from("<i", data, 8)[0]
    out = {}
    for k in range(count):
        blk = k * T8_SONG
        off = base + struct.unpack_from("<i", data, blk + 4)[0]
        if not 0 <= off < len(data):
            continue
        sid = data[off:data.find(bytes(1), off)].decode("ascii", "replace")
        out[sid] = [struct.unpack_from("<i", data, blk + r * RECORD_SIZE + 4 * STAR_INDEX_T8)[0]
                    for r in range(RECORDS_PER_BLOCK)]
    return out


STAR_INDEX_T8 = 5


def set_t14_stars(rec: bytearray, stars) -> None:
    """Write [easy, normal, hard, oni] into one 2028-byte T14 record."""
    for c, v in zip(T14_STAR_COLS, stars):
        struct.pack_into("<i", rec, 4 * c, int(v))


def set_t14_stars_by_id(data: bytes, stars: dict) -> bytes:
    """Copy of a Taiko 14 tuning.bin with {song id: [e, n, h, o]} applied."""
    if not is_t14_layout(data):
        raise ValueError("not a Taiko 14 tuning.bin")
    out = bytearray(data)
    cnt = struct.unpack_from("<I", data, 0)[0]
    ps = 4 + cnt * T14_REC
    for i in range(cnt):
        rec = 4 + i * T14_REC
        off = ps + struct.unpack_from("<i", data, rec)[0]
        sid = data[off:data.find(b"\0", off)].decode("ascii", "replace")
        if sid in stars:
            view = memoryview(out)[rec:rec + T14_REC]
            set_t14_stars(view, stars[sid])
    return bytes(out)


# --------------------------------------------------------------------------- #
#  per-chart gameplay fields (soul gauge, judgement, scoring)
#
#  A T14 record holds one 28-int block per difficulty d starting at column
#  3 + 28*d: [chart-file offset, star, then the gameplay fields]. Columns are
#  given below relative to 28*d, so the star is column 4 and the soul-gauge
#  gains per GOOD / OK / BAD are columns 12 / 13 / 14. Column 30 (the slot
#  just before the next chart's offset) exists only in T14.
#
#  The same values sit in the older layouts, shifted: a T8..T13 record's ints
#  6..26 are T14 columns 5..25. Taiko 7 has no scoring pair (its ints 7 and 8
#  are 0) and one int less: its int 6 is column 5 and its ints 9..25 are
#  columns 9..25. Song ids that both games ship carry identical values.
# --------------------------------------------------------------------------- #
T14_DIFF_STRIDE = 28
T14_PLAY_COLS = tuple(range(5, 31))
_T8_TO_T14 = {6 + j: 5 + j for j in range(21)}
_T7_TO_T14 = {6: 5, **{i: i for i in range(9, 26)}}


def _t14_rows(data: bytes):
    cnt = struct.unpack_from("<I", data, 0)[0]
    ps = 4 + cnt * T14_REC
    for i in range(cnt):
        rec = 4 + i * T14_REC
        off = ps + struct.unpack_from("<i", data, rec)[0]
        yield data[off:data.find(b"\0", off)].decode("ascii", "replace"), rec


def t14_play_fields(data: bytes) -> dict:
    """{song id: [ {col: value} per difficulty, or None ]} (T14 layout)."""
    if not is_t14_layout(data):
        raise ValueError("not a Taiko 14 tuning.bin")
    out = {}
    for sid, rec in _t14_rows(data):
        diffs = []
        for d in range(4):
            star = struct.unpack_from("<i", data, rec + 4 * (4 + T14_DIFF_STRIDE * d))[0]
            diffs.append(None if star <= 0 else {
                c: struct.unpack_from("<i", data, rec + 4 * (c + T14_DIFF_STRIDE * d))[0]
                for c in T14_PLAY_COLS})
        out[sid] = diffs
    return out


def _mapped(rec_ints, mapping) -> dict | None:
    if rec_ints[5] <= 0:                 # no chart on this difficulty
        return None
    return {col: rec_ints[i] for i, col in mapping.items()}


def t8_play_fields(data: bytes) -> dict:
    """{song id: [ {T14 col: value} per difficulty, or None ]} (T8..T13)."""
    pool = _find_pool_offset(data)
    count = struct.unpack_from("<i", data, 0)[0]
    if count <= 0 or not 0 <= pool - count * T8_SONG < RECORD_SIZE:
        raise ValueError("not a T8-layout tuning.bin")
    base = pool - struct.unpack_from("<i", data, 8)[0]
    out = {}
    for k in range(count):
        blk = k * T8_SONG
        off = base + struct.unpack_from("<i", data, blk + 4)[0]
        if not 0 <= off < len(data):
            continue
        sid = data[off:data.find(bytes(1), off)].decode("ascii", "replace")
        out[sid] = [_mapped(struct.unpack_from("<27i", data, blk + r * RECORD_SIZE),
                            _T8_TO_T14) for r in range(RECORDS_PER_BLOCK)]
    return out


def t7_play_fields(data: bytes) -> list:
    """[[ {T14 col: value} per difficulty, or None ], ...] in block (song)
    order, like t7_stars (Taiko 7 has no scoring pair: those columns are left
    out, so the target keeps its own)."""
    if not is_t7_layout(data):
        raise ValueError("not a Taiko 7 tuning.bin")
    song = T7_BLOCK_1P + T7_BLOCK_2P
    rs = T7_RECORD_INTS * 4
    count = struct.unpack_from("<i", data, 0)[0]
    return [[_mapped(struct.unpack_from("<26i", data, k * song + r * rs), _T7_TO_T14)
             for r in range(RECORDS_PER_BLOCK)] for k in range(count)]


def set_t14_play_fields_by_id(data: bytes, fields: dict) -> bytes:
    """Copy of a Taiko 14 tuning.bin with {song id: [ {col: value} or None
    per difficulty ]} written. A difficulty is only written when the target
    has a chart there (star > 0); the star itself is never touched."""
    if not is_t14_layout(data):
        raise ValueError("not a Taiko 14 tuning.bin")
    out = bytearray(data)
    for sid, rec in _t14_rows(data):
        diffs = fields.get(sid)
        if not diffs:
            continue
        for d, cols in enumerate(diffs[:4]):
            star = struct.unpack_from("<i", out, rec + 4 * (4 + T14_DIFF_STRIDE * d))[0]
            if not cols or star <= 0:
                continue
            for c, v in cols.items():
                if c == 4 or not 5 <= c <= 30:
                    continue
                struct.pack_into("<i", out, rec + 4 * (c + T14_DIFF_STRIDE * d), int(v))
    return bytes(out)


# Soul-gauge gains of a chart nobody shipped values for. On every stock TK14
# chart GOOD * hits is a constant per (difficulty, stars) to within 1%, OK is
# a fixed share of GOOD and BAD a fixed multiple, so 769 of its 770
# unbranched charts are reproduced to within 1 by these tables (measured on
# the stock TK14 .0000 archive; hits = don/ka notes of the chart).
_GAUGE_K = {
    0: {1: 16684, 2: 15770, 3: 15785, 4: 13645, 5: 13629},
    1: {1: 15200, 2: 15222, 3: 14400, 4: 14208, 5: 13328, 6: 13315, 7: 13320},
    2: {1: 12800, 2: 12988, 3: 13776, 4: 14475, 5: 14794, 6: 14562, 7: 14536,
        8: 14632},
    3: {1: 14000, 2: 14134, 3: 14099, 4: 14124, 5: 14134, 6: 14105, 7: 14130,
        8: 14292, 9: 13088, 10: 13123},
}
_GAUGE_BAD = {
    0: {s: 0.5 for s in range(1, 11)},
    1: {1: 0.5, 2: 0.5, 3: 0.5, 4: 0.75, **{s: 1.0 for s in range(5, 11)}},
    2: {1: 0.75, 2: 0.75, 3: 1.0, 4: 1.167, **{s: 1.25 for s in range(5, 11)}},
    3: {**{s: 1.6 for s in range(1, 8)}, 8: 2.0, 9: 2.0, 10: 2.0},
}


def estimate_gauge(diff: int, stars: int, hits: int) -> tuple:
    """(GOOD, OK, BAD) soul-gauge gains for difficulty 0..3 (easy..oni)."""
    ks = _GAUGE_K[diff]
    s = min(max(int(stars), min(ks)), max(ks))
    good = max(1, int(ks[s] / max(1, int(hits))))
    ok = int(good * (0.5 if diff == 3 else 0.75))
    bad = -int(good * _GAUGE_BAD[diff][min(max(int(stars), 1), 10)])
    return good, ok, bad


def _scan_blocks(data: bytes, pool_offset: int, marker_index: int = NEG_MARKER_INDEX):
    """Tile [0, pool_offset) into song-player blocks.

    Blocks are delimited by the small-negative marker (-300 < v < -1) that sits
    at int32 index 15 of each chart record. Four such markers (one per
    difficulty record) start every block; the gap to the next group of four
    marks the block boundary.
    """
    # one int32 per 4 bytes of [0, pool_offset): same offsets as the old
    # byte-stepping loop, found in one vectorised pass
    n = max(0, pool_offset) // 4
    try:
        import numpy as np
        v = np.frombuffer(data, dtype="<i4", count=n)
        negs = (np.flatnonzero((v > -300) & (v < -1)) * 4).tolist()
    except ImportError:
        negs = [4 * i for i, (v,) in enumerate(struct.iter_unpack("<i", data[:4 * n]))
                if -300 < v < -1]
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

    marker_byte = marker_index * 4
    # A marker cluster whose first marker sits before index 15 (offset < 60) would
    # imply a negative block start — that's a spurious match in a header/preamble
    # (seen on Taiko 7's tuning), not a real block. Drop it. T8's first real block
    # starts at offset 0 with its marker at 60, so genuine blocks are never dropped.
    starts = [g[0] - marker_byte for g in groups if g[0] - marker_byte >= 0]
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


_POOL_STRING_RE = re.compile(rb"[ -~]{2,}")


def _extract_pool_strings(data: bytes, pool_offset: int):
    return [PoolString(offset=m.start(), text=m.group().decode("ascii", "replace"))
            for m in _POOL_STRING_RE.finditer(data, pool_offset)]


def pool_strings(data: bytes):
    """parse(data).strings without the block scan (the strings do not depend
    on it): the cheap way to list a tuning.bin's ids and titles."""
    return _extract_pool_strings(data, _find_pool_offset(data))


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
    from taiko2x6.app.i18n import tr
    from taiko2x6.app.dialog_polish import polish
    from taiko2x6.bineditors.table_tools import (TableTools, ToolBar, parse_int,
                                                 remember_size)

    # read-only item flags, computed once (per-item flags() & ~Editable is slow)
    _RO = QTableWidgetItem().flags() & ~Qt.ItemIsEditable

    class Editor(QDialog):
        """Modal editor. After exec(), ``result_bytes`` holds new bytes if saved."""

        NO_DEFAULT_REASON = 'table editor: Enter edits cells and the filter box, so Save is an explicit click'

        def __init__(self, data: bytes, title: str = "", parent=None):
            super().__init__(parent)
            self.setWindowTitle(tr("{f} Editor — {t}").format(f="tuning.bin", t=title)
                if title else tr("{f} Editor").format(f="tuning.bin"))
            remember_size(self, "tuning", (1180, 720))
            self.model = parse(data)
            self.result_bytes: bytes | None = None
            self._build_ui()
            polish(self)

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
                warn.setStyleSheet("color:palette(bright-text);")
                lay.addWidget(warn)
                # read-only string pool view only
                self.tbl_str = QTableWidget(len(m.strings), 2)
                self.tbl_str.setHorizontalHeaderLabels([tr("offset"), tr("text")])
                for r, s in enumerate(m.strings):
                    o = QTableWidgetItem(f"0x{s.offset:X}")
                    o.setFlags(_RO)
                    t = QTableWidgetItem(s.text)
                    t.setFlags(_RO)
                    self.tbl_str.setItem(r, 0, o)
                    self.tbl_str.setItem(r, 1, t)
                self.tbl_str.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
                lay.addWidget(self.tbl_str, 1)
                self.tbl = None  # no editable grid
                self._rows = []
                btns = QHBoxLayout(); btns.addStretch(1)
                b_save = QPushButton(tr("Save (unchanged)")); b_save.clicked.connect(self._save)
                b_cancel = QPushButton(tr("Cancel")); b_cancel.clicked.connect(self.reject)
                btns.addWidget(b_save); btns.addWidget(b_cancel)
                lay.addLayout(btns)
                return

            bar_slot = QVBoxLayout()      # filled once the tables exist
            lay.addLayout(bar_slot)

            tabs = QTabWidget()

            # ---- records tab ----
            _field_lbl = {5: "\u2605star", 17: "scoreInit"}
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
                    it.setFlags(_RO)
                    self.tbl.setItem(r, c, it)
                for c, val in enumerate(rec.values):
                    self.tbl.setItem(r, 4 + c, QTableWidgetItem(str(val)))
            self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
            for c, tip in ((0, "Block number (song x 2 players)"),
                           (1, "Song number (block // 2)"),
                           (2, "1P or 2P"), (3, "Difficulty of this record"),
                           (4 + 5, "Star rating"),
                           (4 + 17, "Initial score value")):
                self.tbl.horizontalHeaderItem(c).setToolTip(tr(tip))
            tabs.addTab(self.tbl, tr("Chart records ({n})").format(n=len(rows)))

            # ---- strings tab (read-only context) ----
            self.tbl_str = QTableWidget(len(self.model.strings), 2)
            self.tbl_str.setHorizontalHeaderLabels([tr("offset"), tr("text")])
            for r, s in enumerate(self.model.strings):
                o = QTableWidgetItem(f"0x{s.offset:X}")
                o.setFlags(_RO)
                t = QTableWidgetItem(s.text)
                t.setFlags(_RO)
                self.tbl_str.setItem(r, 0, o)
                self.tbl_str.setItem(r, 1, t)
            self.tbl_str.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            tabs.addTab(self.tbl_str, tr("Song-id pool ({n})").format(n=len(self.model.strings)))

            lay.addWidget(tabs, 1)

            # shared tools: filter, edited highlight, int-range check, copy/paste
            self._tools = [TableTools(
                self.tbl, int_cols=range(4, 4 + RECORD_INTS),
                row_text=lambda r: "%s %s" % (self._rows[r][0].player,
                                              DIFF_NAMES[self._rows[r][1]]))]
            bar = ToolBar(self._tools, tr("Filter by song number, 1P/2P, difficulty or value..."))
            self.ed_filter = bar.ed
            bar_slot.addWidget(bar)

            # ---- buttons ----
            btns = QHBoxLayout()
            btns.addStretch(1)
            b_save = QPushButton(tr("Save")); b_save.clicked.connect(self._save)
            b_cancel = QPushButton(tr("Cancel")); b_cancel.clicked.connect(self.reject)
            btns.addWidget(b_save); btns.addWidget(b_cancel)
            lay.addLayout(btns)

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
                        v = parse_int(item.text())
                        if not (-2147483648 <= v <= 4294967295):
                            raise ValueError(f"row {r} field f{c:02d}: {v} out of int32 range")
                        if v > 2147483647:
                            v -= 4294967296   # accept unsigned spelling
                        rec.values[c] = v
            except ValueError as exc:
                QMessageBox.critical(self, tr("Edit error"), str(exc))
                return
            self.result_bytes = serialize(self.model)
            self.accept()

except ImportError:  # PySide6 not available — parse/serialize still usable
    Editor = None  # type: ignore


# --------------------------------------------------------------------------- #
# self-test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from taiko2x6.app import apppaths
    import os
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, FILENAME),
        str(apppaths.samples_dir() / "bins" / "tuning.bin"),
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

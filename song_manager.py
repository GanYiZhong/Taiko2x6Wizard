#!/usr/bin/env python3
"""
Cross-bin "Song Manager" for Taiko no Tatsujin (SYSTEM256 / PS2-era arcade).

This dialog presents the 90 index-aligned songs from three already-parsed
binary tables as a single editable table and writes back only the files that
actually changed:

  * musicinfo.bin  (bineditor_musicinfo) -- genre flags + score thresholds
  * tuning.bin     (bineditor_tuning)    -- per-chart STAR ratings (1P & 2P)
  * streaminfo.bin (bineditor_streaminfo)-- per-song BGM stream name

The three bins are INDEX-ALIGNED: song k = musicinfo.sec0[k] = tuning blocks
[2k, 2k+1] = (optionally) a streaminfo record named ``music_<id>``.

Edits are patched back through each module's own model + serializer:

  * musicinfo -> MI.serialize (rebuilds file, supports add/remove of sec0 rows)
  * tuning    -> TU.serialize for value-only edits (in-place int patch);
                 a local structural rebuild (``_tuning_rebuild``) for add/remove
  * streaminfo-> SI.serialize for value-only edits; a local structural rebuild
                 (``_stream_rebuild``) for add/remove

self.result is a dict mapping ONLY changed filenames to new bytes (or None if
cancelled / nothing changed). A no-op save is byte-exact for every file.

Run the self-test:
    QT_QPA_PLATFORM=offscreen python song_manager.py
"""
from __future__ import annotations

import os
import struct
import sys

# import the (read-only) per-bin parsers ------------------------------------ #
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import bineditor_musicinfo as MI
import bineditor_tuning as TU
import bineditor_streaminfo as SI

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QHeaderView, QMessageBox, QLineEdit, QAbstractItemView,
)


# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
MUSICINFO = "musicinfo.bin"
TUNING = "tuning.bin"
STREAMINFO = "streaminfo.bin"

DIFF_NAMES = ["easy", "normal", "hard", "oni"]
STAR_INDEX = 5            # values[5] = star rating within a tuning ChartRecord
GENRE_COL = 2             # sec0[k][2]
SCORE_COL = 7             # sec0[k][7] == base score (== col17 == col19)
SCORE3_COL = 18           # base + 3000
SCORE5_COL = 20           # base + 5000

COLS = ["idx", "id", "genre", "easy*", "normal*", "hard*", "oni*",
        "score", "score+3k", "score+5k", "stream"]
COL_IDX, COL_ID, COL_GENRE = 0, 1, 2
COL_EASY, COL_NORMAL, COL_HARD, COL_ONI = 3, 4, 5, 6
COL_SCORE, COL_SCORE3, COL_SCORE5, COL_STREAM = 7, 8, 9, 10


# --------------------------------------------------------------------------- #
# pool helpers for the structural tuning rebuild
# --------------------------------------------------------------------------- #
def _mi_row_by_id(m, song_id) -> int | None:
    """Index of the musicinfo SEC0 row whose id (pool[col0]) == ``song_id``."""
    if not song_id:
        return None
    target = song_id.encode("ascii", "replace")
    pool = m.pool
    for i, rec in enumerate(m.sec0):
        off = rec[0]
        e = pool.find(b"\x00", off)
        if pool[off:(e if e >= 0 else len(pool))] == target:
            return i
    return None


def _music_ids_from_tuning(t: TU.TuningModel) -> list:
    """The ``music_`` pool entries, in order, give the 90 song ids (k-aligned)."""
    return [s.text[len("music_"):] for s in t.strings if s.text.startswith("music_")]


def _pool_segments(t: TU.TuningModel) -> tuple:
    """Split the tuning string pool into one UNIFORM byte-segment per song.

    Returns (segments, trailer).

    Empirically the per-song pool unit is:
        <id>\\0  music_<id>\\0  <title>\\0  then 8 stems <id>{1p,2p}_{e,n,h,m}\\0

    Songs 1..N-1 carry their leading ``<id>\\0`` in the pool, but song 0's
    leading ``<id>\\0`` lives in a TRAILER appended to the last block (4 bytes of
    0xFF alignment slack + ``<id>\\0``) that immediately precedes the pool. We
    therefore return every segment WITH a leading id token (synthesising song
    0's from the trailer), plus the raw trailer bytes up to and including the
    bare id so a rebuild can relocate it.
    """
    raw = bytes(t.raw)
    pool = raw[t.pool_offset:]
    ids = _music_ids_from_tuning(t)

    # song 0's bare id (plus a little alignment slack before it) forms the
    # TRAILER that sits just before the pool, appended to the last block beyond
    # its standard tail. We must NOT greedily absorb the block's own 0xFF filler:
    # the trailer is exactly the bytes of the last block's tail that exceed a
    # *standard* 2P-block tail. A standard 2P tail length = that of any other 2P
    # (odd-index) block that isn't the final one.
    std_2p_tail_len = None
    for blk in t.blocks:
        if (blk.index % 2 == 1) and blk.index != (len(t.blocks) - 1):
            std_2p_tail_len = len(blk.tail)
            break
    last_tail = t.blocks[-1].tail
    if std_2p_tail_len is not None and len(last_tail) > std_2p_tail_len:
        trailer = last_tail[std_2p_tail_len:]   # slack + <id>\0
    else:
        trailer = b""
    # split the bare-id token out of the trailer (everything from the last 0xFF)
    k0 = 0
    while k0 < len(trailer) and trailer[k0] == 0xFF:
        k0 += 1
    song0_id_token = trailer[k0:]               # <id>\0

    # token start offsets within the pool (each token = bytes up to & incl NUL)
    tok_starts = []
    i, n = 0, len(pool)
    while i < n:
        tok_starts.append(i)
        nx = pool.find(b"\x00", i)
        if nx < 0:
            break
        i = nx + 1

    # segment k>=1 begins at the bare-id token preceding ``music_<id>``.
    seg_starts = [0]
    for k in range(1, len(ids)):
        mp = pool.find(b"music_" + ids[k].encode("ascii") + b"\x00")
        prev = [st for st in tok_starts if st < mp]
        seg_starts.append(prev[-1] if prev else mp)
    seg_starts.append(len(pool))

    segments = []
    for k in range(len(ids)):
        seg = pool[seg_starts[k]:seg_starts[k + 1]]
        if k == 0:
            # song 0's segment in the pool lacks its leading bare id (it is in
            # the trailer). Prepend it so every segment is uniform.
            seg = song0_id_token + seg
        segments.append(seg)
    return segments, trailer


def _make_song_segment(song_id: str, title_bytes: bytes) -> bytes:
    """Build one song's UNIFORM pool segment (always with leading bare id).

    Layout: <id>\\0 music_<id>\\0 <title>\\0 then 8 stems <id>{1p,2p}_{e,n,h,m}\\0.
    """
    out = bytearray()
    idb = song_id.encode("ascii")
    out += idb + b"\x00"
    out += b"music_" + idb + b"\x00"
    out += title_bytes + b"\x00"
    for p in ("1p", "2p"):
        for d in ("e", "n", "h", "m"):
            out += idb + p.encode() + b"_" + d.encode() + b"\x00"
    return bytes(out)


def _segment_title(seg: bytes, song_id: str) -> bytes:
    """Extract the UTF-8 title token from a uniform song pool segment."""
    toks = seg.split(b"\x00")
    idx = 0
    bare = song_id.encode("ascii")
    if idx < len(toks) and toks[idx] == bare:
        idx += 1
    if idx < len(toks) and toks[idx] == b"music_" + bare:
        idx += 1
    return toks[idx] if idx < len(toks) else b""


def _split_segment_leading_id(seg: bytes):
    """Return (bare_id_token, rest) splitting a uniform segment's leading id."""
    nul = seg.find(b"\x00")
    if nul < 0:
        return b"", seg
    return seg[:nul + 1], seg[nul + 1:]


# --------------------------------------------------------------------------- #
# structural rebuilds (only used when songs are added/removed)
# --------------------------------------------------------------------------- #
def _tuning_rebuild(t: TU.TuningModel, order: list, new_songs: dict) -> bytes:
    """Rebuild tuning.bin for an add/remove operation.

    ``order`` is the desired list of original song indices (0..89) in their new
    order; entries that are strings starting with "+" reference ``new_songs``
    (a dict key -> dict with 'template_k', 'id', 'stars'(list[4]) ).

    The pre-pool region is the concatenation of each song's two blocks
    (1P even, 2P odd) -- 4 ChartRecords (27 int32) + verbatim tail. The string
    pool is the concatenation of per-song segments. Song-count is written into
    record[0] of the very first emitted 1P block.
    """
    if len([ref for ref in order]) < 1:
        raise ValueError("tuning rebuild requires at least one song")

    segments, trailer = _pool_segments(t)
    ids = _music_ids_from_tuning(t)

    # The game BINARY-SEARCHES the music table (MusicManager::SearchMusic) built
    # from tuning in ON-DISK order and does NOT re-sort at load, so tuning's
    # per-song blocks+pool MUST stay bytewise-sorted by id (the retail invariant:
    # the shipped pool is perfectly id-sorted). Appending a new song at the end
    # breaks the search -> "MusicManager::SearchMusic: <id> not found" self-loop
    # hang the moment the song is highlighted. Emit every song in bytewise-id
    # order so a newcomer lands at its sorted position (existing songs are
    # already sorted, so a stable sort only inserts the newcomer and leaves the
    # rest -- including song 0 -- in place).
    def _id_of(ref):
        if isinstance(ref, str) and ref.startswith("+"):
            return new_songs[ref]["id"].encode("latin-1")
        return ids[ref].encode("latin-1")
    order = sorted(order, key=_id_of)

    # The song-0 id-token relocation relies on a non-empty trailer being split
    # off the original last block (slack + bare id). If _pool_segments could not
    # establish a standard 2P tail (e.g. fewer than 2 original songs in the
    # source model), the trailer is empty and the rebuild would emit a malformed
    # file. Refuse rather than silently corrupt.
    if not trailer:
        raise ValueError(
            "cannot rebuild tuning.bin: source model has too few blocks to "
            "establish the song-0 trailer; structural edits are unsupported "
            "at this song count"
        )

    # The original last block's tail = a standard 2P tail + the trailer
    # (alignment slack + song-0 bare id). Strip that trailer so every block we
    # emit carries only its standard tail; we re-append a fresh trailer (for the
    # NEW first song) after the final block.
    orig_last_block = t.blocks[-1]
    orig_last_tail = orig_last_block.tail
    std_last_tail = orig_last_tail[:len(orig_last_tail) - len(trailer)]

    pre = bytearray()
    pool = bytearray()

    def block_bytes(blk: TU.Block, tail_override=None) -> bytes:
        b = bytearray()
        for rec in blk.records:
            b += struct.pack("<%di" % TU.RECORD_INTS, *rec.values)
        b += (tail_override if tail_override is not None else blk.tail)
        return bytes(b)

    # collect [1P_bytes, 2P_bytes, pool_segment, is_new] per emitted song
    emitted = []
    for ref in order:
        if isinstance(ref, str) and ref.startswith("+"):
            spec = new_songs[ref]
            tk = spec["template_k"]
            sid = spec["id"]
            stars = spec["stars"]
            blk1, blk2 = t.blocks[2 * tk], t.blocks[2 * tk + 1]
            t1 = std_last_tail if (2 * tk == len(t.blocks) - 1) else blk1.tail
            t2 = std_last_tail if (2 * tk + 1 == len(t.blocks) - 1) else blk2.tail
            b1 = bytearray(block_bytes(blk1, t1))
            b2 = bytearray(block_bytes(blk2, t2))
            for di in range(4):
                off = di * TU.RECORD_SIZE + STAR_INDEX * 4
                struct.pack_into("<i", b1, off, int(stars[di]))
                struct.pack_into("<i", b2, off, int(stars[di]))
            title = _segment_title(segments[tk], ids[tk])
            seg = _make_song_segment(sid, title)
            emitted.append([b1, b2, seg, True])       # bytearrays: patched below
        else:
            k = ref
            blk1, blk2 = t.blocks[2 * k], t.blocks[2 * k + 1]
            # normalise the tail of whichever block was the original last block
            t1 = std_last_tail if (2 * k == len(t.blocks) - 1) else blk1.tail
            t2 = std_last_tail if (2 * k + 1 == len(t.blocks) - 1) else blk2.tail
            emitted.append([bytearray(block_bytes(blk1, t1)),
                            bytearray(block_bytes(blk2, t2)),
                            segments[k], False])

    # Each tuning ChartRecord stores offsets into a LOGICAL buffer that is the
    # concatenation of every song's UNIFORM pool segment (id, music_id, title,
    # 8 stems) in emitted order:
    #     offset(song i, token j) = sum(len(segment_k) for k < i) + rel_within_seg
    #   1P block rec0 cols 1/2/3/4 -> id / music_id / title / stem[0];
    #   1P recs 1..3 col4 -> stems 1..3;  2P recs 0..3 col4 -> stems 4..7.
    # (Song 0's leading bare id physically lives in the trailer immediately before
    # the pool, so the game's pool base points there; the offsets are identical to
    # this inline-id logical model -- verified byte-exact against retail.)
    # Recompute these columns for EVERY song from its emitted position, so the
    # sorted insertion above shifts the offsets of songs after the newcomer too.
    # A cloned new-song block would otherwise inherit the TEMPLATE's offsets (and
    # the previous code also mis-based new songs by len(song-0 id) = the +6 that
    # left the highlighted song's tokens pointing 6 bytes early). Byte-exact for an
    # unchanged, already-sorted order.
    RS = TU.RECORD_SIZE
    _P1 = [((0, 1), 0), ((0, 2), 1), ((0, 3), 2), ((0, 4), 3),
           ((1, 4), 4), ((2, 4), 5), ((3, 4), 6)]
    _P2 = [((0, 4), 7), ((1, 4), 8), ((2, 4), 9), ((3, 4), 10)]
    _base = 0
    for b1, b2, seg, _is_new in emitted:
        toks = seg.split(b"\x00")[:-1]
        rel, o = [], 0
        for tkn in toks:
            rel.append(o)
            o += len(tkn) + 1
        for (ri, ci), ti in _P1:
            if ti < len(rel):
                struct.pack_into("<i", b1, ri * RS + ci * 4, _base + rel[ti])
        for (ri, ci), ti in _P2:
            if ti < len(rel):
                struct.pack_into("<i", b2, ri * RS + ci * 4, _base + rel[ti])
        _base += len(seg)

    # assemble pre-pool blocks
    for b1, b2, _seg, _isnew in emitted:
        pre += b1
        pre += b2

    # the trailer carries the FIRST song's bare id; build it from a fresh
    # alignment-slack prefix + that id. The pool's first segment therefore drops
    # its leading bare-id token (it now lives in the trailer); all later segments
    # keep theirs. The slack is the leading 0xFF run of the original trailer.
    slack_len = 0
    while slack_len < len(trailer) and trailer[slack_len] == 0xFF:
        slack_len += 1
    slack = trailer[:slack_len]
    first_id_tok, first_rest = _split_segment_leading_id(emitted[0][2])
    new_trailer = slack + first_id_tok
    pre += new_trailer

    pool += first_rest
    for b1, b2, seg, _isnew in emitted[1:]:
        pool += seg

    # write song count into record[0] of the first 1P block
    struct.pack_into("<i", pre, 0, len(order))

    return bytes(pre) + bytes(pool)


# --------------------------------------------------------------------------- #
# Taiko 14 tuning.bin (different layout from T8)
# --------------------------------------------------------------------------- #
# Layout (reversed from T14GAME.dec loader @file 0x67d8 + SearchMusic @0x68f8):
#   [u32 count][count x 2028-byte fixed records][string pool]
# The game stores manager[4]=records begin, [8]=end, [0xc]=count, [0x10]=pool,
# and MusicManager::SearchMusic binary-searches the records by name (pool[col0]),
# so the records MUST stay bytewise-sorted by their bare id. Each record holds
# ABSOLUTE pool offsets (relative to the pool start = 4+count*2028) in these
# columns -> the 11 tokens id, music_id, title, and the 8 stems
# 1p_{e,n,h,m} / 2p_{e,n,h,m}; the pool segment format is identical to T8's
# _make_song_segment. Pool string data lives after the records.
T14_TUNING_REC = 2028
T14_TUNING_OFFCOLS = [0, 1, 2, 3, 31, 59, 87, 255, 283, 311, 339]


def _is_t14_tuning(tu: bytes) -> bool:
    """True if ``tu`` is the Taiko-14 fixed-record tuning layout."""
    if len(tu) < 8:
        return False
    cnt = struct.unpack_from("<I", tu, 0)[0]
    ps = 4 + cnt * T14_TUNING_REC
    if not (0 < cnt < 100000 and ps + 16 <= len(tu)):
        return False
    e = tu.find(b"\x00", ps)
    if e < 0:
        return False
    bare = tu[ps:e]
    # pool begins right after the last fixed record with "<bareid>\0music_<bareid>"
    return bool(bare) and bare.isascii() and tu[e + 1:e + 7] == b"music_"


def _tuning_rebuild_t14(tu: bytes, keep_ids: list, new_specs: list) -> bytes:
    """Rebuild a Taiko-14 tuning.bin for an add/remove.

    ``keep_ids`` = bare ids of existing songs to retain; ``new_specs`` = list of
    (new_id, template_id). Records are cloned verbatim, new songs get a fresh
    pool segment APPENDED (existing absolute offsets stay valid because they are
    relative to the pool start, which the game recomputes from the count), and
    the whole record array is re-sorted bytewise by id for the binary search.
    """
    cnt = struct.unpack_from("<I", tu, 0)[0]
    REC = T14_TUNING_REC
    poolstart = 4 + cnt * REC
    pool = bytearray(tu[poolstart:])
    recs = [bytearray(tu[4 + i * REC:4 + (i + 1) * REC]) for i in range(cnt)]

    def cs(off):
        e = pool.find(b"\x00", off)
        return bytes(pool[off:e if e >= 0 else len(pool)])

    orig = {}   # bare-id bytes -> record
    for r in recs:
        bid = cs(struct.unpack_from("<i", r, 0)[0])
        orig[bid] = r

    keep = {k.encode("ascii", "replace") for k in keep_ids}
    emitted = [(bid, r) for bid, r in orig.items() if bid in keep]

    for new_id, tmpl_id in new_specs:
        tmpl = orig.get(tmpl_id.encode("ascii", "replace"))
        if tmpl is None:
            raise ValueError(f"T14 tuning rebuild: template id {tmpl_id!r} not found")
        rec = bytearray(tmpl)
        title = cs(struct.unpack_from("<i", tmpl, 2 * 4)[0])   # col2 = title
        seg = _make_song_segment(new_id, title)
        base_off = len(pool)
        pool += seg
        toks = seg.split(b"\x00")[:-1]
        rel, o = [], 0
        for t in toks:
            rel.append(o)
            o += len(t) + 1
        for k, col in enumerate(T14_TUNING_OFFCOLS):
            if k < len(rel):
                struct.pack_into("<i", rec, col * 4, base_off + rel[k])
        emitted.append((new_id.encode("ascii", "replace"), rec))

    emitted.sort(key=lambda t: t[0])   # bytewise id sort (SearchMusic invariant)
    out = bytearray(struct.pack("<I", len(emitted)))
    for _bid, rec in emitted:
        out += rec
    out += pool
    return bytes(out)


def _stream_rebuild(s: SI.Model, keep_names: set, add_specs: list) -> bytes:
    """Rebuild streaminfo.bin honouring removals (keep_names) and additions.

    ``keep_names`` is the set of record names to retain (others dropped).
    ``add_specs`` is a list of (new_name, template_record) to add.

    CRITICAL: the record's four "numeric" fields are actually POOL OFFSETS (e.g.
    field0 = 16 on every record points at a shared empty-string sentinel that
    selects the stream-load code path in the game). Rebuilding the pool compactly
    shifts every offset, flips that path, and hangs the opening on a wrong file
    lookup. So we preserve the ORIGINAL pool bytes verbatim and only APPEND new
    names — every existing record keeps its exact name_offset AND field offsets,
    and cloned records inherit offsets that still resolve against the same pool.
    The game also BINARY-SEARCHES the records by name, so the array is kept
    bytewise name-sorted (the retail invariant).
    """
    pool = bytearray(s.raw_pool)                     # original pool, verbatim

    def name_slot(name: str) -> int:
        b = name.encode("ascii", "replace") + b"\x00"
        i = bytes(pool).find(b)                      # reuse an identical slot
        if i >= 0:
            return i
        off = len(pool)
        pool.extend(b)                               # else append at the end
        return off

    # (name, name_offset, fields) — existing records keep their offsets/fields.
    recs = []
    for r in s.records:
        if r.name.startswith("music_") and r.name not in keep_names:
            continue
        recs.append((r.name, r.name_offset, list(r.fields)))
    for name, tmpl in add_specs:
        recs.append((name, name_slot(name), list(tmpl.fields)))

    recs.sort(key=lambda t: t[0].encode("latin-1"))  # bytewise name-sorted

    count = len(recs)
    pool_offset = SI.HEADER_SIZE + count * SI.RECORD_SIZE
    out = bytearray()
    out += struct.pack(SI.HEADER_FMT, count, s.record_size, pool_offset)
    for _name, name_off, fields in recs:
        out += struct.pack(SI.RECORD_FMT, name_off, *fields)
    out += bytes(pool)
    return bytes(out)


# --------------------------------------------------------------------------- #
# the per-song view assembled from the three models
# --------------------------------------------------------------------------- #
class _Song:
    # ``k`` is the ORIGINAL song index (0..N-1) for songs loaded from the bins,
    # and ``None`` for songs added in this session (they have no original row).
    # Never store a running row-counter in ``k`` -- the source/template row for a
    # new song lives exclusively in ``_template_k``, and the new song's stable
    # identity (its rebuild token) lives in ``_token``.
    __slots__ = ("k", "id", "genre", "stars", "score", "score3", "score5",
                 "stream", "stream_rec", "_template_k", "_is_new", "_token")

    def __init__(self, k, sid, genre, stars, score, score3, score5,
                 stream, stream_rec):
        self.k = k
        self.id = sid
        self.genre = genre
        self.stars = stars              # list[4]
        self.score = score
        self.score3 = score3
        self.score5 = score5
        self.stream = stream            # stream name or ""
        self.stream_rec = stream_rec    # SI.Record or None
        self._template_k = None
        self._is_new = False
        self._token = None              # rebuild token ("+N") for new songs


# --------------------------------------------------------------------------- #
# the dialog
# --------------------------------------------------------------------------- #
class SongManager(QDialog):
    """Cross-bin song manager.

    After exec(): ``self.result`` is a dict mapping ONLY changed filenames to
    new bytes (e.g. {"musicinfo.bin": b"...", "tuning.bin": b"..."}); ``None``
    if cancelled or nothing changed.
    """

    def __init__(self, mi_bytes: bytes, tu_bytes: bytes, si_bytes: bytes, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Taiko Song Manager (cross-bin)")
        self.resize(1120, 700)

        self._mi_in = bytes(mi_bytes)
        self._tu_in = bytes(tu_bytes)
        self._si_in = bytes(si_bytes)

        self.mi = MI.parse(mi_bytes)
        self.tu = TU.parse(tu_bytes)
        self.si = SI.parse(si_bytes)

        self.result: dict | None = None

        # operation log for add/remove (drives structural rebuild on save)
        # we maintain a working "order" list of original-k indices / "+tokens"
        self._order = list(range(len(self.mi.sec0)))
        self._new_songs = {}      # "+token" -> spec dict (for tuning rebuild)
        self._new_counter = 0
        self._removed_anything = False
        self._added_anything = False

        self._songs = self._assemble()
        # immutable snapshot of originally-loaded per-song values, keyed by k,
        # used for diff-based writes so a no-op save stays byte-exact.
        self._orig_songs = {
            s.k: _Song(s.k, s.id, s.genre, list(s.stars), s.score, s.score3,
                       s.score5, s.stream, s.stream_rec)
            for s in self._songs
        }
        self._build_ui()

    # -- assemble per-song rows from the three models ----------------------- #
    def _assemble(self):
        ids = _music_ids_from_tuning(self.tu)
        # stream lookup by name == music_<id>
        stream_by_name = {}
        for r in self.si.records:
            if r.name.startswith("music_"):
                stream_by_name[r.name] = r

        songs = []
        for k in range(len(self.mi.sec0)):
            sid = ids[k] if k < len(ids) else f"song{k}"
            rec = self.mi.sec0[k]
            # Guard the tuning lookup the same way `ids[k]` is guarded above: a
            # DB where musicinfo.bin has more song records than tuning.bin has
            # blocks would otherwise crash here with "list index out of range".
            blk = 2 * k
            if blk < len(self.tu.blocks):
                block = self.tu.blocks[blk]
                stars = []
                for di in range(4):
                    try:
                        stars.append(block.records[di].values[STAR_INDEX])
                    except (IndexError, AttributeError):
                        stars.append(1)
            else:
                stars = [1, 1, 1, 1]
            sname = f"music_{sid}"
            srec = stream_by_name.get(sname)
            songs.append(_Song(
                k=k, sid=sid,
                genre=rec[GENRE_COL],
                stars=stars,
                score=rec[SCORE_COL],
                score3=rec[SCORE3_COL],
                score5=rec[SCORE5_COL],
                stream=srec.name if srec else "",
                stream_rec=srec,
            ))
        return songs

    # -- ui ----------------------------------------------------------------- #
    def _build_ui(self):
        lay = QVBoxLayout(self)

        info = QLabel(
            f"{len(self._songs)} songs · musicinfo sec0={len(self.mi.sec0)} · "
            f"tuning blocks={len(self.tu.blocks)} · streaminfo records={self.si.count}"
        )
        info.setStyleSheet("color:#888;font-family:Consolas;")
        lay.addWidget(info)

        note = QLabel(
            "Add duplicates only the DATABASE entry (musicinfo + tuning + "
            "streaminfo). Chart (fumen) / audio / textures are separate and are "
            "NOT created here."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#c98;")
        lay.addWidget(note)

        # filter box
        top = QHBoxLayout()
        top.addWidget(QLabel("filter id:"))
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText("type a song id…")
        self.ed_filter.textChanged.connect(self._apply_filter)
        top.addWidget(self.ed_filter, 1)
        lay.addLayout(top)

        self.tbl = QTableWidget(len(self._songs), len(COLS))
        self.tbl.setHorizontalHeaderLabels(COLS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
        )
        for r, song in enumerate(self._songs):
            self._fill_row(r, song)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl.horizontalHeader().setSectionResizeMode(COL_STREAM, QHeaderView.Stretch)
        lay.addWidget(self.tbl, 1)

        btns = QHBoxLayout()
        b_add = QPushButton("Add (duplicate selected)")
        b_add.setToolTip("Duplicate the selected song's DATABASE entry only "
                         "(no chart/audio/textures).")
        b_add.clicked.connect(self._on_add)
        b_rem = QPushButton("Remove selected")
        b_rem.clicked.connect(self._on_remove)
        btns.addWidget(b_add)
        btns.addWidget(b_rem)
        btns.addStretch(1)
        b_save = QPushButton("Save")
        b_save.clicked.connect(self._on_save)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_save)
        btns.addWidget(b_cancel)
        lay.addLayout(btns)

    def _fill_row(self, r, song: _Song):
        def item(text, editable=True):
            it = QTableWidgetItem(str(text))
            if not editable:
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            return it

        idx_text = "+" if (getattr(song, "_is_new", False) or song.k is None) else song.k
        self.tbl.setItem(r, COL_IDX, item(idx_text, editable=False))
        self.tbl.setItem(r, COL_ID, item(song.id))
        self.tbl.setItem(r, COL_GENRE, item(song.genre))
        self.tbl.setItem(r, COL_EASY, item(song.stars[0]))
        self.tbl.setItem(r, COL_NORMAL, item(song.stars[1]))
        self.tbl.setItem(r, COL_HARD, item(song.stars[2]))
        self.tbl.setItem(r, COL_ONI, item(song.stars[3]))
        self.tbl.setItem(r, COL_SCORE, item(song.score))
        self.tbl.setItem(r, COL_SCORE3, item(song.score3))
        self.tbl.setItem(r, COL_SCORE5, item(song.score5))
        self.tbl.setItem(r, COL_STREAM, item(song.stream))

    def _apply_filter(self, text):
        text = text.strip().lower()
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, COL_ID)
            hay = it.text().lower() if it else ""
            self.tbl.setRowHidden(r, bool(text) and text not in hay)

    # -- read the table back into _songs ------------------------------------ #
    def _harvest_table(self):
        """Pull current cell values into the _songs list. Raises on bad ints."""
        for r in range(self.tbl.rowCount()):
            song = self._songs[r]
            song.id = self.tbl.item(r, COL_ID).text().strip()
            song.genre = int(self.tbl.item(r, COL_GENRE).text())
            song.stars = [
                int(self.tbl.item(r, COL_EASY).text()),
                int(self.tbl.item(r, COL_NORMAL).text()),
                int(self.tbl.item(r, COL_HARD).text()),
                int(self.tbl.item(r, COL_ONI).text()),
            ]
            song.score = int(self.tbl.item(r, COL_SCORE).text())
            song.score3 = int(self.tbl.item(r, COL_SCORE3).text())
            song.score5 = int(self.tbl.item(r, COL_SCORE5).text())
            song.stream = self.tbl.item(r, COL_STREAM).text().strip()

        # re-validate ids: post-hoc table edits bypass the add-time uniqueness /
        # non-empty checks, and a blank/duplicate id silently corrupts the
        # music_<id> pool entries and tuning stems on a structural save.
        seen = {}
        for r in range(self.tbl.rowCount()):
            sid = self._songs[r].id
            if not sid:
                raise ValueError(f"row {r}: song id must not be empty")
            if sid in seen:
                raise ValueError(
                    f"row {r}: duplicate song id {sid!r} (also row {seen[sid]})"
                )
            seen[sid] = r

    # -- add / remove ------------------------------------------------------- #
    def _selected_row(self):
        rows = self.tbl.selectionModel().selectedRows()
        if not rows:
            sel = self.tbl.currentRow()
            return sel if sel >= 0 else None
        return rows[0].row()

    def _on_add(self):
        try:
            self._harvest_table()
        except Exception as exc:
            QMessageBox.critical(self, "Edit error", str(exc))
            return
        r = self._selected_row()
        if r is None:
            QMessageBox.information(self, "Add", "Select a song to duplicate.")
            return
        src = self._songs[r]

        # unique id
        existing = {s.id for s in self._songs}
        new_id = f"{src.id}_copy"
        n = 1
        while new_id in existing:
            n += 1
            new_id = f"{src.id}_copy{n}"

        token = f"+{self._new_counter}"
        self._new_counter += 1
        self._new_songs[token] = {
            "template_k": src.k,
            "template_id": src.id,   # used by the T14 tuning rebuild (order differs)
            "id": new_id,
            "stars": list(src.stars),
        }
        self._order.append(token)
        self._added_anything = True

        # new in-memory _Song. ``k`` is None (no original row); the source row
        # is carried in ``_template_k`` and the rebuild token in ``_token`` so
        # removal can target the exact token regardless of insertion order.
        new_song = _Song(
            k=None, sid=new_id, genre=src.genre,
            stars=list(src.stars), score=src.score, score3=src.score3,
            score5=src.score5,
            stream=(f"music_{new_id}" if src.stream_rec is not None else ""),
            stream_rec=src.stream_rec,   # template for fields; name overridden
        )
        # tag the source musicinfo/stream template index for save-time cloning
        new_song._template_k = src.k          # type: ignore[attr-defined]
        new_song._is_new = True               # type: ignore[attr-defined]
        new_song._token = token               # type: ignore[attr-defined]
        self._songs.append(new_song)

        r2 = self.tbl.rowCount()
        self.tbl.insertRow(r2)
        self._fill_row(r2, new_song)
        # idx column shows "+" for new (non-original) songs
        self.tbl.item(r2, COL_IDX).setText("+")

    def _on_remove(self):
        try:
            self._harvest_table()
        except Exception as exc:
            QMessageBox.critical(self, "Edit error", str(exc))
            return
        r = self._selected_row()
        if r is None:
            QMessageBox.information(self, "Remove", "Select a song to remove.")
            return
        if len(self._songs) <= 1:
            QMessageBox.warning(self, "Remove", "Cannot remove the last song.")
            return

        song = self._songs[r]
        # update order list: remove the matching entry (token for new, k for orig)
        if getattr(song, "_is_new", False):
            # Each new song carries the exact rebuild token it was created with,
            # so removing the right one never depends on insertion order. Fall
            # back to matching by (unique) id among the still-present new songs
            # for songs created via an external path that did not stamp _token.
            tok = getattr(song, "_token", None)
            if tok is None or tok not in self._new_songs:
                tok = None
                for key, spec in self._new_songs.items():
                    if spec["id"] == song.id:
                        tok = key
                        break
            if tok is not None:
                if tok in self._order:
                    self._order.remove(tok)
                self._new_songs.pop(tok, None)
        else:
            if song.k in self._order:
                self._order.remove(song.k)
            self._removed_anything = True

        del self._songs[r]
        self.tbl.removeRow(r)

    # -- save --------------------------------------------------------------- #
    def _on_save(self):
        try:
            self._harvest_table()
        except Exception as exc:
            QMessageBox.critical(self, "Edit error", str(exc))
            return
        try:
            result = self._build_result()
        except Exception as exc:
            # surface a readable reason (validation errors carry the offending
            # row/id); nothing is written, so cleanly-serialised bins are not
            # half-applied.
            QMessageBox.critical(self, "Save error", str(exc) or repr(exc))
            return
        self.result = result if result else None
        self.accept()

    def _build_result(self) -> dict:
        # "structural" means the set / order of songs differs from the original
        # 0..N-1 identity. Add-then-remove that returns to the original order is
        # therefore NOT structural, so the value-only (byte-exact) serializers
        # are used and a pristine state round-trips byte-identically.
        structural = self._order != list(range(len(self.mi.sec0)))

        # musicinfo is always rebuilt from self._order (it needs no structural
        # flag); tuning/streaminfo branch on it for the value-only fast path.
        mi_bytes = self._serialize_musicinfo()
        tu_bytes = self._serialize_tuning(structural)
        si_bytes = self._serialize_streaminfo(structural)

        result = {}
        if mi_bytes != self._mi_in:
            result[MUSICINFO] = mi_bytes
        if tu_bytes != self._tu_in:
            result[TUNING] = tu_bytes
        if si_bytes != self._si_in:
            result[STREAMINFO] = si_bytes
        return result

    # ---- musicinfo -------------------------------------------------------- #
    def _serialize_musicinfo(self) -> bytes:
        m = MI.parse(self._mi_in)   # fresh model from original bytes

        # Resolve a new song's TEMPLATE musicinfo SEC0 row. ``template_k`` is a
        # _songs list position, which only equals the musicinfo SEC0 index when
        # the two are parallel (true on T8). On T14 the SongManager's _songs
        # order (id-sorted, from tuning) differs from musicinfo SEC0 order
        # (genre-grouped, with header rows), so template_k would clone the WRONG
        # row -- e.g. picking a genre-header row, giving the new song a header
        # genre (col2) that belongs to no song folder => invisible in select.
        # Match by the template's id (its col0 string) and fall back to
        # template_k only if the id can't be found.
        def _tmpl_row(spec):
            # T8: _songs is parallel to musicinfo SEC0, so template_k (a _songs
            # index) is the correct SEC0 index. T14: they diverge, so resolve by
            # id instead. Guard by variant so T8 stays byte-exact.
            if m.variant == "t14":
                r = _mi_row_by_id(m, spec.get("template_id"))
                if r is not None:
                    return r
            return spec["template_k"]

        # Build the new sec0 list according to self._order.
        new_sec0 = []
        for ref in self._order:
            if isinstance(ref, str) and ref.startswith("+"):
                spec = self._new_songs[ref]
                rec = list(m.sec0[_tmpl_row(spec)])   # clone template row (by id)
                new_sec0.append(rec)
            else:
                new_sec0.append(list(m.sec0[ref]))

        # SEC0 col0 / col1 are POOL OFFSETS to the song's ID and TITLE strings
        # (the game builds asset names like "select_full_<pool[col0]>" and reads
        # the wheel name from pool[col0]). A cloned row inherits the TEMPLATE's
        # offsets, which point at the wrong song and leave the new id/title with
        # no slot — so the song-list build reads garbage and hangs. Append the new
        # song's "<id>\0<title>\0" to the pool (kept > every existing offset, so
        # the monotone-in-SEC0-order invariant also holds) and repoint col0/col1.
        _pool = bytearray(m.pool)
        for i, ref in enumerate(self._order):
            if not (isinstance(ref, str) and ref.startswith("+")):
                continue
            spec = self._new_songs[ref]
            sid = spec["id"].encode("ascii", "replace")
            # reuse the template row's title string (display-only) for the clone
            tt_off = m.sec0[_tmpl_row(spec)][1]
            tend = bytes(_pool).find(b"\x00", tt_off) if 0 <= tt_off < len(_pool) else -1
            title = bytes(_pool[tt_off:tend]) if tend >= 0 else sid
            id_off = len(_pool)
            _pool += sid + b"\x00"
            title_off = len(_pool)
            _pool += title + b"\x00"
            new_sec0[i][0] = id_off
            new_sec0[i][1] = title_off
        m.pool = bytes(_pool)

        # --- Taiko 14+ SEC0 index / display-permutation columns ------------- #
        # T14 SEC0 records are 26 ints and carry per-record indices the selection
        # wheel depends on:
        #   col13, col14 = the record's OWN index (identity 0..N-1)
        #   col15, col16 = two display-order PERMUTATIONS of 0..N-1 -- the wheel
        #                  iterates these, so an index that appears in NEITHER is
        #                  never drawn (the song is silently invisible)
        #   col10        = index of a paired "ura/ex" chart, else -1
        # A cloned new row inherits the TEMPLATE's values for all of these, which
        # duplicates the template's slot, drops the new row's index from both
        # permutations (=> invisible in song select), and leaves a stale ura
        # pointer. New rows are APPENDED at the end of SEC0, so each new row's
        # index == its new (last) display slot: set self-index + both permutations
        # to that index, and clear the pair. (Only touches new rows -> existing
        # rows and a no-op save stay byte-exact.)
        if m.variant == "t14":
            for i, ref in enumerate(self._order):
                if isinstance(ref, str) and ref.startswith("+"):
                    rec = new_sec0[i]
                    rec[13] = rec[14] = rec[15] = rec[16] = i
                    rec[10] = -1

        # Now overlay edited per-song values. self._songs is aligned to _order.
        # Apply DIFF-BASED to stay byte-exact: only touch a column when its
        # displayed value actually changed from what was loaded. This matters
        # because cols 17/19 mirror col7 for most songs but a handful diverge,
        # so blindly mirroring would corrupt those rows on a no-op save.
        #
        # These column indices (GENRE_COL=2, SCORE_COL=7, 17, 19, SCORE3_COL=18,
        # SCORE5_COL=20) are TAIKO 8 semantics. In T14 the score fields live in
        # cols 22..25 and cols 17..21 are a fixed -1 sentinel on EVERY record --
        # writing the T8 columns leaves a cloned new row with col17/col19 == 0,
        # the only record in the file that breaks the -1 invariant, which the
        # song-select filter rejects => the song never appears in the wheel. The
        # T14 clone already carries correct values for all of these, so skip the
        # T8-shaped overlay entirely for T14 (per-song value EDITING on T14 is a
        # separate, not-yet-mapped feature).
        if m.variant != "t14":
            for i, song in enumerate(self._songs):
                rec = new_sec0[i]
                ref = self._order[i]
                if isinstance(ref, str) and ref.startswith("+"):
                    orig = None   # newly added clone: write all canonical fields
                else:
                    orig = self._orig_songs[ref]

                if orig is None or song.genre != orig.genre:
                    rec[GENRE_COL] = song.genre
                if orig is None or song.score != orig.score:
                    rec[SCORE_COL] = song.score
                    rec[17] = song.score
                    rec[19] = song.score
                if orig is None or song.score3 != orig.score3:
                    rec[SCORE3_COL] = song.score3
                if orig is None or song.score5 != orig.score5:
                    rec[SCORE5_COL] = song.score5

        m.sec0 = new_sec0
        m.counts[0] = len(new_sec0)
        return MI.serialize(m)

    # ---- tuning ----------------------------------------------------------- #
    def _serialize_tuning(self, structural: bool) -> bytes:
        # Taiko 14 uses a different tuning layout (fixed 2028-byte records + a
        # string pool, songs binary-searched by name). Route it to its own
        # rebuild; the T8 block model below does not apply.
        if _is_t14_tuning(self._tu_in):
            return self._serialize_tuning_t14(structural)

        t = TU.parse(self._tu_in)

        if not structural:
            # value-only: patch star ratings into BOTH 1P and 2P blocks
            for i, song in enumerate(self._songs):
                k = self._order[i]   # original index (no add/remove here)
                for di in range(4):
                    t.blocks[2 * k].records[di].values[STAR_INDEX] = song.stars[di]
                    t.blocks[2 * k + 1].records[di].values[STAR_INDEX] = song.stars[di]
            return TU.serialize(t)

        # structural: first patch stars of *existing* songs into the model so
        # the rebuild copies the up-to-date values, and feed new-song star specs.
        # Map order-position -> star list for existing songs.
        for i, (song, ref) in enumerate(zip(self._songs, self._order)):
            if isinstance(ref, str) and ref.startswith("+"):
                # ensure the new_songs spec carries current edited stars/id
                self._new_songs[ref]["stars"] = list(song.stars)
                self._new_songs[ref]["id"] = song.id
            else:
                k = ref
                for di in range(4):
                    t.blocks[2 * k].records[di].values[STAR_INDEX] = song.stars[di]
                    t.blocks[2 * k + 1].records[di].values[STAR_INDEX] = song.stars[di]
        return _tuning_rebuild(t, self._order, self._new_songs)

    def _serialize_tuning_t14(self, structural: bool) -> bytes:
        # Value-only star edits on T14 are not mapped yet, so a non-structural
        # save returns the bytes unchanged (never corrupt). Structural add/remove
        # is handled by _tuning_rebuild_t14, keyed by song id (tuning order is
        # id-sorted and differs from musicinfo order).
        if not structural:
            return bytes(self._tu_in)
        keep_ids, new_specs = [], []
        for song, ref in zip(self._songs, self._order):
            if isinstance(ref, str) and ref.startswith("+"):
                spec = self._new_songs[ref]
                new_specs.append((song.id, spec.get("template_id", "")))
            else:
                keep_ids.append(song.id)
        return _tuning_rebuild_t14(self._tu_in, keep_ids, new_specs)

    # ---- streaminfo ------------------------------------------------------- #
    def _serialize_streaminfo(self, structural: bool) -> bytes:
        s = SI.parse(self._si_in)

        # name lookup
        by_name = {r.name: r for r in s.records}

        if not structural:
            # value-only: a song's stream name may have been renamed. Apply edits
            # to the matching record's name slot in place.
            ids = _music_ids_from_tuning(self.tu)
            for song in self._songs:
                # new songs have k is None; value-only path is only reached when
                # there are no structural changes, but guard regardless.
                k = song.k
                orig_name = (f"music_{ids[k]}"
                             if (k is not None and 0 <= k < len(ids)) else None)
                rec = by_name.get(orig_name) if orig_name else None
                if rec is None:
                    # also try matching by the song's current displayed name
                    rec = by_name.get(song.stream)
                if rec is not None and song.stream and song.stream != rec.name:
                    if len(song.stream.encode("ascii", "replace")) + 1 > rec.capacity:
                        raise ValueError(
                            f"stream name {song.stream!r} ({len(song.stream) + 1} "
                            f"bytes) does not fit slot of {rec.capacity} bytes "
                            f"(row id {song.id!r}); shorten it or change the song "
                            f"structurally"
                        )
                    SI._apply_name(s, rec, song.stream)
            return SI.serialize(s)

        # structural add/remove
        ids = _music_ids_from_tuning(self.tu)
        keep_names = set()
        add_specs = []
        # determine, per order entry, the stream record involved
        for song, ref in zip(self._songs, self._order):
            if isinstance(ref, str) and ref.startswith("+"):
                spec = self._new_songs[ref]
                tmpl_name = f"music_{ids[spec['template_k']]}"
                tmpl = by_name.get(tmpl_name)
                if tmpl is not None and song.stream:
                    add_specs.append((song.stream, tmpl))
            else:
                orig_name = f"music_{ids[ref]}" if ref < len(ids) else None
                if orig_name and orig_name in by_name:
                    keep_names.add(orig_name)
        return _stream_rebuild(s, keep_names, add_specs)


# --------------------------------------------------------------------------- #
# self-test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    BINS = os.environ.get(
        "TAIKO_BINS",
        r"C:\Users\User\AppData\Local\Temp\claude\D--"
        r"\ee6b327c-e139-4924-9604-a691103f5eaa\scratchpad\bins",
    )
    mi = open(os.path.join(BINS, "musicinfo.bin"), "rb").read()
    tu = open(os.path.join(BINS, "tuning.bin"), "rb").read()
    si = open(os.path.join(BINS, "streaminfo.bin"), "rb").read()

    app = QApplication.instance() or QApplication(sys.argv)
    fails = []

    def check(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # --- 1) NO-OP byte-exact ------------------------------------------------ #
    sm = SongManager(mi, tu, si)
    res = sm._build_result()
    check(res == {}, "no-op save yields empty change set (byte-exact)")

    # --- 2) EDIT round-trip (through the real harvest-from-table path) ------ #
    sm = SongManager(mi, tu, si)
    sm.tbl.item(3, COL_ONI).setText("9")     # song 3 oni star -> 9
    sm.tbl.item(3, COL_GENRE).setText("4")   # song 3 genre   -> 4
    sm._harvest_table()                      # mirrors what _on_save() does
    res = sm._build_result()
    check(MUSICINFO in res and TUNING in res, "edit changes musicinfo + tuning")
    check(STREAMINFO not in res, "edit leaves streaminfo unchanged (byte-exact)")
    # re-parse changed bins and confirm values
    m2 = MI.parse(res[MUSICINFO])
    t2 = TU.parse(res[TUNING])
    check(m2.sec0[3][GENRE_COL] == 4, "musicinfo genre re-parses as 4")
    check(t2.blocks[6].records[3].values[STAR_INDEX] == 9, "tuning 1P oni star == 9")
    check(t2.blocks[7].records[3].values[STAR_INDEX] == 9, "tuning 2P oni star == 9")
    check(len(m2.sec0) == 90 and t2.song_count == 90, "edit keeps song count 90")

    # --- 2b) STREAM NAME edit round-trip (in-place slot, same length) ------- #
    sm = SongManager(mi, tu, si)
    old_stream = sm._songs[2].stream
    new_stream = old_stream[:-1] + ("x" if old_stream[-1] != "x" else "y")
    sm.tbl.item(2, COL_STREAM).setText(new_stream)
    sm._harvest_table()
    res = sm._build_result()
    check(set(res.keys()) == {STREAMINFO},
          "stream rename changes only streaminfo (byte-exact elsewhere)")
    s2 = SI.parse(res[STREAMINFO])
    check(new_stream in [r.name for r in s2.records],
          f"streaminfo re-parses with renamed stream {new_stream!r}")

    # --- 3) ADD then verify +1 --------------------------------------------- #
    sm = SongManager(mi, tu, si)
    sm.tbl.selectRow(5)
    sm._on_add()
    res = sm._build_result()
    check(MUSICINFO in res and TUNING in res, "add changes musicinfo + tuning")
    m3 = MI.parse(res[MUSICINFO])
    t3 = TU.parse(res[TUNING])
    s3 = SI.parse(res[STREAMINFO]) if STREAMINFO in res else SI.parse(si)
    check(len(m3.sec0) == 91, f"add: musicinfo sec0 == 91 (got {len(m3.sec0)})")
    check(m3.counts[0] == 91, f"add: musicinfo counts[0] == 91 (got {m3.counts[0]})")
    check(len(t3.blocks) == 182, f"add: tuning blocks == 182 (got {len(t3.blocks)})")
    check(t3.song_count == 91, f"add: tuning song_count == 91 (got {t3.song_count})")
    new_ids = _music_ids_from_tuning(t3)
    check(len(new_ids) == 91, f"add: tuning has 91 music ids (got {len(new_ids)})")
    # tuning is emitted in bytewise-id order (retail invariant for MusicManager's
    # binary search), so the new *_copy song lands at its sorted position, not the
    # tail -- assert presence, not last-ness.
    check(any(x.endswith("_copy") for x in new_ids),
          f"add: a *_copy id is present (ids sorted, not append order)")
    check(s3.count == 106, f"add: streaminfo count == 106 (got {s3.count})")

    # --- 4) ADD then REMOVE -> back to 90 ----------------------------------- #
    sm = SongManager(mi, tu, si)
    sm.tbl.selectRow(5)
    sm._on_add()
    # remove the newly added row (last row)
    sm.tbl.selectRow(sm.tbl.rowCount() - 1)
    sm._on_remove()
    res = sm._build_result()
    check(res == {}, "add+remove same song returns byte-exact (empty change set)")

    # --- 5) REMOVE existing -> 89 ------------------------------------------ #
    sm = SongManager(mi, tu, si)
    sm.tbl.selectRow(10)
    sm._on_remove()
    res = sm._build_result()
    m5 = MI.parse(res[MUSICINFO])
    t5 = TU.parse(res[TUNING])
    s5 = SI.parse(res[STREAMINFO]) if STREAMINFO in res else SI.parse(si)
    check(len(m5.sec0) == 89, f"remove: musicinfo sec0 == 89 (got {len(m5.sec0)})")
    check(m5.counts[0] == 89, f"remove: musicinfo counts[0] == 89 (got {m5.counts[0]})")
    check(len(t5.blocks) == 178, f"remove: tuning blocks == 178 (got {len(t5.blocks)})")
    check(t5.song_count == 89, f"remove: tuning song_count == 89 (got {t5.song_count})")
    check(s5.count == 104, f"remove: streaminfo count == 104 (got {s5.count})")

    # --- 6) REMOVE song 0 (id token lives in tail) -------------------------- #
    sm = SongManager(mi, tu, si)
    sm.tbl.selectRow(0)
    sm._on_remove()
    res = sm._build_result()
    t6 = TU.parse(res[TUNING])
    check(t6.song_count == 89, f"remove song0: tuning song_count == 89 (got {t6.song_count})")
    ids6 = _music_ids_from_tuning(t6)
    check(len(ids6) == 89, f"remove song0: 89 ids (got {len(ids6)})")
    check(ids6[0] == "1ps", f"remove song0: first id now 1ps (got {ids6[0]!r})")

    # --- 7) ADD two, REMOVE the FIRST-added (regression: token mismatch) ----- #
    # Previously removed the wrong (last-created) token and produced a hybrid
    # record. The remaining song must be a clean clone of source row 40 only.
    sm = SongManager(mi, tu, si)
    src5_id = sm._songs[5].id
    src40_id = sm._songs[40].id
    sm.tbl.selectRow(5); sm._on_add()     # bou_copy   (row 90)
    sm.tbl.selectRow(40); sm._on_add()    # konan3_copy(row 91)
    first_added_id = sm._songs[90].id     # the bou_copy id
    second_added_id = sm._songs[91].id    # the konan3_copy id
    sm.tbl.selectRow(90)                  # select the FIRST-added new row
    sm._on_remove()
    res = sm._build_result()
    m7 = MI.parse(res[MUSICINFO]); t7 = TU.parse(res[TUNING])
    ids7 = _music_ids_from_tuning(t7)
    check(len(m7.sec0) == 91, f"add2/rem-first: sec0 == 91 (got {len(m7.sec0)})")
    check(first_added_id not in ids7,
          f"add2/rem-first: removed id {first_added_id!r} absent")
    check(second_added_id in ids7,
          f"add2/rem-first: surviving id {second_added_id!r} present (got {ids7!r})")
    # the surviving clone must equal source row 40 for every INHERITED column.
    # cols 0/1 now legitimately differ from row40: they point to the new song's
    # OWN id/title appended to the musicinfo pool (gate #8), not the template's
    # offsets -- so verify they RESOLVE to the surviving id / a non-empty title
    # rather than matching row40's raw offset values.
    src40_rec = MI.parse(mi).sec0[40]
    surv_rec = m7.sec0[-1]

    def _mi_str(m, off):
        e = m.pool.find(b"\x00", off)
        return m.pool[off:(e if e >= 0 else len(m.pool))].decode("latin1")
    check(_mi_str(m7, surv_rec[0]) == second_added_id,
          f"add2/rem-first: surviving col0 -> {second_added_id!r} "
          f"(got {_mi_str(m7, surv_rec[0])!r})")
    check(_mi_str(m7, surv_rec[1]) != "",
          "add2/rem-first: surviving col1 -> non-empty title")
    for col in (GENRE_COL, SCORE_COL, 8, 9, 10, 11, 17, 18, 19, 20):
        check(surv_rec[col] == src40_rec[col],
              f"add2/rem-first: surviving sec0 col{col} == row40 "
              f"({surv_rec[col]} vs {src40_rec[col]})")

    # --- 8) ADD three, REMOVE the MIDDLE-added -------------------------------- #
    sm = SongManager(mi, tu, si)
    sm.tbl.selectRow(5); sm._on_add()
    sm.tbl.selectRow(6); sm._on_add()
    sm.tbl.selectRow(7); sm._on_add()
    mid_id = sm._songs[91].id
    sm.tbl.selectRow(91); sm._on_remove()   # remove middle-added
    res = sm._build_result()
    t8 = TU.parse(res[TUNING]); ids8 = _music_ids_from_tuning(t8)
    check(t8.song_count == 92, f"add3/rem-mid: song_count == 92 (got {t8.song_count})")
    check(mid_id not in ids8, f"add3/rem-mid: removed id {mid_id!r} absent")

    # --- 9) builder-style add (no _token stamped) still removable ------------ #
    sm = SongManager(mi, tu, si)
    sm.tbl.selectRow(5); sm._on_add()
    # simulate the external song_builder path which sets k/_template_k/_is_new
    # but not _token; removal must fall back to id-matching.
    sm._songs[90]._token = None
    sm.tbl.selectRow(90); sm._on_remove()
    res = sm._build_result()
    check(res == {}, "builder-style add (no _token) removes byte-exact")

    print()
    print("=== RESULT:", "PASS" if not fails else f"FAIL ({len(fails)})", "===")
    sys.exit(0 if not fails else 1)

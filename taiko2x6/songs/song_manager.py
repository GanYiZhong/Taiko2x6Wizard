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
    QT_QPA_PLATFORM=offscreen python -m taiko2x6.songs.song_manager
"""
from __future__ import annotations

import os
import re
import struct
import sys

# import the (read-only) per-bin parsers ------------------------------------ #
from taiko2x6.bineditors import bineditor_musicinfo as MI
from taiko2x6.bineditors import bineditor_tuning as TU
from taiko2x6.bineditors import bineditor_streaminfo as SI
from taiko2x6.app.i18n import tr
from taiko2x6.app.dialog_polish import polish

from PySide6.QtCore import Qt, QByteArray
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QHeaderView, QMessageBox, QLineEdit, QAbstractItemView,
    QComboBox, QCheckBox, QSpinBox, QStyledItemDelegate, QInputDialog,
    QDialogButtonBox,
)

from taiko2x6.app import appconfig
from taiko2x6.app.i18n import tr


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

COLS = ["idx", "id", "title", "genre", "easy*", "normal*", "hard*", "oni*",
        "score", "score3k", "score5k", "stream"]
COL_IDX, COL_ID, COL_TITLE, COL_GENRE = 0, 1, 2, 3
COL_EASY, COL_NORMAL, COL_HARD, COL_ONI = 4, 5, 6, 7
COL_SCORE, COL_SCORE3, COL_SCORE5, COL_STREAM = 8, 9, 10, 11


def _headers() -> list:
    return [tr("#"), tr("id"), tr("title"), tr("genre"), tr("easy stars"),
            tr("normal stars"), tr("hard stars"), tr("oni stars"), tr("score"),
            tr("score +3k"), tr("score +5k"), tr("stream")]


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


def _t14_place_by_stars(sec0, order, songs, new_songs, tuning) -> None:
    """Slot added songs into the T14 difficulty orders (SEC0 col15 / col16).

    col15 lists every row by hard-course stars, col16 by oni stars (the seven
    folder header rows first). New rows start at the end of both, so they
    would always sort last. Insert each one after the last row with the same
    or lower rating, keep the original rows in their relative order, and
    renumber both permutations.
    """
    try:
        stock = TU.t14_stars(tuning) if TU.is_t14_layout(tuning) else {}
    except Exception:
        stock = {}
    new_rows = []
    stars = {}
    for i, ref in enumerate(order):
        if isinstance(ref, str) and ref.startswith("+"):
            st = new_songs[ref].get("stars")
            if not st or len(st) < 4:
                return                      # nothing to place by
            stars[i] = list(st)
            new_rows.append(i)
        else:
            st = stock.get(songs[i].id)
            if st is None:
                return                      # can't rank the stock rows; leave as is
            stars[i] = st
    if not new_rows:
        return

    def oni(st):
        return st[3] if st[3] > 0 else st[2]

    for col, key in ((15, lambda i: stars[i][2]), (16, lambda i: oni(stars[i]))):
        is_new = set(new_rows)
        seq = sorted((i for i in range(len(sec0)) if i not in is_new),
                     key=lambda i: sec0[i][col])
        headers = sum(1 for i in seq if sec0[i][GENRE_COL] > 6)
        for i in new_rows:
            k = key(i)
            pos = len(seq)
            while pos > headers and key(seq[pos - 1]) > k:
                pos -= 1
            seq.insert(pos, i)
        for p, i in enumerate(seq):
            sec0[i][col] = p


def _tuning_rebuild_t14(tu: bytes, keep_ids: list, new_specs: list) -> bytes:
    """Rebuild a Taiko-14 tuning.bin for an add/remove.

    ``keep_ids`` = bare ids of existing songs to retain; ``new_specs`` = list of
    (new_id, template_id[, stars]). Records are cloned verbatim, new songs get a fresh
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

    for spec in new_specs:
        new_id, tmpl_id = spec[0], spec[1]
        stars = spec[2] if len(spec) > 2 else None
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
        if stars:
            # the clone carries the template's difficulty; use the song's own
            TU.set_t14_stars(rec, stars)
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
                 "stream", "stream_rec", "_template_k", "_is_new", "_token", "title")

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
        self.title = ""                 # display title (musicinfo pool), read-only


# --------------------------------------------------------------------------- #
# the dialog
# --------------------------------------------------------------------------- #
# musicinfo genre folder -> display name (the game's own folder names, like the
# Omnimix summary). 7 and 9..15 are folder-header rows, never a song genre.
GENRE_NAMES = {0: "J-POP", 1: "アニメ", 2: "バラエティ", 3: "クラシック",
               4: "ナムコオリジナル", 5: "どうよう", 6: "ゲームミュージック",
               8: "C-POP"}
STAR_MIN, STAR_MAX = 1, 10
SORT_ROLE = Qt.UserRole + 1          # value the sort compares (int or str)
GENRE_ROLE = Qt.UserRole + 2         # the genre number behind a genre cell
_CHANGED_BG = QColor(255, 226, 140)
_NEW_BG = QColor(200, 235, 200)
_INK = QColor(0, 0, 0)
_PREF_SECTION = "song_manager"


def genre_name(g: int) -> str:
    """Display name of a musicinfo genre number."""
    return GENRE_NAMES.get(g) or tr("genre {g}").format(g=g)


def parse_genre(text) -> int:
    """A genre from a number, a folder name or 'genre N'. ValueError if neither."""
    s = str(text).strip()
    try:
        return int(s)
    except ValueError:
        pass
    low = s.lower()
    for g, name in GENRE_NAMES.items():
        if low == name.lower():
            return g
    m = re.fullmatch(r"(?:genre|類別)\s*(-?\d+)", low)
    if m:
        return int(m.group(1))
    raise ValueError(tr("Unknown genre: {v}").format(v=s))


def parse_stars(text) -> int:
    """A star rating 1..10. ValueError otherwise."""
    try:
        v = int(str(text).strip())
    except ValueError:
        raise ValueError(tr("Stars must be a whole number from 1 to 10."))
    if not STAR_MIN <= v <= STAR_MAX:
        raise ValueError(tr("Stars must be a whole number from 1 to 10."))
    return v


def _decode_title(raw: bytes) -> str:
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin1")


class _Cell(QTableWidgetItem):
    """A cell that sorts by SORT_ROLE (numbers as numbers, text case-blind)."""

    def __lt__(self, other):
        a, b = self.data(SORT_ROLE), other.data(SORT_ROLE)
        if a is not None and b is not None:
            try:
                return a < b
            except TypeError:
                pass
        return super().__lt__(other)


_RO_FLAGS = QTableWidgetItem().flags() & ~Qt.ItemIsEditable   # computed once


class _GenreDelegate(QStyledItemDelegate):
    """Genre cells edit through a drop-down of the named genre folders."""

    def createEditor(self, parent, option, index):
        box = QComboBox(parent)
        for g, name in GENRE_NAMES.items():
            box.addItem(name, g)
        cur = index.data(GENRE_ROLE)
        if isinstance(cur, int) and cur not in GENRE_NAMES:
            box.addItem(genre_name(cur), cur)
        return box

    def setEditorData(self, editor, index):
        i = editor.findData(index.data(GENRE_ROLE))
        if i >= 0:
            editor.setCurrentIndex(i)

    def setModelData(self, editor, model, index):
        model.setData(index, str(editor.currentData()), Qt.EditRole)


class _StarDelegate(QStyledItemDelegate):
    """Star cells edit through a 1..10 spin box."""

    def createEditor(self, parent, option, index):
        box = QSpinBox(parent)
        box.setRange(STAR_MIN, STAR_MAX)
        return box

    def setEditorData(self, editor, index):
        try:
            editor.setValue(int(index.data(Qt.EditRole)))
        except (TypeError, ValueError):
            pass

    def setModelData(self, editor, model, index):
        model.setData(index, str(editor.value()), Qt.EditRole)


class _StarsDialog(QDialog):
    """Pick which difficulties to set and to what, for the selected songs."""

    def __init__(self, parent, n: int):
        super().__init__(parent)
        self.setWindowTitle(tr("Set stars"))
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Set the stars of {n} selected songs. Tick the "
                                "difficulties to change.").format(n=n)))
        self._rows = []
        for nm in (tr("Easy"), tr("Normal"), tr("Hard"), tr("Oni")):
            row = QHBoxLayout()
            cb = QCheckBox(nm)
            sp = QSpinBox()
            sp.setRange(STAR_MIN, STAR_MAX)
            sp.setValue(5)
            row.addWidget(cb, 1)
            row.addWidget(sp)
            lay.addLayout(row)
            self._rows.append((cb, sp))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def values(self) -> dict:
        return {i: sp.value() for i, (cb, sp) in enumerate(self._rows) if cb.isChecked()}


class SongManager(QDialog):
    """Cross-bin song manager.

    After exec(): ``self.result`` is a dict mapping ONLY changed filenames to
    new bytes (e.g. {"musicinfo.bin": b"...", "tuning.bin": b"..."}); ``None``
    if cancelled or nothing changed.
    """

    NO_DEFAULT_REASON = 'table editor: Enter edits cells and the filter box, so Save is an explicit click'

    def __init__(self, mi_bytes: bytes, tu_bytes: bytes, si_bytes: bytes, parent=None,
                 archive=None, host=None):
        super().__init__(parent)
        # optional: the opened archive (+ explorer window) enables the
        # read-only "Song assets" panel
        self._archive = archive
        self._host = host
        self.assets_panel = None
        self.setWindowTitle(tr("Taiko Song Manager (cross-bin)"))
        self.setSizeGripEnabled(True)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.resize(1180, 720)

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
        # T14 value edits are not mapped back (see _serialize_*), so those
        # columns are read-only there instead of silently doing nothing.
        self._t14 = getattr(self.mi, "variant", "") == "t14"
        self._busy = False
        self._uid_songs = {}      # row key -> _Song (rows move when sorted)
        self._uid_of = {}         # id(_Song) -> row key
        self._next_uid = 0
        self._sort_col, self._sort_order = COL_IDX, Qt.AscendingOrder
        self._build_ui()
        polish(self)

    # -- assemble per-song rows from the three models ----------------------- #
    def _title_of(self, k, sid) -> str:
        """The song's display title from the musicinfo pool ('' if unreadable)."""
        try:
            if getattr(self.mi, "variant", "") == "t14":
                k = _mi_row_by_id(self.mi, sid)
            if k is None or not 0 <= k < len(self.mi.sec0):
                return ""
            off = self.mi.sec0[k][1]
            pool = self.mi.pool
            if not 0 <= off < len(pool):
                return ""
            e = pool.find(b"\x00", off)
            return _decode_title(bytes(pool[off:(e if e >= 0 else len(pool))]))
        except Exception:
            return ""

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
            songs[-1].title = self._title_of(k, sid)
        return songs

    # -- ui ----------------------------------------------------------------- #
    def _build_ui(self):
        lay = QVBoxLayout(self)

        info = QLabel(
            tr("{n} songs · musicinfo sec0={a} · tuning blocks={b} · "
               "streaminfo records={c}").format(
                n=len(self._songs), a=len(self.mi.sec0),
                b=len(self.tu.blocks), c=self.si.count)
        )
        info.setStyleSheet("color:palette(mid);font-family:Consolas,monospace;")
        lay.addWidget(info)

        note = QLabel(
            tr("Add duplicates only the DATABASE entry (musicinfo + tuning + "
               "streaminfo). Chart (fumen) / audio / textures are separate and "
               "are NOT created here.")
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:palette(mid);")
        lay.addWidget(note)
        if self._t14:
            n14 = QLabel(tr("This game version only supports adding and removing "
                            "songs here; genre, stars and scores of existing "
                            "songs are read-only."))
            n14.setWordWrap(True)
            lay.addWidget(n14)

        # search / filters
        top = QHBoxLayout()
        top.addWidget(QLabel(tr("Search:")))
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText(tr("id, title, stream or genre (Ctrl+F, Esc to clear)"))
        self.ed_filter.setClearButtonEnabled(True)
        self.ed_filter.textChanged.connect(self._apply_filter)
        top.addWidget(self.ed_filter, 1)
        self.cb_genre = QComboBox()
        self.cb_genre.addItem(tr("All genres"), None)
        for g, name in GENRE_NAMES.items():
            self.cb_genre.addItem(name, g)
        self.cb_genre.currentIndexChanged.connect(self._apply_filter)
        top.addWidget(self.cb_genre)
        self.chk_changed = QCheckBox(tr("Edited only"))
        self.chk_changed.toggled.connect(self._apply_filter)
        top.addWidget(self.chk_changed)
        self.lbl_shown = QLabel("")
        top.addWidget(self.lbl_shown)
        lay.addLayout(top)

        self.tbl = QTableWidget(0, len(COLS))
        self.tbl.setHorizontalHeaderLabels(_headers())
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tbl.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
            | QAbstractItemView.EditKeyPressed
        )
        self.tbl.setSortingEnabled(False)        # sorted on header click only
        self.tbl.setItemDelegateForColumn(COL_GENRE, _GenreDelegate(self.tbl))
        for c in (COL_EASY, COL_NORMAL, COL_HARD, COL_ONI):
            self.tbl.setItemDelegateForColumn(c, _StarDelegate(self.tbl))
        self.tbl.setRowCount(len(self._songs))
        self._busy = True
        for r, song in enumerate(self._songs):
            self._fill_row(r, song)
        self._busy = False
        hdr = self.tbl.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(True)
        hdr.setSectionsClickable(True)
        hdr.setSortIndicatorShown(True)
        hdr.setSortIndicator(COL_IDX, Qt.AscendingOrder)
        self.tbl.resizeColumnsToContents()
        self.tbl.setColumnWidth(COL_TITLE, max(self.tbl.columnWidth(COL_TITLE), 180))
        hdr.sectionClicked.connect(self._on_header_clicked)
        self.tbl.itemChanged.connect(self._on_item_changed)
        self.tbl.itemSelectionChanged.connect(self._update_buttons)
        lay.addWidget(self.tbl, 1)

        if self._archive is not None:
            from taiko2x6.songs.song_assets import SongAssetsPanel
            self.assets_panel = SongAssetsPanel(self._archive, self._host)
            lay.addWidget(self.assets_panel)
            self.tbl.itemSelectionChanged.connect(self._update_assets)

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        lay.addWidget(self.lbl_status)
        self.lbl_changes = QLabel("")
        lay.addWidget(self.lbl_changes)

        bulk = QHBoxLayout()
        self.b_bulk_genre = QPushButton(tr("Set genre of selected…"))
        self.b_bulk_genre.clicked.connect(self._on_bulk_genre)
        self.b_bulk_stars = QPushButton(tr("Set stars of selected…"))
        self.b_bulk_stars.clicked.connect(self._on_bulk_stars)
        self.b_revert_sel = QPushButton(tr("Revert selected"))
        self.b_revert_sel.setToolTip(tr("Put the selected songs back to the values they were opened with."))
        self.b_revert_sel.clicked.connect(self.revert_selected)
        self.b_revert_all = QPushButton(tr("Revert all edits"))
        self.b_revert_all.clicked.connect(self._on_revert_all)
        for b in (self.b_bulk_genre, self.b_bulk_stars, self.b_revert_sel, self.b_revert_all):
            bulk.addWidget(b)
        bulk.addStretch(1)
        lay.addLayout(bulk)

        btns = QHBoxLayout()
        b_add = QPushButton(tr("Add (duplicate selected)"))
        b_add.setToolTip(tr("Duplicate the selected song's DATABASE entry only "
                            "(no chart/audio/textures)."))
        b_add.clicked.connect(self._on_add)
        b_rem = QPushButton(tr("Remove selected"))
        b_rem.clicked.connect(self._on_remove)
        btns.addWidget(b_add)
        btns.addWidget(b_rem)
        btns.addStretch(1)
        b_save = QPushButton(tr("Save"))
        b_save.clicked.connect(self._on_save)
        b_cancel = QPushButton(tr("Cancel"))
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_save)
        btns.addWidget(b_cancel)
        lay.addLayout(btns)

        QShortcut(QKeySequence.Find, self, activated=self._focus_search)
        QShortcut(QKeySequence(Qt.Key_Escape), self.ed_filter,
                  activated=self._escape_in_search, context=Qt.WidgetShortcut)
        self.setTabOrder(self.ed_filter, self.cb_genre)
        self.setTabOrder(self.cb_genre, self.chk_changed)
        self.setTabOrder(self.chk_changed, self.tbl)
        self._restore_prefs()
        self._apply_filter()
        self._update_buttons()
        self._update_summary()

    def _focus_search(self):
        self.ed_filter.setFocus()
        self.ed_filter.selectAll()

    def _escape_in_search(self):
        if self.ed_filter.text():
            self.ed_filter.clear()
        else:
            self.reject()           # empty search: Esc closes, as everywhere

    # -- rows --------------------------------------------------------------- #
    def _uid_for(self, song) -> int:
        uid = self._uid_of.get(id(song))
        if uid is None:
            uid = self._next_uid
            self._next_uid += 1
            self._uid_of[id(song)] = uid
            self._uid_songs[uid] = song
        return uid

    def _song_for_row(self, r):
        it = self.tbl.item(r, COL_IDX)
        if it is None:
            return None
        return self._uid_songs.get(it.data(Qt.UserRole))

    def _row_of(self, song):
        uid = self._uid_of.get(id(song))
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, COL_IDX)
            if it is not None and it.data(Qt.UserRole) == uid:
                return r
        return None

    def _is_new(self, song) -> bool:
        return bool(getattr(song, "_is_new", False)) or song.k is None

    def _cell_editable(self, song, col) -> bool:
        if col in (COL_IDX, COL_TITLE):
            return False
        new = self._is_new(song)
        if col == COL_ID:
            return new
        if col == COL_STREAM:
            return True
        if self._t14:
            return new and col in (COL_EASY, COL_NORMAL, COL_HARD, COL_ONI)
        return True

    def _cell_value(self, song, col):
        """(display text, sort key) of one cell."""
        if col == COL_IDX:
            return ("+", 10 ** 9) if self._is_new(song) else (str(song.k), song.k)
        if col == COL_ID:
            return song.id, song.id.lower()
        if col == COL_TITLE:
            t = getattr(song, "title", "") or ""
            return t, t.lower()
        if col == COL_GENRE:
            return genre_name(song.genre), song.genre
        if COL_EASY <= col <= COL_ONI:
            v = song.stars[col - COL_EASY]
            return str(v), v
        if col == COL_SCORE:
            return str(song.score), song.score
        if col == COL_SCORE3:
            return str(song.score3), song.score3
        if col == COL_SCORE5:
            return str(song.score5), song.score5
        return song.stream, song.stream.lower()

    def _fill_row(self, r, song: _Song):
        """Create row ``r``'s cells straight from ``song`` (same end state as
        creating blank cells and calling _refresh_row, without the second
        pass over every cell)."""
        uid = self._uid_for(song)
        changed = self._changed_cols(song)
        new = self._is_new(song)
        setItem = self.tbl.setItem
        for col in range(len(COLS)):
            it = _Cell()
            if not self._cell_editable(song, col):
                it.setFlags(_RO_FLAGS)
            if col == COL_IDX:
                it.setData(Qt.UserRole, uid)
            text, key = self._cell_value(song, col)
            it.setText(text)
            it.setData(SORT_ROLE, key)
            if col == COL_GENRE:
                it.setData(GENRE_ROLE, song.genre)
            if new:
                it.setData(Qt.BackgroundRole, _NEW_BG)
                it.setData(Qt.ForegroundRole, _INK)
            elif col in changed:
                it.setData(Qt.BackgroundRole, _CHANGED_BG)
                it.setData(Qt.ForegroundRole, _INK)
            setItem(r, col, it)

    def _refresh_row(self, r, song):
        """Rewrite one row's cells in place from ``song`` and mark edits."""
        was = self._busy
        self._busy = True
        try:
            changed = self._changed_cols(song)
            new = self._is_new(song)
            for col in range(len(COLS)):
                it = self.tbl.item(r, col)
                if it is None:
                    continue
                text, key = self._cell_value(song, col)
                it.setText(text)
                it.setData(SORT_ROLE, key)
                if col == COL_GENRE:
                    it.setData(GENRE_ROLE, song.genre)
                if new:
                    it.setData(Qt.BackgroundRole, _NEW_BG)
                    it.setData(Qt.ForegroundRole, _INK)
                elif col in changed:
                    it.setData(Qt.BackgroundRole, _CHANGED_BG)
                    it.setData(Qt.ForegroundRole, _INK)
                else:
                    it.setData(Qt.BackgroundRole, None)
                    it.setData(Qt.ForegroundRole, None)
        finally:
            self._busy = was

    def _changed_cols(self, song) -> set:
        """Columns whose value differs from what the song was opened with."""
        if self._is_new(song):
            return set()
        o = self._orig_songs.get(song.k)
        if o is None:
            return set()
        out = set()
        if song.genre != o.genre:
            out.add(COL_GENRE)
        for i in range(4):
            if song.stars[i] != o.stars[i]:
                out.add(COL_EASY + i)
        if song.score != o.score:
            out.add(COL_SCORE)
        if song.score3 != o.score3:
            out.add(COL_SCORE3)
        if song.score5 != o.score5:
            out.add(COL_SCORE5)
        if song.stream != o.stream:
            out.add(COL_STREAM)
        if song.id != o.id:
            out.add(COL_ID)
        return out

    def _is_edited(self, song) -> bool:
        return self._is_new(song) or bool(self._changed_cols(song))

    # -- search / filter ---------------------------------------------------- #
    def _row_visible(self, song, text, genre, edited_only) -> bool:
        if genre is not None and song.genre != genre:
            return False
        if edited_only and not self._is_edited(song):
            return False
        if text:
            hay = " ".join((song.id, getattr(song, "title", "") or "", song.stream,
                            genre_name(song.genre))).lower()
            if text not in hay:
                return False
        return True

    def _apply_filter(self, *_):
        text = self.ed_filter.text().strip().lower()
        genre = self.cb_genre.currentData()
        edited_only = self.chk_changed.isChecked()
        shown = 0
        for r in range(self.tbl.rowCount()):
            song = self._song_for_row(r)
            vis = song is None or self._row_visible(song, text, genre, edited_only)
            self.tbl.setRowHidden(r, not vis)
            shown += bool(vis)
        self.lbl_shown.setText(tr("{shown} of {total} shown").format(
            shown=shown, total=self.tbl.rowCount()))

    # -- sorting ------------------------------------------------------------ #
    def _on_header_clicked(self, col):
        if col == self._sort_col:
            order = (Qt.DescendingOrder if self._sort_order == Qt.AscendingOrder
                     else Qt.AscendingOrder)
        else:
            order = Qt.AscendingOrder
        self.sort_by(col, order)

    def sort_by(self, col, order=Qt.AscendingOrder):
        self._sort_col, self._sort_order = col, order
        self.tbl.horizontalHeader().setSortIndicator(col, order)
        self.tbl.sortItems(col, order)
        self._apply_filter()                    # row-hidden flags do not follow items

    # -- validation / write-through ---------------------------------------- #
    def _set_status(self, text, error=False):
        self.lbl_status.setStyleSheet("color: palette(bright-text);" if error else "")
        self.lbl_status.setText(text)

    def _apply_cell(self, song, col, text):
        """Validate ``text`` for one cell and store it on ``song``. ValueError if bad."""
        if col == COL_ID:
            sid = text.strip()
            if not sid:
                raise ValueError(tr("Song id must not be empty."))
            if any(s is not song and s.id == sid for s in self._songs):
                raise ValueError(tr("Another song already uses the id {id}.").format(id=sid))
            song.id = sid
        elif col == COL_GENRE:
            song.genre = parse_genre(text)
        elif COL_EASY <= col <= COL_ONI:
            song.stars[col - COL_EASY] = parse_stars(text)
        elif col in (COL_SCORE, COL_SCORE3, COL_SCORE5):
            try:
                v = int(str(text).strip())
            except ValueError:
                raise ValueError(tr("Not a whole number: {v}").format(v=text))
            setattr(song, {COL_SCORE: "score", COL_SCORE3: "score3",
                           COL_SCORE5: "score5"}[col], v)
        elif col == COL_STREAM:
            song.stream = text.strip()

    def _on_item_changed(self, it):
        if self._busy:
            return
        r, col = it.row(), it.column()
        song = self._song_for_row(r)
        if song is None or col in (COL_IDX, COL_TITLE):
            return
        try:
            self._apply_cell(song, col, it.text())
        except ValueError as exc:
            self._set_status(str(exc), error=True)
        else:
            self._set_status("")
        self._refresh_row(r, song)             # canonical text; bad input snaps back
        self._after_edit()

    def _after_edit(self):
        self._update_summary()
        if self.chk_changed.isChecked():
            self._apply_filter()

    def _update_summary(self):
        edited = sum(1 for s in self._songs
                     if not self._is_new(s) and self._changed_cols(s))
        added = sum(1 for s in self._songs if self._is_new(s))
        kept = {s.k for s in self._songs if not self._is_new(s)}
        removed = sum(1 for k in self._orig_songs if k not in kept)
        self.lbl_changes.setText(
            tr("Edited: {e} · added: {a} · removed: {r}").format(e=edited, a=added, r=removed))
        self.b_revert_all.setEnabled(edited > 0)

    # -- read the table back into _songs ------------------------------------ #
    def _harvest_table(self):
        """Pull current cell values into the _songs list. Raises on bad ints."""
        for r in range(self.tbl.rowCount()):
            song = self._song_for_row(r)
            if song is None:
                continue
            txt = lambda c: self.tbl.item(r, c).text()
            song.id = txt(COL_ID).strip()
            song.genre = parse_genre(txt(COL_GENRE))
            song.stars = [int(txt(c)) for c in (COL_EASY, COL_NORMAL, COL_HARD, COL_ONI)]
            song.score = int(txt(COL_SCORE))
            song.score3 = int(txt(COL_SCORE3))
            song.score5 = int(txt(COL_SCORE5))
            song.stream = txt(COL_STREAM).strip()

        # re-validate ids: post-hoc table edits bypass the add-time uniqueness /
        # non-empty checks, and a blank/duplicate id silently corrupts the
        # music_<id> pool entries and tuning stems on a structural save.
        seen = {}
        for r in range(self.tbl.rowCount()):
            song = self._song_for_row(r)
            if song is None:
                continue
            sid = song.id
            if not sid:
                raise ValueError(tr("row {r}: song id must not be empty").format(r=r))
            if sid in seen:
                raise ValueError(tr("row {r}: duplicate song id {sid} (also row {o})")
                                 .format(r=r, sid=repr(sid), o=seen[sid]))
            seen[sid] = r

    # -- selection ----------------------------------------------------------- #
    def _selected_row(self):
        rows = self.tbl.selectionModel().selectedRows()
        if not rows:
            sel = self.tbl.currentRow()
            return sel if sel >= 0 else None
        return rows[0].row()

    def _selected_songs(self) -> list:
        out = []
        for ix in self.tbl.selectionModel().selectedRows():
            if self.tbl.isRowHidden(ix.row()):
                continue
            s = self._song_for_row(ix.row())
            if s is not None:
                out.append(s)
        return out

    def _update_assets(self):
        if self.assets_panel is None:
            return
        sel = self._selected_songs()
        if len(sel) == 1:
            self.assets_panel.set_song(sel[0].id, sel[0].stars)   # _Song.id
        else:
            self.assets_panel.set_song(None)

    def select_song(self, sid: str) -> bool:
        """Select (and scroll to) the row of song id ``sid``; False if absent."""
        if self.ed_filter.text() or self.chk_changed.isChecked() \
                or self.cb_genre.currentIndex() != 0:
            self.ed_filter.clear()
            self.chk_changed.setChecked(False)
            self.cb_genre.setCurrentIndex(0)
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, COL_ID)
            if it is not None and it.text() == sid and not self.tbl.isRowHidden(r):
                self.tbl.selectRow(r)
                self.tbl.scrollToItem(it)
                return True
        return False

    def _update_buttons(self):
        n = len(self._selected_songs())
        self.b_bulk_genre.setEnabled(n > 0 and not self._t14)
        self.b_bulk_stars.setEnabled(n > 0)
        self.b_revert_sel.setEnabled(n > 0)

    # -- bulk edits / revert ------------------------------------------------- #
    def bulk_set_genre(self, songs, genre: int) -> int:
        """Set the genre of ``songs``; returns how many changed."""
        n = 0
        for s in songs:
            if not self._cell_editable(s, COL_GENRE) or s.genre == genre:
                continue
            s.genre = genre
            n += 1
        self._refresh_all()
        return n

    def bulk_set_stars(self, songs, values: dict) -> int:
        """Set stars ({difficulty index: 1..10}) on ``songs``; returns songs changed."""
        vals = {di: parse_stars(v) for di, v in values.items() if 0 <= di < 4}
        n = 0
        for s in songs:
            hit = False
            for di, v in vals.items():
                if self._cell_editable(s, COL_EASY + di) and s.stars[di] != v:
                    s.stars[di] = v
                    hit = True
            n += hit
        self._refresh_all()
        return n

    def revert_songs(self, songs) -> int:
        """Put songs back to their opened values (added songs are left alone)."""
        n = 0
        for s in songs:
            o = None if self._is_new(s) else self._orig_songs.get(s.k)
            if o is None or not self._changed_cols(s):
                continue
            s.id, s.genre, s.stars = o.id, o.genre, list(o.stars)
            s.score, s.score3, s.score5, s.stream = o.score, o.score3, o.score5, o.stream
            n += 1
        self._refresh_all()
        return n

    def revert_selected(self):
        n = self.revert_songs(self._selected_songs())
        self._set_status(tr("Reverted {n} songs.").format(n=n))

    def _on_revert_all(self):
        edited = [s for s in self._songs if not self._is_new(s) and self._changed_cols(s)]
        if not edited:
            return
        if QMessageBox.question(
                self, tr("Revert all edits"),
                tr("Put {n} edited songs back to the values they were opened "
                   "with? Added and removed songs are not affected.").format(n=len(edited))
        ) != QMessageBox.Yes:
            return
        self._set_status(tr("Reverted {n} songs.").format(n=self.revert_songs(edited)))

    def _refresh_all(self):
        for r in range(self.tbl.rowCount()):
            s = self._song_for_row(r)
            if s is not None:
                self._refresh_row(r, s)
        self._update_summary()
        if self.chk_changed.isChecked():
            self._apply_filter()

    def _on_bulk_genre(self):
        songs = self._selected_songs()
        if not songs:
            return
        names = list(GENRE_NAMES.values())
        name, ok = QInputDialog.getItem(
            self, tr("Set genre"),
            tr("Genre for {n} selected songs:").format(n=len(songs)), names, 0, False)
        if not ok:
            return
        genre = next(g for g, nm in GENRE_NAMES.items() if nm == name)
        if QMessageBox.question(
                self, tr("Set genre"),
                tr("Set the genre of {n} songs to {g}?").format(n=len(songs), g=name)
        ) != QMessageBox.Yes:
            return
        n = self.bulk_set_genre(songs, genre)
        self._set_status(tr("Changed the genre of {n} songs.").format(n=n))

    def _on_bulk_stars(self):
        songs = self._selected_songs()
        if not songs:
            return
        dlg = _StarsDialog(self, len(songs))
        ok = dlg.exec()
        vals = dlg.values()
        dlg.deleteLater()
        if not ok or not vals:
            return
        if QMessageBox.question(
                self, tr("Set stars"),
                tr("Change the stars of {n} songs?").format(n=len(songs))
        ) != QMessageBox.Yes:
            return
        n = self.bulk_set_stars(songs, vals)
        self._set_status(tr("Changed the stars of {n} songs.").format(n=n))

    # -- remembered layout ---------------------------------------------------- #
    def _restore_prefs(self):
        try:
            raw = appconfig.get(_PREF_SECTION, "header", "")
            if raw:
                hdr = self.tbl.horizontalHeader()
                hdr.restoreState(QByteArray.fromBase64(raw.encode("ascii")))
                hdr.setStretchLastSection(True)
                col = hdr.sortIndicatorSection()
                if 0 <= col < len(COLS):
                    order = hdr.sortIndicatorOrder()
                    if (col, order) != (COL_IDX, Qt.AscendingOrder):
                        self._sort_col, self._sort_order = col, order
                        self.tbl.sortItems(col, order)
        except Exception:
            pass                                 # a bad saved layout is just ignored

    def _save_prefs(self):
        try:
            state = bytes(self.tbl.horizontalHeader().saveState().toBase64()).decode("ascii")
            appconfig.set_value(_PREF_SECTION, "header", state)
        except Exception:
            pass

    def done(self, code):
        if self.assets_panel is not None:
            self.assets_panel.close_previews()
        self._save_prefs()
        super().done(code)

    def reject(self):
        if self.isVisible():
            kept = {s.k for s in self._songs if not self._is_new(s)}
            n = sum(1 for s in self._songs if self._is_edited(s))
            n += sum(1 for k in self._orig_songs if k not in kept)
            if n and QMessageBox.question(
                    self, tr("Discard changes"),
                    tr("Close without saving? Your edits to the song list "
                       "will be lost.")) != QMessageBox.Yes:
                return
        super().reject()

    # -- add / remove ------------------------------------------------------- #
    def _on_add(self):
        try:
            self._harvest_table()
        except Exception as exc:
            QMessageBox.critical(self, tr("Edit error"), str(exc))
            return
        r = self._selected_row()
        src = self._song_for_row(r) if r is not None else None
        if src is None:
            QMessageBox.information(self, tr("Add"), tr("Select a song to duplicate."))
            return

        # unique id
        existing = {s.id for s in self._songs}
        new_id = f"{src.id}_copy"
        n = 1
        while new_id in existing:
            n += 1
            new_id = f"{src.id}_copy{n}"

        # Duplicating a song added in this session: it has no original row
        # (k is None), so clone from the ORIGINAL row it was itself cloned from.
        if getattr(src, "_is_new", False):
            src_spec = self._new_songs.get(src._token, {})
            tmpl_k = src._template_k
            tmpl_id = src_spec.get("template_id", src.id)
        else:
            tmpl_k = src.k
            tmpl_id = src.id

        token = f"+{self._new_counter}"
        self._new_counter += 1
        self._new_songs[token] = {
            "template_k": tmpl_k,
            "template_id": tmpl_id,  # used by the T14 tuning rebuild (order differs)
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
        new_song.title = getattr(src, "title", "")
        # tag the source musicinfo/stream template index for save-time cloning
        new_song._template_k = tmpl_k         # type: ignore[attr-defined]
        new_song._is_new = True               # type: ignore[attr-defined]
        new_song._token = token               # type: ignore[attr-defined]
        self._songs.append(new_song)

        r2 = self.tbl.rowCount()
        self._busy = True
        self.tbl.insertRow(r2)
        self._fill_row(r2, new_song)          # idx column shows "+" for new songs
        self._busy = False
        self._apply_filter()
        self.tbl.scrollToItem(self.tbl.item(r2, COL_ID))
        self._update_summary()
        self._set_status(tr("Added {id}. Its id and stars can be edited.").format(id=new_id))

    def _remove_one(self, song):
        """Drop one song from the working order / song list (not the table)."""
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
        for i, s in enumerate(self._songs):
            if s is song:
                del self._songs[i]
                break
        uid = self._uid_of.pop(id(song), None)
        self._uid_songs.pop(uid, None)

    def _on_remove(self):
        try:
            self._harvest_table()
        except Exception as exc:
            QMessageBox.critical(self, tr("Edit error"), str(exc))
            return
        victims = self._selected_songs()
        if not victims:
            r = self._selected_row()
            s = self._song_for_row(r) if r is not None else None
            victims = [s] if s is not None else []
        if not victims:
            QMessageBox.information(self, tr("Remove"), tr("Select a song to remove."))
            return
        if len(self._songs) <= len(victims):
            QMessageBox.warning(self, tr("Remove"), tr("Cannot remove the last song."))
            return
        if len(victims) > 1 and QMessageBox.question(
                self, tr("Remove"),
                tr("Remove {n} songs from the database?").format(n=len(victims))
        ) != QMessageBox.Yes:
            return
        for song in victims:
            r = self._row_of(song)
            self._remove_one(song)
            if r is not None:
                self.tbl.removeRow(r)
        self._apply_filter()
        self._update_summary()


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
                    # col2 = genre folder. A cloned row inherits the TEMPLATE's
                    # genre, so without this EVERY added song lands in the
                    # template's folder. Use the song's real source genre when the
                    # spec carries one (clamped to the 7 real folders 0..6; 7/9-15
                    # are header-row markers, never a song genre).
                    g = self._new_songs[ref].get("genre")
                    if g is not None:
                        g = int(g)
                        rec[GENRE_COL] = g if g == 8 else max(0, min(6, g))   # 8 = C-POP
            _t14_place_by_stars(new_sec0, self._order, self._songs,
                                self._new_songs, self._tu_in)

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
                new_specs.append((song.id, spec.get("template_id", ""),
                                  list(song.stars)))
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
        # ``ref`` is a MUSICINFO index (from _order); on T14 the musicinfo row
        # order differs from tuning id order, so ``ids[ref]`` (a TUNING id) names
        # the WRONG song. A kept song's stream is music_<its musicinfo id>, so
        # resolve the name from the musicinfo pool for T14. (T8 is parallel, so
        # _mi_id(ref) == ids[ref] there — keep ids[ref] to stay byte-exact.)
        is_t14 = _is_t14_tuning(self._tu_in)
        mipool = getattr(self.mi, "pool", None)
        def _mi_id(k):
            o = self.mi.sec0[k][0]
            e = mipool.find(b"\x00", o)
            return bytes(mipool[o:e if e >= 0 else len(mipool)]).decode("latin-1")
        def _keep_id(k):
            if is_t14 and mipool is not None and 0 <= k < len(self.mi.sec0):
                return _mi_id(k)
            return ids[k] if k < len(ids) else None
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
                kid = _keep_id(ref)
                orig_name = f"music_{kid}" if kid is not None else None
                if orig_name and orig_name in by_name:
                    keep_names.add(orig_name)
        return _stream_rebuild(s, keep_names, add_specs)


# --------------------------------------------------------------------------- #
# self-test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from taiko2x6.app import apppaths
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    BINS = os.environ.get(
        "TAIKO_BINS",
        str(apppaths.samples_dir() / "bins"),
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

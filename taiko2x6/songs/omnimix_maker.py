#!/usr/bin/env python3
"""
Omnimix Maker — fuse the songs of several Taiko images into one target.

Point it at N source archives (Taiko 7 … 14+) and a target (e.g. Taiko 14). It
harvests every song the target does NOT already have — charts, name textures,
audio and difficulty stars — and appends them into the target's DATA.000 in one
rebuild, then (for an HDD .img target) lifts the song-count ceiling in the game
executable so they are all reachable.

Why this works with what we already proved:
  * Taiko 8…14 share the SYSTEM256/2x6 container (LIST.BIN + DATA.000, fmt 2)
    and the same sht / TIM2 / VAG asset formats, so a song's chart/texture/audio
    groups can be copied VERBATIM between versions — no re-encoding, no loss of
    the original art. Only the song-DB bins (musicinfo/tuning/streaminfo) differ
    per generation, and SongManager already reads/writes each variant.
  * Taiko 7 is the exception: its charts use an older 112-byte measure record
    (integer time/BPM, no scroll speeds) that T8+ misreads, so harvest_song
    converts them (charts.sht_t7; notes are unchanged), and its tuning.bin
    stars are read with the T7 record layout (bineditor_tuning.t7_stars).
    Audio, textures and the container are copied as-is.
  * We only ever add ids the target lacks (dedup by song id, "14 is the base"),
    so group names never collide and nothing existing is touched.
  * The DB add is the proven batch path: one SongManager takes every add, one
    archive_builder.build_archive emits the merged archive, and the three DB
    bins stay consistent (musicinfo == tuning == streaminfo song count).
  * taiko_exe.patch_hdd raises the executable's song ceiling to the new count.

Song-count reality (see taiko_exe / song_tables): ≤214 uses the arrayB patch,
proven in-game. Above 214 taiko_exe relocates arrayA AND song_tables grows the
per-song record tables (score/crown/play-count data, popularity ranking) that
are otherwise fixed at 210 and get overrun by every song id ≥ 210. Max 644.
Records of songs past 210 work in play but are not kept across a reboot (the
SRAM save is full); the first 210 are saved exactly as before.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("taiko.omnimix")

_TEX_TYPES = ("games", "kenri_song", "result", "songlevel", "topten",
              "total_result")
SAFE_LIMIT = 214            # proven-sound ceiling (arrayB relocation)

# fumen.<id><1p|2p>_<e|n|h|m|x>  — the per-song chart groups
_FUMEN_RE = re.compile(r"^fumen\.(?P<id>.+?)(?P<pl>1p|2p)_(?P<d>[enhmx])$")


# --------------------------------------------------------------------------- #
#  opening sources / targets
# --------------------------------------------------------------------------- #
def open_pair(list_path, data_path, fmt=2):
    """Open a (LIST.BIN, DATA.000) pair as an Archive."""
    from taiko2x6.archive import archive_model as gui
    return gui.Archive(str(list_path), str(data_path), fmt=fmt)


# --------------------------------------------------------------------------- #
#  HDD .img sources / targets (extract a PFS partition to a temp pair)
# --------------------------------------------------------------------------- #
def taiko_partitions(img) -> list:
    """PFS partition names in `img` that could hold a Taiko archive."""
    from taiko2x6.ps2 import ps2hdd
    h = ps2hdd.Ps2Hdd(str(img))
    try:
        return [p["name"] for p in h.partitions()
                if p.get("is_pfs") and not p["name"].startswith("__")]
    finally:
        try:
            h.close()
        except Exception:
            pass


def extract_partition(img, partition, tmpdir, log=lambda s: None) -> tuple:
    """Read /list.bin + /DATA.000 out of an .img partition to temp files.

    Returns (list_path, data_path). PCSX2 (or anything holding the .img open)
    must be closed first or the read is fine but a later write-back is denied.
    """
    from taiko2x6.ps2 import ps2hdd
    tmpdir = Path(tmpdir)
    tmpdir.mkdir(parents=True, exist_ok=True)
    lp = tmpdir / "list.bin"
    dp = tmpdir / "DATA.000"
    h = ps2hdd.Ps2Hdd(str(img))
    try:
        log(f"extracting {partition}/list.bin + DATA.000 from {Path(img).name}…")
        lp.write_bytes(h.pfs_read(partition, "/list.bin"))
        dp.write_bytes(h.pfs_read(partition, "/DATA.000"))
    finally:
        try:
            h.close()
        except Exception:
            pass
    log(f"extracted list.bin {lp.stat().st_size:,} B, DATA.000 {dp.stat().st_size:,} B")
    return lp, dp


# --------------------------------------------------------------------------- #
#  ISO 9660 sources (a single-game Taiko disc: DATA.000 + LIST.BIN in the root)
# --------------------------------------------------------------------------- #
_ISO_SEC = 2048


def _iso_root_records(f) -> dict:
    """{name.upper(): (lba, size)} for the files in an ISO's root directory."""
    import struct
    f.seek(16 * _ISO_SEC)
    pvd = f.read(_ISO_SEC)
    if pvd[1:6] != b"CD001":
        raise ValueError("not an ISO 9660 image (no CD001 at sector 16)")
    root_lba = struct.unpack_from("<I", pvd, 156 + 2)[0]
    root_size = struct.unpack_from("<I", pvd, 156 + 10)[0]
    f.seek(root_lba * _ISO_SEC)
    buf = f.read(((root_size + _ISO_SEC - 1) // _ISO_SEC) * _ISO_SEC)
    recs = {}
    off = 0
    while off < len(buf):
        rlen = buf[off]
        if rlen == 0:                       # padding to the next sector
            off = ((off // _ISO_SEC) + 1) * _ISO_SEC
            continue
        lba = struct.unpack_from("<I", buf, off + 2)[0]
        size = struct.unpack_from("<I", buf, off + 10)[0]
        flags = buf[off + 25]
        nlen = buf[off + 32]
        name = buf[off + 33:off + 33 + nlen]
        if not (nlen == 1 and name in (b"\x00", b"\x01")) and not (flags & 2):
            nm = name.split(b";")[0].decode("latin1", "replace").upper()
            recs[nm] = (lba, size)
        off += rlen
    return recs


def iso_extract_pair(iso, tmpdir, log=lambda s: None) -> tuple:
    """Extract DATA.000 + LIST.BIN from a single-game Taiko ISO to temp files.

    Returns (list_path, data_path). Reads the two files straight out of the
    ISO 9660 root directory by LBA/size — no external tools.
    """
    import struct  # noqa: F401  (kept for symmetry; _iso_root_records uses it)
    tmpdir = Path(tmpdir)
    tmpdir.mkdir(parents=True, exist_ok=True)
    lp = tmpdir / "list.bin"
    dp = tmpdir / "DATA.000"
    with open(iso, "rb") as f:
        recs = _iso_root_records(f)
        if "DATA.000" not in recs or "LIST.BIN" not in recs:
            raise ValueError(
                f"{Path(iso).name}: no DATA.000 + LIST.BIN in the ISO root "
                f"(found {sorted(recs)[:8]}…)")
        log(f"extracting DATA.000 + LIST.BIN from {Path(iso).name}…")
        for name, out in (("LIST.BIN", lp), ("DATA.000", dp)):
            lba, size = recs[name]
            f.seek(lba * _ISO_SEC)
            remaining = size
            with open(out, "wb") as w:
                while remaining > 0:
                    chunk = f.read(min(1 << 22, remaining))
                    if not chunk:
                        break
                    w.write(chunk)
                    remaining -= len(chunk)
    log(f"extracted list.bin {lp.stat().st_size:,} B, DATA.000 {dp.stat().st_size:,} B")
    return lp, dp


def write_partition(img, partition, list_bytes, data_bytes, log=lambda s: None,
                    undo=True):
    """Write the merged DATA.000 + list.bin back into an .img partition.

    Both writes run in one undo-journaled session (ps2hdd.write_session): if
    either raises, the partition is rolled back to its old pair. undo=False
    skips the journal when a full image backup was just taken.
    """
    from taiko2x6.ps2 import ps2hdd
    check_data_size(data_bytes, log)
    with ps2hdd.write_session(str(img), undo=undo, log=log) as h:
        log(f"writing DATA.000 {len(data_bytes):,} B → {partition}…")
        h.pfs_write(partition, "/DATA.000", data_bytes)
        log(f"writing list.bin {len(list_bytes):,} B → {partition}…")
        h.pfs_write(partition, "/list.bin", list_bytes)


# The game opens PFS:DATA.000 in a retry loop (sub_104998) that never gives
# up; the PS2 PFS driver refuses a file of 2 GiB or more, so the game hangs on
# its LOADING screen forever. In PCSX2x6 2,131,183,680 B boots and
# 2,145,599,552 B already hangs, so the real limit sits a little below 2 GiB;
# the largest size seen booting is used as the cap.
MAX_DATA_SIZE = 2_131_183_680


class DataTooLargeError(ValueError):
    pass


def check_data_size(data_bytes, log=lambda s: None):
    n = len(data_bytes)
    if n > MAX_DATA_SIZE:
        raise DataTooLargeError(
            f"DATA.000 would be {n:,} B — above {MAX_DATA_SIZE:,} B the game "
            f"cannot open DATA.000 (limit just under 2 GiB) and hangs on "
            f"LOADING. Add fewer songs or re-encode the audio smaller.")
    log(f"DATA.000 {n:,} B ({n / MAX_DATA_SIZE:.1%} of the {MAX_DATA_SIZE:,} B limit)")


_ENOSPC_RE = re.compile(r"need up to (\d+) zones.*?only (\d+) free")


def write_partition_growing(img, partition, list_bytes, data_bytes,
                            log=lambda s: None, undo=True):
    """write_partition, but if the PFS partition is too small, GROW it (append
    an APA sub-partition) and retry — so a merged DATA.000 bigger than the
    partition still fits. The _grow allocator verifies free space before any
    device write, so an ENOSPC leaves the partition untouched and the retry is
    safe. Enlarges the .img file when it grows.
    """
    from taiko2x6.ps2 import ps2hdd
    check_data_size(data_bytes, log)
    for attempt in (1, 2):
        # each attempt is its own undo-journaled session: an error in the
        # final attempt rolls that attempt back (a grow from attempt 1 stays;
        # it only adds unused space to the partition).
        with ps2hdd.write_session(str(img), undo=undo, log=log) as h:
            try:
                log(f"writing DATA.000 {len(data_bytes):,} B → {partition}…")
                h.pfs_write(partition, "/DATA.000", data_bytes)
                log(f"writing list.bin {len(list_bytes):,} B → {partition}…")
                h.pfs_write(partition, "/list.bin", list_bytes)
                return
            except OSError as exc:
                m = _ENOSPC_RE.search(str(exc))
                if not m or attempt == 2:
                    raise
                need, free = int(m.group(1)), int(m.group(2))
                deficit = (need - free) * 8192
                log("partition full (need %d, free %d zones) - growing it by "
                    ">=%.0f MiB and retrying..." % (need, free, deficit / 1048576))
                h.grow_partition(partition, deficit, log=log)


# --------------------------------------------------------------------------- #
#  song-count ceiling in the game executable
# --------------------------------------------------------------------------- #
def patch_song_limit(img, count, cap_safe=False, log=print) -> int:
    """Raise the `taiko` exe's song ceiling to hold `count` songs.

    cap_safe=True clamps to the proven-sound 214 (arrayB); otherwise uses the
    experimental arrayA relocation up to taiko_exe.MAX_SONGS_BIG. Returns the
    ceiling actually written. Only valid for a T14+ HDD .img target.
    """
    from taiko2x6.exe import taiko_exe
    target = SAFE_LIMIT if cap_safe else count
    if not cap_safe and target > taiko_exe.MAX_SONGS_BIG:
        log(f"⚠ {target} exceeds the exe max {taiko_exe.MAX_SONGS_BIG}; "
            f"capping the ceiling there (songs above it stay in the data but "
            f"may be unreachable)")
        target = taiko_exe.MAX_SONGS_BIG
    target = max(SAFE_LIMIT, target)
    log(f"patching song ceiling → {target}"
        + (" (safe/proven ≤214)" if target <= SAFE_LIMIT else " (experimental >214)"))
    taiko_exe.patch_hdd(str(img), target)
    return target


def taiko_exe_max() -> int:
    """Highest song ceiling the `taiko` patch supports."""
    from taiko2x6.exe import taiko_exe
    return taiko_exe.MAX_SONGS_BIG


def song_id_set(archive) -> set:
    from taiko2x6.songs import song_builder
    return set(song_builder.song_ids(archive))


# --------------------------------------------------------------------------- #
#  per-archive read cache (planning only)
#
#  plan_merge reads each archive's tuning.bin / musicinfo.bin and derives the
#  same id list / genre table from them a dozen times (every helper re-reads
#  the file, and each read walks the whole group table and decompresses the
#  group). While a plan_merge / repair_song_db is running the archives are
#  read-only, so within `_read_cache()` those reads and derivations are
#  memoised per archive object. Outside the context nothing is cached.
# --------------------------------------------------------------------------- #
import threading
from contextlib import contextmanager

_cache_state = threading.local()


@contextmanager
def _read_cache():
    """Memoise _named / _ordered_ids / _genres for the archives touched inside."""
    outer = getattr(_cache_state, "cache", None)
    if outer is None:
        _cache_state.cache = {}
    try:
        yield
    finally:
        if outer is None:
            _cache_state.cache = None


def _cached(archive, key, compute):
    cache = getattr(_cache_state, "cache", None)
    if cache is None:
        return compute()
    # the archive is kept in the value so its id() cannot be reused meanwhile
    ent = cache.get((id(archive), key))
    if ent is None:
        try:
            ent = (archive, True, compute())
        except Exception as exc:
            ent = (archive, False, exc)
        cache[(id(archive), key)] = ent
    if not ent[1]:
        raise ent[2]
    return ent[2]


def _named(archive, filename):
    """song_builder._read_named, memoised inside _read_cache()."""
    from taiko2x6.songs import song_builder
    return _cached(archive, ("file", filename.lower()),
                   lambda: song_builder._read_named(archive, filename))


def _rows(archive) -> list:
    """_musicinfo_rows of the archive's musicinfo.bin (memoised, shared list:
    callers must not modify it)."""
    return _cached(archive, ("rows",),
                   lambda: _musicinfo_rows(_named(archive, "musicinfo.bin")))


def _previews(archive) -> dict:
    return _cached(archive, ("previews",),
                   lambda: musicinfo_previews(_named(archive, "musicinfo.bin")))


def _genres(archive) -> dict:
    """musicinfo_genres of the archive's musicinfo.bin (memoised, shared dict:
    callers must not modify it)."""
    return _cached(archive, ("genres",),
                   lambda: musicinfo_genres(_named(archive, "musicinfo.bin")))


# --------------------------------------------------------------------------- #
#  reading per-song metadata (stars / genre) in the source's own DB variant
# --------------------------------------------------------------------------- #
CPOP = 8                 # C-POP song genre
CPOP_HEADER = 15         # its folder header row ("selcpp")


def musicinfo_genres(raw: bytes) -> dict:
    """{song id: genre} for the song rows of musicinfo.bin (headers left out)."""
    return _song_genres(_musicinfo_rows(raw))


def _song_genres(rows) -> dict:
    return {sid: g for sid, g in rows
            if 0 <= g <= 6 or g == CPOP}       # 8 = C-POP (T11T/T12T, TK14 .0001)


def musicinfo_ids(raw: bytes) -> set:
    """Every id that has a row in musicinfo.bin (songs and folder headers)."""
    return {sid for sid, _g in _musicinfo_rows(raw)}


def _musicinfo_rows(raw: bytes) -> list:
    """[(id, genre)] straight from musicinfo.bin's own rows.

    Each SEC0 row starts with two pool offsets (id, title) and the genre in
    column 2, so this needs no row-to-id pairing. Genre numbers are the same
    from Taiko 7 to 14 (0 J-POP, 1 anime, 2 variety, 3 classic, 4 Namco
    original, 5 children's songs, 6 game music); 7..15 mark the folder header
    rows. Works for the T8 and T14 layouts and for T11T/T12T, whose 100/104-byte
    rows sit behind a T8-style header.
    """
    import struct
    from taiko2x6.bineditors import bineditor_musicinfo as MI
    if len(raw) < 0x34:
        return []
    t14 = struct.unpack_from("<I", raw, 0x20)[0] == MI.HEADER_SIZE_T14
    if t14:
        try:
            m = MI.parse(raw)
            rows, pool_b = m.sec0, m.pool
        except Exception:
            return []
    else:
        hdr = struct.unpack_from("<13I", raw, 0)
        count, o0, o5 = hdr[0], hdr[7], hdr[12]
        if not count or (o0 - 0x34) % count or o5 > len(raw):
            return []
        rs = (o0 - 0x34) // count
        if rs < 12:
            return []
        rows = [struct.unpack_from("<3i", raw, 0x34 + i * rs) for i in range(count)]
        pool_b = raw[o5:]

    def cs(off):
        if not 0 <= off < len(pool_b):
            return None
        end = pool_b.find(b"\0", off)
        return pool_b[off:end].decode("ascii", "replace") if end > off else None

    out = []
    for r in rows:
        sid = cs(r[0])
        if sid:
            out.append((sid, r[2]))
    return out


# Song-select preview: where the select screen starts playing the song (ms).
# Each row ends with four preview words [start, start+3000, start, start+5000]
# (T7 has its own fade marks there); TK14 also keeps the start in column 12.
# The rows are 26 ints in T13/T14, 25 in T11T/T12T and 21 in T7..T10, so the
# start is always the fourth word from the end.
_PREVIEW_COL_T14 = 12
_PREVIEW_TAIL_T14 = 22


def musicinfo_previews(raw: bytes) -> dict:
    """{song id: preview start in ms} from musicinfo.bin (T7..T14 layouts)."""
    import struct
    from taiko2x6.bineditors import bineditor_musicinfo as MI
    if len(raw) < 0x34:
        return {}
    if struct.unpack_from("<I", raw, 0x20)[0] == MI.HEADER_SIZE_T14:
        m = MI.parse(raw)
        rows, pool = [list(r) for r in m.sec0], m.pool
    else:
        hdr = struct.unpack_from("<13I", raw, 0)
        count, o0, o5 = hdr[0], hdr[7], hdr[12]
        if not count or (o0 - 0x34) % count or o5 > len(raw):
            return {}
        rs = (o0 - 0x34) // count
        if rs < 32:
            return {}
        rows = [list(struct.unpack_from("<%di" % (rs // 4), raw, 0x34 + i * rs))
                for i in range(count)]
        pool = raw[o5:]
    out = {}
    for r in rows:
        if not 0 <= r[0] < len(pool):
            continue
        sid = pool[r[0]:pool.find(b"\0", r[0])].decode("ascii", "replace")
        if sid and r[-4] > 0:
            out[sid] = r[-4]
    return out


def _apply_previews(m, previews: dict, sids=None) -> None:
    """Write preview starts into the parsed T14 rows (in place)."""
    pool = m.pool
    for k, rec in enumerate(m.sec0):
        if sids is None:
            o = rec[0]
            sid = pool[o:pool.find(b"\0", o)].decode("ascii", "replace")
        else:
            sid = sids[k]
        st = previews.get(sid)
        if st is None or st <= 0:
            continue
        rec[_PREVIEW_COL_T14] = st
        rec[_PREVIEW_TAIL_T14:_PREVIEW_TAIL_T14 + 4] = [st, st + 3000, st, st + 5000]


def set_musicinfo_previews(raw: bytes, previews: dict) -> bytes:
    """TK14 musicinfo.bin with {song id: preview start ms} written the way
    TK14's own rows hold it (column 12 and [s, s+3000, s, s+5000])."""
    from taiko2x6.bineditors import bineditor_musicinfo as MI
    if not previews:
        return raw
    m = MI.parse(raw)
    _apply_previews(m, previews)
    return MI.serialize(m)


def read_song_meta(archive) -> dict:
    """{sid: {"stars":[e,n,h,o], "genre":int}} read via SongManager.

    SongManager auto-detects the archive's musicinfo variant, so this is correct
    for both T8- and T14-shaped sources. Requires a QApplication (SongManager is
    a QWidget); callers on a worker thread must have created one already.
    """
    from taiko2x6.songs import song_builder
    from taiko2x6.songs.song_manager import SongManager
    from taiko2x6.bineditors import bineditor_tuning as TU
    tuning = _named(archive, "tuning.bin")
    sm = SongManager(
        _named(archive, "musicinfo.bin"),
        tuning,
        _named(archive, "streaminfo.bin"))
    meta = {s.id: {"stars": list(s.stars), "genre": s.genre} for s in sm._songs}
    # SongManager pairs musicinfo rows with ids by position, which drifts on
    # other builds; take each song's genre from its own musicinfo row instead.
    genres = _genres(archive)
    for sid, g in genres.items():
        meta.setdefault(sid, {"stars": [1, 1, 1, 1], "genre": g})["genre"] = g
    if TU.is_t14_layout(tuning):
        # Taiko 14's normal/hard/oni records are one int longer than T8's, so
        # SongManager's T8 reader picks up a score field instead of the stars.
        for sid, st in TU.t14_stars(tuning).items():
            if sid in meta:
                meta[sid]["stars"] = st
    elif not TU.is_t7_layout(tuning):
        # T8..T13 / T11T / T12T: read by id (SongManager can't list T11T/T12T
        # songs, and pairs rows by position elsewhere).
        try:
            for sid, st in TU.t8_stars(tuning).items():
                meta.setdefault(sid, {"stars": st, "genre": 0})["stars"] = st
        except ValueError:
            pass
    if TU.is_t7_layout(tuning):
        # Taiko 7's 26-int tuning records: SongManager's T8 reader lands on
        # the wrong fields, so read the stars with the T7 layout instead.
        stars = TU.t7_stars(tuning)
        for s in sm._songs:
            if s.k < len(stars):
                meta[s.id]["stars"] = stars[s.k]
    return meta


def read_play_fields(archive) -> dict:
    """{sid: [ {T14 tuning column: value} or None per difficulty ]}: each
    chart's soul-gauge gains, judgement and scoring values from the archive's
    tuning.bin, in Taiko 14 column numbers (bineditor_tuning.t14_play_fields).
    Needs a QApplication for Taiko 7 (ids come from SongManager there)."""
    from taiko2x6.songs import song_builder
    from taiko2x6.bineditors import bineditor_tuning as TU
    tuning = _named(archive, "tuning.bin")
    if TU.is_t14_layout(tuning):
        return TU.t14_play_fields(tuning)
    if TU.is_t7_layout(tuning):
        from taiko2x6.songs.song_manager import SongManager
        sm = SongManager(_named(archive, "musicinfo.bin"), tuning,
                         _named(archive, "streaminfo.bin"))
        rows = TU.t7_play_fields(tuning)
        return {s.id: rows[s.k] for s in sm._songs if s.k < len(rows)}
    try:
        return TU.t8_play_fields(tuning)
    except ValueError:
        return {}


_GAUGE_HITS = {1, 2, 3, 4, 5, 7, 8, 11, 13}     # don / ka note types (sht)


def _fumen_index(archive) -> dict:
    """{(sid, letter): (group, entry)} of the archive's 1P charts."""
    out = {}
    for g in archive.layout.groups:
        m = _FUMEN_RE.match(g["name"])
        if m and m["pl"] == "1p":
            files = archive.layout.files_for_group(g)
            if files:
                out[(m["id"], m["d"])] = (g, files[0])
    return out


def estimated_play(archive, sid, stars, index=None) -> list:
    """Soul-gauge gains estimated from the song's own charts in `archive`
    (bineditor_tuning.estimate_gauge), for a song no release ships tuning
    values for. [ {12: good, 13: ok, 14: bad} or None per difficulty ]."""
    from taiko2x6.charts import sht_export, tja2sht
    from taiko2x6.bineditors import bineditor_tuning as TU
    index = index if index is not None else _fumen_index(archive)
    out = []
    for d, letter in enumerate("enhm"):
        hit = index.get((sid, letter))
        st = (stars or [0] * 4)[d] if d < len(stars or []) else 0
        if not hit or not st or st <= 0:
            out.append(None)
            continue
        try:
            model = tja2sht.parse_sht(archive.read_file(*hit))
            _br, rts = sht_export.routes(model)
            n = sum(1 for trk in rts for x in trk[0] if x["type"] in _GAUGE_HITS)
        except Exception:
            out.append(None)
            continue
        if n <= 0:
            out.append(None)
            continue
        g, o, b = TU.estimate_gauge(d, st, n)
        out.append({12: g, 13: o, 14: b})
    return out


# --------------------------------------------------------------------------- #
#  harvesting one song's assets (verbatim)
# --------------------------------------------------------------------------- #
def _fumen_belongs(group_name: str, sid: str) -> bool:
    m = _FUMEN_RE.match(group_name)
    return bool(m) and m.group("id") == sid


_STREAM_PREFIX = "sound.stream.music_"
_TEX_PREFIXES = tuple(f"music_texture.{t}_" for t in _TEX_TYPES)
_SELECT_GROUP = "music_texture.music_select"


class HarvestIndex:
    """One pass over an archive's group table, so harvesting N songs costs
    O(groups + N) instead of O(N x groups).

    per_song[sid]: that song's chart / stream / name-texture groups in layout
    order; select: [(group, is_shared, {entry name: [(pos, entry)]})] for the
    shared music_texture.music_select* groups, in layout order.
    """

    def __init__(self, archive):
        self.per_song = {}
        self.select = []
        layout = archive.layout
        for grp in layout.groups:
            gn = grp["name"]
            m = _FUMEN_RE.match(gn)
            sid = m.group("id") if m else None
            if sid is None and gn.startswith(_STREAM_PREFIX):
                sid = gn[len(_STREAM_PREFIX):]
            if sid is None:
                for pre in _TEX_PREFIXES:
                    if gn.startswith(pre):
                        sid = gn[len(pre):]
                        break
            if sid is not None:
                self.per_song.setdefault(sid, []).append(grp)
            elif gn == _SELECT_GROUP or gn.startswith(_SELECT_GROUP + "_"):
                names = {}
                for pos, e in enumerate(layout.files_for_group(grp)):
                    names.setdefault(e["name"], []).append((pos, e))
                self.select.append((grp, gn == _SELECT_GROUP, names))


def harvest_song(archive, sid: str, index: HarvestIndex = None) -> tuple:
    """Collect sid's assets from `archive`.

    Returns (new_groups, extra_files):
      * new_groups: per-song groups (charts, name textures, audio) as
        [{name, files:[(fname, raw_bytes)], compression}] — decompressed on read,
        recompressed by build_archive. Taiko 7 charts (an older measure-record
        layout) are converted to the Gen2 layout here; such groups carry
        "converted": True.
      * extra_files: this song's files that live inside SHARED groups
        (music_texture.music_select*, holding select_full/short/non) as
        {group_name: [(fname, raw_bytes)]}.

    `index` (HarvestIndex(archive)) skips the per-call scan of the group
    table when many songs are harvested from one archive.
    """
    from taiko2x6.charts import sht_t7
    if index is None:
        index = HarvestIndex(archive)
    new_groups = []
    extra_files = {}
    want_select = {f"select_full_{sid}", f"select_short_{sid}"}
    non_name = f"select_non_{sid}"
    for grp in index.per_song.get(sid, ()):
        gn = grp["name"]
        comp = grp.get("compression", 2)
        files = [(e["name"], archive.read_file(grp, e))
                 for e in archive.layout.files_for_group(grp)]
        g = {"name": gn, "files": files, "compression": comp}
        if gn.startswith("fumen."):
            conv = [sht_t7.ensure_gen2(d) for _n, d in files]
            if any(c for _d, c in conv):
                g["files"] = [(n, d) for (n, _o), (d, _c) in zip(files, conv)]
                g["converted"] = True
        new_groups.append(g)
    for grp, shared, names in index.select:
        gn = grp["name"]
        if shared:
            hits = sorted(h for w in want_select for h in names.get(w, ()))
        else:
            hits = names.get(non_name, ())
        for _pos, e in hits:
            extra_files.setdefault(gn, []).append((e["name"], archive.read_file(grp, e)))
    return new_groups, extra_files


def song_asset_summary(new_groups, extra_files) -> dict:
    charts = sum(1 for g in new_groups if g["name"].startswith("fumen."))
    texg = sum(1 for g in new_groups if g["name"].startswith("music_texture."))
    audio = any(g["name"].startswith("sound.stream.") for g in new_groups)
    selfiles = sum(len(v) for v in extra_files.values())
    out = {"chart_groups": charts, "texture_groups": texg,
           "audio": audio, "select_files": selfiles}
    conv = sum(1 for g in new_groups if g.get("converted"))
    if conv:
        out["t7_charts_converted"] = conv
    return out


# --------------------------------------------------------------------------- #
#  planning the merge
# --------------------------------------------------------------------------- #
@dataclass
class SourceSpec:
    """One source archive to harvest from."""
    label: str                      # human name, e.g. "Taiko 8"
    list_path: str
    data_path: str
    fmt: int = 2


@dataclass
class PlanItem:
    sid: str
    source_label: str
    stars: list
    genre: int
    assets: dict = field(default_factory=dict)
    # gauge / judgement / scoring per difficulty, from the song's first
    # trusted source (see read_play_fields); None keeps the template's
    play: list = None
    # song-select preview start (ms) from the same source; None keeps it
    preview: int = None


@dataclass
class MergePlan:
    target_ids: list                # ids already in the target (kept as-is)
    items: list                     # PlanItem, in add order
    skipped_dupes: dict = field(default_factory=dict)   # sid -> [labels]
    # sid -> (source label, title, id it duplicates) for same-title skips
    skipped_titles: dict = field(default_factory=dict)
    # target rows whose genre is corrected in place: sid -> genre
    genre_fixes: dict = field(default_factory=dict)
    # target rows whose stars are corrected in place: sid -> [e, n, h, o]
    star_fixes: dict = field(default_factory=dict)
    # sid -> sort key inside its genre block (see _resolve_trusted)
    order: dict = field(default_factory=dict)
    # target rows whose gauge / scoring values are corrected: sid -> play
    play_fixes: dict = field(default_factory=dict)
    # target rows whose preview start is corrected: sid -> ms
    preview_fixes: dict = field(default_factory=dict)
    # read-only info for the plan view: sid -> title (where known), and the
    # ids whose gauge values were estimated from the chart, not taken from a source
    titles: dict = field(default_factory=dict)
    play_estimated: set = field(default_factory=set)

    @property
    def total_after(self) -> int:
        return len(self.target_ids) + len(self.items)


# --------------------------------------------------------------------------- #
#  song titles (for same-song-different-id dedup)
# --------------------------------------------------------------------------- #
PLACEHOLDER_SHARE = 5   # a title used by this many ids in one archive is a placeholder


def _norm_title(title: str) -> str:
    """Comparable form: NFKC, no whitespace, case-folded."""
    import unicodedata
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", title)).casefold()


def song_titles(archive) -> dict:
    """{sid: title} from tuning.bin's string pool ("<id> music_<id> <title> ...").

    Titles shared by PLACEHOLDER_SHARE or more ids are dropped: songs added by
    older builders all carry their template's title, so it says nothing.
    """
    from collections import Counter
    from taiko2x6.songs import song_builder
    tu = _named(archive, "tuning.bin")
    toks = tu.split(b"\0")
    out = {}
    for i in range(len(toks) - 1):
        t = toks[i]
        if t.startswith(b"music_") and len(t) > 6:
            out.setdefault(t[6:].decode("latin-1"),
                           toks[i + 1].decode("utf-8", "replace"))
    count = Counter(_norm_title(v) for v in out.values())
    return {k: v for k, v in out.items()
            if v.strip() and count[_norm_title(v)] < PLACEHOLDER_SHARE}


def _title_key(sid: str, title: str):
    """Same-song key. `ex*` ids are the ura / long variants; some share the
    base song's title exactly (e.g. Tank!), so they only match each other."""
    return _norm_title(title), sid.startswith("ex")


def plan_merge(target, sources, log=lambda s: None,
               dedupe_titles: bool = True) -> MergePlan:
    """Decide which songs to add. 14 is the base: keep every target song, and
    from the sources add only ids the target does not already have. If the same
    new id appears in several sources, the FIRST source in `sources` order wins
    (put the newest/most-complete version first).

    dedupe_titles: also skip a song whose title is already present under a
    different id (the same song renamed between versions). Titles come from
    every archive involved, so a target song with a placeholder title still
    gets its real one from the game it was taken from.
    """
    with _read_cache():
        return _plan_merge(target, sources, log, dedupe_titles)


def _plan_merge(target, sources, log, dedupe_titles) -> MergePlan:
    from taiko2x6.songs import song_builder
    target_ids = list(_ordered_ids(target))
    have = set(target_ids)
    items = []
    skipped = {}
    skipped_titles = {}
    seen_new = set()

    opened = []
    titles = {}
    try:
        for spec in sources:
            src = open_pair(spec.list_path, spec.data_path, spec.fmt)
            opened.append((spec, src))
            if dedupe_titles:
                for k, v in song_titles(src).items():
                    titles.setdefault(k, v)
        if dedupe_titles:
            for k, v in song_titles(target).items():
                titles.setdefault(k, v)
        seen_titles = {}
        for sid in target_ids:
            if sid in titles:
                seen_titles.setdefault(_title_key(sid, titles[sid]), sid)

        metas = {}
        plays = {}
        previews = {}
        unlisted = {}
        for spec, src in opened:
            meta = read_song_meta(src)
            metas[spec.label] = meta
            try:
                plays[spec.label] = read_play_fields(src)
            except Exception as exc:
                log(f"{spec.label}: no gauge/scoring values ({exc})")
                plays[spec.label] = {}
            try:
                previews[spec.label] = _previews(src)
            except Exception:
                previews[spec.label] = {}
            try:
                rows = _rows(src)
            except Exception:
                rows = []
            # song rows only: a folder header is not a song either (the old
            # games' Namco header is "origin", TK14's is "selorg")
            listed = {sid for sid, g in rows if 0 <= g <= 6 or g == CPOP}
            for sid in _ordered_ids(src):
                if sid in have:
                    continue                    # already in target (14 wins)
                if rows and sid not in listed:
                    # a folder header row (T9..T11T's "origin") or an id song
                    # select does not list at all: not a song
                    unlisted.setdefault(sid, []).append(spec.label)
                    continue
                if sid in seen_new:
                    skipped.setdefault(sid, []).append(spec.label)
                    continue                    # an earlier source already added it
                if dedupe_titles and sid in titles:
                    key = _title_key(sid, titles[sid])
                    if key in seen_titles:
                        skipped_titles[sid] = (spec.label, titles[sid], seen_titles[key])
                        continue
                    seen_titles[key] = sid
                m = meta.get(sid, {"stars": [1, 1, 1, 1], "genre": 0})
                items.append(PlanItem(sid=sid, source_label=spec.label,
                                      stars=m["stars"], genre=m["genre"]))
                seen_new.add(sid)
            log(f"{spec.label}: +{sum(1 for it in items if it.source_label == spec.label)} new")
        fixes = _vote_genres(target, opened, items, log, metas)
        star_fixes = _target_star_fixes(target, opened, metas, log)
        gfix, sfix, order, play_fixes, preview_fixes = _resolve_trusted(
            target, opened, items, metas, log, plays, previews)
        for sid, g in gfix.items():
            fixes.setdefault(sid, g)          # the C-POP refiling wins
        star_fixes.update(sfix)
        play_estimated = set()
        _estimate_missing_play(target, opened, items, play_fixes, log,
                               estimated=play_estimated)
    finally:
        for _spec, src in opened:
            src.close()
    if unlisted:
        log(f"not in the source's song list, skipped: {', '.join(sorted(unlisted))}")
    if skipped_titles:
        log(f"same title under another id, skipped: {len(skipped_titles)}")
    return MergePlan(target_ids=target_ids, items=items, skipped_dupes=skipped,
                     skipped_titles=skipped_titles, genre_fixes=fixes,
                     star_fixes=star_fixes, order=order, play_fixes=play_fixes,
                     preview_fixes=preview_fixes, titles=dict(titles),
                     play_estimated=play_estimated)


def _valid_stars(st) -> bool:
    return (isinstance(st, list) and len(st) == 4
            and all(1 <= v <= 10 for v in st[:3]) and (st[3] == -1 or 1 <= st[3] <= 10))


def _vote_genres(target, opened, items, log=lambda s: None, metas=None) -> None:
    """Pick each added song's genre and stars from every source that has it.

    Sources are weighted by how often their genre agrees with the target's for
    the songs they share, so a hand-made Omnimix that filed songs under the
    wrong folder (or copied one template's difficulty onto every song) is
    outvoted by the official releases. Genre: weighted vote. Stars: taken from
    the most trusted source with a valid rating.
    """
    metas = metas or {}
    from taiko2x6.songs import song_builder
    def genres(arc):
        try:
            return _genres(arc)
        except Exception:
            return {}
    tgt = genres(target)
    per = []
    for spec, src in opened:
        g = genres(src)
        shared = [k for k in g if k in tgt]
        agree = sum(g[k] == tgt[k] for k in shared)
        weight = (agree + 1) / (len(shared) + 2)          # unknown source ~ 0.5
        per.append((spec.label, g, weight))
        log(f"genres {spec.label}: {agree}/{len(shared)} agree with the target")
    changed = restar = 0
    for it in items:
        best_st, best_w = None, -1.0
        for label, _g, w in per:
            st = metas.get(label, {}).get(it.sid, {}).get("stars")
            if _valid_stars(st) and w > best_w:
                best_st, best_w = list(st), w
        if best_st and best_st != it.stars:
            it.stars = best_st
            restar += 1
        votes = {}
        for _label, g, w in per:
            if it.sid in g:
                votes[g[it.sid]] = votes.get(g[it.sid], 0.0) + w
        if votes:
            best = max(votes, key=lambda k: votes[k])
            if best != it.genre:
                changed += 1
            it.genre = best
    log(f"genres: {changed} added songs refiled by the cross-version vote, "
        f"{restar} took their stars from a more trusted source")

    # C-POP: any song a release files under C-POP goes there (target rows too:
    # TK14's own C-POP songs sit in Namco original), and the C-POP header row,
    # which TK14 also files as a Namco song, becomes a folder header again.
    cpop = {sid for _l, g, _w in per for sid, v in g.items() if v == CPOP}
    for it in items:
        if it.sid in cpop:
            it.genre = CPOP
    fixes = {sid: CPOP for sid in tgt if sid in cpop and tgt[sid] != CPOP}
    if "selcpp" in set(_ordered_ids(target)):
        fixes["selcpp"] = CPOP_HEADER
    if fixes:
        log(f"genres: C-POP folder ({len(cpop)} songs), {len(fixes)} target rows refiled")
    return fixes


def _resolve_trusted(target, opened, items, metas, log=lambda s: None, plays=None,
                     previews=None):
    """Genre, stars and folder order of every song from its first trusted source.

    The first source is the base release (stock TK14). Any other archive in
    the base's own format (TK14 tuning layout), or one that holds every base
    song, is a mod built on it (an earlier Omnimix, a hand-made pack): the
    songs it adds on top of the base got their folder and stars from that
    merge, which filed dozens under Namco original or copied one template's
    difficulty, so those rows are not evidence. Older releases are.

    For each song the first source in `opened` order (newest first) that
    holds it as trusted data decides its genre and stars; songs no trusted
    source knows keep what they have. Returns (target genre fixes, target
    star fixes, rank, target play fixes) where rank[sid] = (source index, row
    in that source) orders each genre block like the games it came from.
    C-POP songs are left to the C-POP rule.

    `plays` ({label: read_play_fields(...)}) supplies each chart's soul-gauge
    and scoring values the same way: added songs get them on `it.play`, and
    target songs an earlier merge added (cloned from its template row, so
    every one of them had the template's gauge) get them as play fixes.
    `previews` ({label: musicinfo_previews(...)}) does the same for the
    song-select preview start (returned last, as target preview fixes).
    """
    from taiko2x6.songs import song_builder

    from taiko2x6.bineditors import bineditor_tuning as TU

    def info(arc):
        try:
            ids = list(_ordered_ids(arc))
            g = _genres(arc)
        except Exception:
            return [], {}, False
        try:
            t14 = TU.is_t14_layout(_named(arc, "tuning.bin"))
        except Exception:
            t14 = False
        return ids, g, t14

    plays = plays or {}
    previews = previews or {}
    if not opened:
        return {}, {}, {}, {}, {}
    srcs = [(spec.label, *info(src)) for spec, src in opened]
    base_ids = set(srcs[0][1])
    base_t14 = srcs[0][3]
    tgt_ids, tgt_g, tgt_t14 = info(target)

    def derived(ids, t14):
        return bool(base_ids) and ((base_t14 and t14) or base_ids <= set(ids))

    trusted = []
    for n, (label, ids, g, t14) in enumerate(srcs):
        mod = n > 0 and derived(ids, t14)
        keep = set(ids) & base_ids if mod else set(ids)
        trusted.append((label, {sid: i for i, sid in enumerate(ids) if sid in keep}, g))
        if mod:
            log(f"trust: {label} is built on {srcs[0][0]}; its added songs are not used")
    tgt_derived = derived(tgt_ids, tgt_t14)

    def first(sid):
        for n, (label, pos, g) in enumerate(trusted):
            if sid in pos and sid in g:
                return n, label, pos[sid], g[sid]
        return None

    def first_play(sid):
        for label, pos, _g in trusted:
            if sid in pos and any(plays.get(label, {}).get(sid) or ()):
                return plays[label][sid]
        return None

    def first_preview(sid):
        for label, pos, _g in trusted:
            v = previews.get(label, {}).get(sid)
            if sid in pos and v:
                return v
        return None

    rank, gfix, sfix, pfix, vfix = {}, {}, {}, {}, {}
    if tgt_derived:
        for sid in tgt_ids:
            if sid not in base_ids:
                pl = first_play(sid)
                if pl:
                    pfix[sid] = pl
                pv = first_preview(sid)
                if pv:
                    vfix[sid] = pv
    for sid in tgt_ids:
        hit = first(sid)
        if not hit:
            continue
        n, label, p, genre = hit
        rank[sid] = (n, p)
        if not tgt_derived or sid in base_ids:
            continue                      # an untouched release: keep its rows
        if sid in tgt_g and tgt_g[sid] != genre and tgt_g[sid] != CPOP:
            gfix[sid] = genre
        st = metas.get(label, {}).get(sid, {}).get("stars")
        if _valid_stars(st):
            sfix[sid] = list(st)
    regenre = 0
    for it in items:
        hit = first(it.sid)
        if not hit:
            continue
        n, label, p, genre = hit
        rank[it.sid] = (n, p)
        if it.genre != CPOP and it.genre != genre:
            it.genre = genre
            regenre += 1
        st = metas.get(label, {}).get(it.sid, {}).get("stars")
        if _valid_stars(st):
            it.stars = list(st)
    nplay = 0
    for it in items:
        it.play = first_play(it.sid)
        it.preview = first_preview(it.sid)
        nplay += bool(it.play)
    log(f"trust: {len(gfix)} target songs refiled, {len(sfix)} target ratings and "
        f"{regenre} added songs' folders taken from their first trusted source")
    log(f"gauge/scoring: {nplay}/{len(items)} added songs and {len(pfix)} earlier-"
        f"merged target songs take their source's values")
    log(f"preview: {sum(1 for it in items if it.preview)}/{len(items)} added songs "
        f"and {len(vfix)} earlier-merged target songs take their source's start")
    return gfix, sfix, rank, pfix, vfix


def _estimate_missing_play(target, opened, items, play_fixes, log=lambda s: None,
                           estimated=None):
    """Gauge estimates for the songs _resolve_trusted found no values for:
    added songs (from the chart in their own source) and target songs an
    earlier merge added that still carry the template row's gauge."""
    from taiko2x6.songs import song_builder
    from taiko2x6.bineditors import bineditor_tuning as TU
    by_label = {spec.label: src for spec, src in opened}
    idx = {}

    def index(arc, key):
        if key not in idx:
            idx[key] = _fumen_index(arc)
        return idx[key]

    n_items = 0
    for it in items:
        if it.play or it.source_label not in by_label:
            continue
        arc = by_label[it.source_label]
        try:
            est = estimated_play(arc, it.sid, it.stars, index(arc, it.source_label))
        except Exception as exc:
            log(f"gauge estimate for {it.sid} skipped ({exc})")
            continue
        if any(est):
            it.play = est
            n_items += 1
            if estimated is not None:
                estimated.add(it.sid)
    n_tgt = 0
    try:
        tuning = _named(target, "tuning.bin")
        if TU.is_t14_layout(tuning) and opened:
            cur, stars = TU.t14_play_fields(tuning), TU.t14_stars(tuning)
            base = set(_ordered_ids(opened[0][1]))
            tmpl = cur.get(pick_template_id(target))
            for sid, f in cur.items():
                if sid in base or sid in play_fixes or f != tmpl or not tmpl:
                    continue
                est = estimated_play(target, sid, stars.get(sid), index(target, None))
                if any(est):
                    play_fixes[sid] = est
                    n_tgt += 1
                    if estimated is not None:
                        estimated.add(sid)
    except Exception as exc:
        log(f"gauge estimate for target songs skipped ({exc})")
    if n_items or n_tgt:
        log(f"gauge: estimated from the charts for {n_items} added and {n_tgt} "
            f"target songs no source has values for")


def repair_song_db(target, sources, log=lambda s: None) -> dict:
    with _read_cache():
        return _repair_song_db(target, sources, log)


def _repair_song_db(target, sources, log) -> dict:
    """{file name: new bytes} that fix an already built Omnimix, empty if
    nothing changes.

    Builds before these fixes cloned every added song's soul-gauge / scoring
    values (tuning.bin) and song-select preview start (musicinfo.bin) from
    the template row. `sources` must start with the base release (stock
    TK14), then the games the songs came from; each song the base does not
    have takes the values of its first trusted source, as a fresh build would.
    Needs a QApplication when a source is Taiko 7.
    """
    from taiko2x6.songs import song_builder
    from taiko2x6.bineditors import bineditor_tuning as TU
    tuning = _named(target, "tuning.bin")
    music = _named(target, "musicinfo.bin")
    if not TU.is_t14_layout(tuning):
        raise ValueError("target is not a Taiko 14 tuning layout")
    opened, plays, previews = [], {}, {}
    try:
        for spec in sources:
            src = open_pair(spec.list_path, spec.data_path, spec.fmt)
            opened.append((spec, src))
            plays[spec.label] = read_play_fields(src)
            previews[spec.label] = _previews(src)
        _g, _s, _r, pfix, vfix = _resolve_trusted(target, opened, [], {}, log,
                                                  plays, previews)
        _estimate_missing_play(target, opened, [], pfix, log)
    finally:
        for _spec, src in opened:
            src.close()
    out = {}
    tu = TU.set_t14_play_fields_by_id(tuning, pfix)
    if tu != tuning:
        out["tuning.bin"] = tu
    mi = set_musicinfo_previews(music, vfix)
    if mi != music:
        out["musicinfo.bin"] = mi
    return out


def repair_image(img, partition, sources, tmpdir, log=lambda s: None,
                 song_db: bool = True, titles: bool = True) -> bool:
    """Bring an already built Omnimix on an .img partition up to date, in one
    pass written back undo-journaled: `song_db` runs repair_song_db (needs
    `sources`, base release first), `titles` runs trim_title_textures (the
    2nd-song hang). DATA.000 keeps its size: a gamedata group that no longer
    fits its sectors moves into dead space (Archive.save). Every changed file
    is read back before anything is written. Returns whether anything was
    written. PCSX2 must be closed."""
    from taiko2x6.songs import song_builder
    tmpdir = Path(tmpdir)
    lp, dp = extract_partition(img, partition, tmpdir / "in", log)
    arc = open_pair(lp, dp)
    try:
        new = repair_song_db(arc, sources, log) if song_db and sources else {}
        if song_db and not sources:
            log("no sources given: song DB (gauge, previews) left as it is")
        for name, data in new.items():
            g, e = song_builder.find_named_entry(arc, name)
            arc.stage_replace(g["index"], e["index"], data)
        trimmed = trim_title_textures(arc, log) if titles else 0
        if not new and not trimmed:
            log("nothing to repair; nothing written")
            return False
        want = {}
        for g in arc.layout.groups:
            if trimmed and g["name"] in TITLE_TEXTURE_GROUPS:
                for e in arc.layout.files_for_group(g):
                    want[(g["name"], e["name"])] = arc.read_file(g, e)
        ol, od = tmpdir / "list.bin", tmpdir / "DATA.000"
        arc.save(ol, od, allow_relayout=True)
    finally:
        arc.close()
    check = open_pair(ol, od)
    try:
        for name, data in new.items():
            if song_builder._read_named(check, name) != data:
                raise ValueError(f"repaired {name} did not read back; nothing written")
        for g in check.layout.groups:
            if trimmed and g["name"] in TITLE_TEXTURE_GROUPS:
                for e in check.layout.files_for_group(g):
                    if check.read_file(g, e) != want.get((g["name"], e["name"])):
                        raise ValueError(f"{g['name']} did not read back; nothing written")
                log(f"{g['name']}: {g['unpacked_size'] / 2**20:.2f} MB in game memory")
    finally:
        check.close()
    if od.stat().st_size != Path(dp).stat().st_size:
        raise ValueError("DATA.000 would change size; nothing written")
    write_partition(img, partition, ol.read_bytes(), od.read_bytes(), log)
    log(f"written: {', '.join(list(new) + (['title textures'] if trimmed else []))}")
    return True


def repair_image_song_db(img, partition, sources, tmpdir, log=lambda s: None) -> bool:
    """repair_image for the song DB only (gauge values, preview starts)."""
    return repair_image(img, partition, sources, tmpdir, log, titles=False)


def trim_image_title_textures(img, partition, tmpdir, log=lambda s: None) -> bool:
    """repair_image for the title textures only (see trim_title_textures)."""
    return repair_image(img, partition, [], tmpdir, log, song_db=False)


# Shared select-screen title textures (select_full/short in the first,
# select_non in the second). The game loads each group whole into its heap.
TITLE_TEXTURE_GROUPS = ("music_texture.music_select",
                        "music_texture.music_select_music")


def _trim_title_file(gn: str, data: bytes) -> bytes:
    from taiko2x6.graphics import tim2
    if gn not in TITLE_TEXTURE_GROUPS:
        return data
    return tim2.crop_transparent_bottom(data) or data


def trim_title_textures(archive, log=lambda s: None) -> int:
    """Stage every title texture with its transparent bottom rows cut off, and
    compact both groups so their decompressed size shrinks too.

    Why: the game loads each of these groups whole into the EE heap, which
    already reaches the stack at the top of RAM. With ~600 songs the select
    textures cost 8 MB more than stock, and on the 2nd song of a credit the
    difficulty select could no longer get 3.97 MB in one piece for
    music_select_music, so it hung on 「ちょっとまってね」 (memalign
    returned 0 in sub_104DB0). The pictures draw from their top edge at their
    own size, so the screen does not change. Returns the bytes saved."""
    saved = 0
    for g in archive.layout.groups:
        if g["name"] not in TITLE_TEXTURE_GROUPS:
            continue
        for e in archive.layout.files_for_group(g):
            data = archive.read_file(g, e)
            new = _trim_title_file(g["name"], data)
            if len(new) < len(data):
                archive.stage_replace(g["index"], e["index"], new)
                saved += len(data) - len(new)
        archive.stage_compact(g["index"])
    log(f"title textures: {saved / 2**20:.1f} MB of transparent rows cut")
    return saved


def _ordered_ids(archive):
    from taiko2x6.songs import song_builder
    return list(_cached(archive, ("ids",), lambda: song_builder.song_ids_from(
        _named(archive, "tuning.bin"))))


# --------------------------------------------------------------------------- #
#  choosing a DB template row in the target
# --------------------------------------------------------------------------- #
def _target_star_fixes(target, opened, metas, log=lambda s: None) -> dict:
    """Correct target songs that carry the DB template's stars.

    Songs added to the target by an earlier build were cloned from its
    template row; if that build had no stars for them they kept the
    template's difficulty (TK14: 10jiku's [4, 7, 8, 10], so dozens of songs
    showed 10 stars). Target songs are kept as-is otherwise, so repair just
    those: a song whose stars equal the template's, when a source has a
    different valid rating for it. Real ratings that differ from the
    template's are never touched.
    """
    from taiko2x6.songs import song_builder
    from taiko2x6.bineditors import bineditor_tuning as TU
    try:
        tuning = _named(target, "tuning.bin")
        if not TU.is_t14_layout(tuning):
            return {}
        tgt = TU.t14_stars(tuning)
        tmpl = pick_template_id(target)
    except Exception:
        return {}
    tstars = tgt.get(tmpl)
    if not _valid_stars(tstars):
        return {}
    fixes = {}
    for sid, st in tgt.items():
        if sid == tmpl or st != tstars:
            continue
        for spec, _src in opened:
            cand = metas.get(spec.label, {}).get(sid, {}).get("stars")
            if _valid_stars(cand) and list(cand) != tstars:
                fixes[sid] = list(cand)
                break
    if fixes:
        log(f"stars: {len(fixes)} target songs had the template's {tstars}, "
            f"restored from their source")
    return fixes


def pick_template_id(target) -> str:
    """A real (non-header) target song to clone the DB record shape from.

    Textures are copied verbatim, so the template only supplies the musicinfo/
    tuning/streaminfo record layout — any real song works. Returns the first
    target song id (song_ids skips header rows already).
    """
    ids = _ordered_ids(target)
    if not ids:
        raise ValueError("target has no songs to use as a DB template")
    return ids[0]


# --------------------------------------------------------------------------- #
#  building the merged archive (one SongManager, one rebuild)
# --------------------------------------------------------------------------- #
def precompute_db(target, plan, template_id=None) -> dict:
    """GUI-THREAD step: build the merged musicinfo/tuning/streaminfo bins.

    SongManager is a QWidget, so this MUST run on the Qt GUI thread (mirrors
    song_builder.prepare_new_song_db). Returns the precomputed_db dict to hand
    into :func:`assemble` on a worker thread. Raises if the target DB is
    inconsistent (an un-bootable base) or the template id is missing.
    """
    from taiko2x6.songs import song_builder
    from taiko2x6.songs.song_manager import SongManager

    con = song_builder.check_db_consistency(target)
    if not con["consistent"]:
        raise ValueError(
            "target song-DB bins disagree on the song count "
            f"(musicinfo={con['musicinfo']}, tuning_ids={con['tuning_ids']}, "
            f"tuning_blocks={con['tuning_blocks']}); reload a clean target.")

    tmpl_id = template_id or pick_template_id(target)
    tgt_ids = _ordered_ids(target)
    if tmpl_id not in tgt_ids:
        raise ValueError(f"template id '{tmpl_id}' not in target")
    k_template = tgt_ids.index(tmpl_id)

    sm = SongManager(
        song_builder._read_named(target, "musicinfo.bin"),
        song_builder._read_named(target, "tuning.bin"),
        song_builder._read_named(target, "streaminfo.bin"))
    for it in plan.items:
        stars = [max(1, min(10, int(v))) for v in (it.stars or [1, 1, 1, 1])[:4]]
        while len(stars) < 4:
            stars.append(1)
        # Without the source genre every added song copies the template's
        # folder, piling hundreds into one genre, which freezes song select.
        song_builder._db_add_song(sm, it.sid, k_template, stars, genre=it.genre)
    res = sm._build_result()
    # Always pass through: besides the C-POP refiling this regroups the rows
    # by genre and repoints the folder jump table (T14 only; no-op otherwise).
    from taiko2x6.songs.song_manager import MUSICINFO
    mi = res.get(MUSICINFO) or song_builder._read_named(target, "musicinfo.bin")
    # Added rows were cloned from the template, preview start included, so
    # every added song previewed from the template song's position.
    prev = dict(getattr(plan, "preview_fixes", None) or {})
    prev.update({it.sid: it.preview for it in plan.items if getattr(it, "preview", None)})
    out = set_musicinfo_fixes(mi, getattr(plan, "genre_fixes", None) or {},
                              getattr(plan, "order", None) or {}, prev)
    if out != mi:
        res[MUSICINFO] = out
    star_fixes = getattr(plan, "star_fixes", None) or {}
    if star_fixes:
        from taiko2x6.songs.song_manager import TUNING
        from taiko2x6.bineditors import bineditor_tuning as TU
        tu = res.get(TUNING) or song_builder._read_named(target, "tuning.bin")
        if TU.is_t14_layout(tu):
            res[TUNING] = TU.set_t14_stars_by_id(tu, star_fixes)
    # Every added row was cloned from the template, gauge and scoring
    # included: without this the soul gauge filled at the template song's
    # rate, so the clear line was wrong on every added chart.
    play = dict(getattr(plan, "play_fixes", None) or {})
    play.update({it.sid: it.play for it in plan.items if getattr(it, "play", None)})
    if play:
        from taiko2x6.songs.song_manager import TUNING
        from taiko2x6.bineditors import bineditor_tuning as TU
        tu = res.get(TUNING) or song_builder._read_named(target, "tuning.bin")
        if TU.is_t14_layout(tu):
            res[TUNING] = TU.set_t14_play_fields_by_id(tu, play)
    return res


def _row_sids(m) -> list:
    pool = m.pool
    find = pool.find
    return [pool[r[0]:find(b"\0", r[0])].decode("ascii", "replace")
            for r in m.sec0]


def set_musicinfo_genres(raw: bytes, genres: dict, order: dict = None) -> bytes:
    """Set column 2 (genre) of the musicinfo rows whose id is in `genres`."""
    return set_musicinfo_fixes(raw, genres, order, None)


def set_musicinfo_fixes(raw: bytes, genres: dict, order: dict = None,
                        previews: dict = None) -> bytes:
    """set_musicinfo_genres followed by set_musicinfo_previews, byte-identical
    to chaining them, but parsed and serialized once (the preview columns are
    independent of the regrouping, which only moves whole rows)."""
    from taiko2x6.bineditors import bineditor_musicinfo as MI
    m = MI.parse(raw)
    if m.sec0 and (genres or previews or order):
        sids = _row_sids(m)
        if genres:
            for rec, sid in zip(m.sec0, sids):
                if sid in genres:
                    rec[2] = int(genres[sid])
        if previews:
            _apply_previews(m, previews, sids)
        group_rows_by_genre(m, order, sids)
    else:
        group_rows_by_genre(m, order)
    fix_genre_jumps(m)
    return MI.serialize(m)


SONG_GENRES = (0, 1, 2, 3, 4, 5, 6, CPOP)
# folder header genre -> the song genre its folder holds
HEADER_GENRE = {7: 4, 9: 0, 10: 6, 11: 3, 12: 2, 13: 1, 14: 5, CPOP_HEADER: CPOP}


def group_rows_by_genre(m, order_by_sid: dict = None, _sids=None) -> None:
    """Reorder T14 musicinfo rows so every genre is one contiguous block.

    After a folder is picked, song select walks the rows in file order (only
    the optional re-sort uses the col13..16 orders). Stock TK14 stores its
    rows grouped by genre; rows appended by the merge land at the end in id
    order, which scatters genres inside each folder. The layout becomes: each
    folder header followed by its genre's block (with the exe's header masks
    off, the header shows as a divider card in the song list), in the
    headers' order; genres without a header follow in first-seen order.
    Inside a block rows follow `order_by_sid` (sid -> sort key, e.g.
    (source, row) from _resolve_trusted); rows without a key go last, and
    ties keep the existing order. Every row index stored in the file is
    remapped: col10 (ura row), col13 and col14 (own index), SEC1, SEC4
    field 0, SEC6 and SEC7.
    """
    if getattr(m, "variant", "") != "t14" or not m.sec0:
        return
    rows = m.sec0
    headers = []
    order = []
    members = {}                      # genre -> row indices in file order
    for i, r in enumerate(rows):
        g = r[2]
        if g in SONG_GENRES:
            if g not in members:
                members[g] = []
                order.append(g)
            members[g].append(i)
        else:
            headers.append(i)
    key = {}
    if order_by_sid:
        sids = _sids if _sids is not None else _row_sids(m)
        last = (1 << 30,)
        for i, sid in enumerate(sids):
            key[i] = order_by_sid.get(sid, last) + (i,)

    def block(g):
        # genres absent from the rows (a header with no songs) are empty
        return sorted(members.get(g, ()), key=lambda i: key.get(i, (i,)))

    # each folder header opens its own block (a divider card in the song
    # list), blocks in the headers' order; headers of unknown kind go first
    new, done = [], set()
    lone = [h for h in headers if HEADER_GENRE.get(rows[h][2]) is None]
    new += lone
    for h in headers:
        g = HEADER_GENRE.get(rows[h][2])
        if g is None:
            continue
        new.append(h)
        if g not in done:
            new += block(g)
            done.add(g)
    for g in order:
        if g not in done:
            new += block(g)
            done.add(g)
    if new == list(range(len(rows))):
        return
    old2new = {o: n for n, o in enumerate(new)}

    def remap(i):
        return old2new.get(i, i) if i >= 0 else i

    out = []
    for o in new:
        r = list(rows[o])
        r[10] = remap(r[10])
        if r[13] == o:
            r[13] = old2new[o]
        if r[14] == o:
            r[14] = old2new[o]
        out.append(r)
    m.sec0[:] = out
    m.sec1[:] = [remap(i) for i in m.sec1]
    m.sec4[:] = [(remap(a),) + tuple(rest) for a, *rest in m.sec4]
    if m.sec6:
        m.sec6[:] = [remap(i) for i in m.sec6]
    if m.sec7:
        m.sec7[:] = [remap(i) for i in m.sec7]


# T14 musicinfo SEC6: per folder header, the row the cursor lands on when
# that folder is picked (sub_175C20 -> sub_108218(list, slot)).  Folder
# header genre h uses slot h - 3 (namco 7 -> 4) or h - 4 (the rest).
GENRE_JUMP_SLOT = {4: 4, 0: 5, 6: 6, 3: 7, 2: 8, 1: 9, 5: 10, CPOP: 11}


def fix_genre_jumps(m) -> None:
    """Point every SEC6 folder slot at a row of that folder's genre.

    Stock TK14 leaves the C-POP slot at 0 (the J-POP header), so picking
    C-POP lands outside the folder and the cursor search can lock up."""
    sec6 = getattr(m, "sec6", None)
    if not sec6:
        return
    rows = m.sec0
    first = {}
    for i, r in enumerate(rows):
        first.setdefault(r[2], i)
    for genre, slot in GENRE_JUMP_SLOT.items():
        if slot >= len(sec6):
            continue
        cur = sec6[slot]
        if 0 <= cur < len(rows) and rows[cur][2] == genre:
            continue
        f = first.get(genre)
        if f is not None:
            sec6[slot] = f


# Bump when resample_vag / resample_sinc / encode_vag output could change, so
# stale cache entries are never reused.
AUDIO_ENCODER_VERSION = 2
AUDIO_CACHE_MAX_BYTES = 6 * 2**30
_CACHE_MAGIC = b"T2X6AC1\0"


def _audio_cache_path(cache, raw, rate, mono):
    """Content-addressed entry path: (hash of input bytes, rate, mono, encoder)."""
    import hashlib, os
    return os.path.join(cache, "%s_%d%s_v%d.vag" % (
        hashlib.sha1(raw).hexdigest(), rate or 0, "m" if mono else "",
        AUDIO_ENCODER_VERSION))


def _cache_read(path):
    """Verified entry bytes, or None (missing or corrupt; corrupt is deleted)."""
    import hashlib, os
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        return None
    n = len(_CACHE_MAGIC)
    if (blob[:n] == _CACHE_MAGIC and len(blob) >= n + 20
            and hashlib.sha1(blob[n + 20:]).digest() == blob[n:n + 20]):
        try:
            os.utime(path)                      # LRU stamp for the size cap
        except OSError:
            pass
        return blob[n + 20:]
    try:
        os.remove(path)
    except OSError:
        pass
    return None


def _cache_write(path, out):
    """Atomic write: unique temp file in the same dir, then os.replace."""
    import hashlib, os
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "wb") as f:
            f.write(_CACHE_MAGIC + hashlib.sha1(out).digest())
            f.write(out)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def prune_audio_cache(cache, max_bytes=AUDIO_CACHE_MAX_BYTES):
    """Delete least-recently-used entries until the cache fits max_bytes."""
    import os
    ents = []
    try:
        for e in os.scandir(cache):
            if e.is_file():
                if e.name.endswith(".tmp"):
                    continue
                st = e.stat()
                ents.append((st.st_mtime, st.st_size, e.path))
    except OSError:
        return 0
    total = sum(e[1] for e in ents)
    removed = 0
    for _mt, size, path in sorted(ents):
        if total <= max_bytes:
            break
        try:
            os.remove(path)
            total -= size
            removed += 1
        except OSError:
            pass
    return removed


def _resample_job(args):
    raw, rate, cache, mono = args
    from taiko2x6.audio import vagtool
    path = None
    if cache:
        path = _audio_cache_path(cache, raw, rate, mono)
        hit = _cache_read(path)
        if hit is not None:
            return hit
    out = vagtool.resample_vag(raw, rate, mono=mono)
    if path:
        _cache_write(path, out)
    return out


def _resample_many(jobs, rate, cache, mono, workers=None, log=lambda s: None):
    """Resample every raw stream in `jobs`; ordered like the input.

    Cache hits are resolved here (hashing runs in threads: hashlib drops the
    GIL), so only misses are pickled to worker processes - and biggest first,
    for load balance. Workers write the cache entries themselves."""
    from concurrent.futures import ThreadPoolExecutor
    n = len(jobs)
    outs = [None] * n
    todo = list(range(n))
    paths = [None] * n
    if cache:
        import os
        os.makedirs(cache, exist_ok=True)
        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as ex:
            def look(i):
                p = _audio_cache_path(cache, jobs[i], rate, mono)
                return p, _cache_read(p)
            for i, (p, hit) in enumerate(ex.map(look, range(n))):
                paths[i] = p
                outs[i] = hit
        todo = [i for i in range(n) if outs[i] is None]
        log(f"  audio cache: {n - len(todo)} hit, {len(todo)} to encode")
    todo.sort(key=lambda i: -len(jobs[i]))
    # cache=None for the workers' key work: the parent already knows the path
    res = _map_parallel(_resample_job,
                        [(jobs[i], rate, None, mono) for i in todo], workers, log)
    for i, r in zip(todo, res):
        outs[i] = r
        if cache:
            _cache_write(paths[i], r)
    if cache:
        prune_audio_cache(cache)
    return outs


def _map_parallel(func, items, workers=None, log=lambda s: None):
    """func over items in worker processes (ordered); serial if that fails
    (e.g. a frozen build without multiprocessing support)."""
    import os
    from concurrent.futures import ProcessPoolExecutor
    n = len(items)
    if n == 0:
        return []
    w = max(1, min(workers or os.cpu_count() or 2, n))
    try:
        out = []
        if w == 1:
            raise RuntimeError("single worker")
        with ProcessPoolExecutor(max_workers=w) as ex:
            for i, r in enumerate(ex.map(func, items, chunksize=1), 1):
                out.append(r)
                if i % 25 == 0 or i == n:
                    log(f"  audio {i}/{n}")
        return out
    except Exception as exc:                     # pragma: no cover - fallback
        if str(exc) != "single worker":
            log(f"  parallel audio failed ({exc}); continuing in one process")
        return [func(x) for x in items]


def _is_song_group(name: str) -> bool:
    return (name.startswith(("fumen.", "sound.stream.music_"))
            or any(name.startswith(f"music_texture.{t}_") for t in _TEX_TYPES))


def _retail_layout(target, extra_files, keep_songs=True):
    """pin_sectors + ghosts that keep a compact rebuild as close to retail as
    an append build: every unchanged non-song group stays at its original
    sector, and every edited non-song group (DB bins, shared select textures)
    leaves its original bytes at the original sector - T14 reads some data by
    absolute sector outside LIST.BIN (see archive_builder). Only song groups
    (charts, audio, per-song textures), which are always table-driven, move.
    """
    edited = {gi for gi, _fi in target.replacements} | {
        g["index"] for g in target.layout.groups if g["name"] in extra_files}
    pins, ghosts = {}, []
    with open(target.data_path, "rb") as f:
        for g in target.layout.groups:
            if _is_song_group(g["name"]):
                # keep_songs: an existing song's charts/textures stay put;
                # only the (re-encoded) audio streams are re-packed.
                if keep_songs and not g["name"].startswith("sound.stream.music_"):
                    pins[g["name"]] = g["sector"]
                continue
            if g["index"] in edited:
                f.seek(g["sector"] * 2048)
                ghosts.append((g["sector"] * 2048, f.read(g["packed_size"])))
            else:
                pins[g["name"]] = g["sector"]
    return pins, ghosts


def assemble(target, sources, plan, precomputed_db, log=lambda s: None,
             audio_override=None, audio_rate=None, workers=None,
             audio_cache=None, keep_layout=True, repack=True,
             audio_mono=False, audio_added_only=False) -> dict:
    """WORKER-THREAD step: harvest every added song and emit the merged archive.

    Copies each planned song's chart/texture/audio groups (and its shared-group
    select files) verbatim from its source, stages the precomputed DB bins, and
    does one archive_builder.build_archive. Returns
    {"list", "data", "added", "total", "errors", "warnings"}. No QWidget here.

    audio_override: optional {sid: vag_bytes} — substitute an added song's
    sound stream at HARVEST time (e.g. a re-encoded/compressed version). This
    is the only way a smaller stream actually shrinks the output: build_archive
    appends new groups and never shrinks, so post-rebuild replacement of an
    already-appended stream would only leave padding.

    audio_rate: re-encode EVERY song stream (target and added) at this rate
    (e.g. 32000) and repack the archive densely. The game takes the rate from
    each stream's header, so songs keep their speed and pitch; this is what
    lets more songs fit under the 2 GiB DATA.000 limit (MAX_DATA_SIZE).
    """
    from taiko2x6.songs import song_builder
    from taiko2x6.archive import archive_builder

    result = {"added": 0, "total": plan.total_after, "errors": [],
              "warnings": [], "list": None, "data": None}
    tgt_ids = _ordered_ids(target)

    new_groups = []
    extra_files = {}
    by_source = {}
    for it in plan.items:
        by_source.setdefault(it.source_label, []).append(it)
    src_by_label = {s.label: s for s in sources}
    for label, its in by_source.items():
        spec = src_by_label[label]
        src = open_pair(spec.list_path, spec.data_path, spec.fmt)
        try:
            hindex = HarvestIndex(src)
            for it in its:
                try:
                    g, x = harvest_song(src, it.sid, hindex)
                    if not any(gg["name"].startswith("fumen.") for gg in g):
                        result["warnings"].append(
                            f"{it.sid} ({label}): no chart groups found — skipped")
                        continue
                    ov = audio_override.get(it.sid) if audio_override else None
                    if ov is not None:
                        want = "sound.stream.music_" + it.sid
                        for gg in g:
                            if gg["name"] == want:
                                gg["files"] = [(fn, ov) for fn, _old in gg["files"]]
                    it.assets = song_asset_summary(g, x)
                    new_groups.extend(g)
                    for gn, files in x.items():
                        extra_files.setdefault(gn, []).extend(files)
                    result["added"] += 1
                    log(f"harvested {it.sid} ({label}): {it.assets}")
                except Exception as exc:
                    result["errors"].append(f"{it.sid} ({label}): {exc}")
        finally:
            src.close()

    compact = False
    if audio_rate or audio_mono:
        jobs = []
        for grp in target.layout.groups:
            if grp["name"].startswith("sound.stream.music_") and not audio_added_only:
                for e in target.layout.files_for_group(grp):
                    jobs.append(("t", (grp["index"], e["index"]), target.read_file(grp, e)))
        for gi, gg in enumerate(new_groups):
            if gg["name"].startswith("sound.stream.music_"):
                for fi, (_fn, data) in enumerate(gg["files"]):
                    jobs.append(("n", (gi, fi), data))
        before = sum(len(j[2]) for j in jobs)
        log(f"re-encoding {len(jobs)} song streams "
            f"({'mono' if audio_mono else 'stereo'}, {audio_rate or 'original'} Hz) "
            f"({before / 2**30:.2f} GiB)…")
        outs = _resample_many([j[2] for j in jobs], audio_rate, audio_cache,
                              audio_mono, workers, log)
        for (kind, ref, raw), new in zip(jobs, outs):
            if new is raw or new == raw:
                continue
            if kind == "t":
                target.stage_replace(ref[0], ref[1], new)
            else:
                gi, fi = ref
                fn = new_groups[gi]["files"][fi][0]
                new_groups[gi]["files"][fi] = (fn, new)
        after = sum(len(o) for o in outs)
        log(f"song audio {before / 2**30:.2f} -> {after / 2**30:.2f} GiB")
        result["audio_saved"] = before - after
        compact = repack

    # cut the transparent rows off every select title texture: the game
    # loads these groups whole, and 600 songs' worth no longer fits the heap
    trim_title_textures(target, log)
    for gn, files in extra_files.items():
        extra_files[gn] = [(fn, _trim_title_file(gn, data)) for fn, data in files]
    for fn, data in precomputed_db.items():
        ent = song_builder.find_named_entry(target, fn)
        if ent:
            target.stage_replace(ent[0]["index"], ent[1]["index"], data)
    log(f"rebuilding merged DATA.000 (+{len(new_groups)} groups, "
        f"{sum(len(v) for v in extra_files.values())} select files"
        f"{', repacked' if compact else ''})…")
    pins, ghosts = ((_retail_layout(target, extra_files, keep_layout)
                     if compact else (None, None)))
    list_bytes, data_bytes = archive_builder.build_archive(
        target, new_groups, extra_files, compact=compact,
        pin_sectors=pins, ghosts=ghosts)
    result["list"] = list_bytes
    result["data"] = data_bytes
    result["total"] = len(tgt_ids) + result["added"]
    return result


def build_omnimix(target, sources, plan, template_id=None,
                  log=lambda s: None) -> dict:
    """Convenience (headless/CLI): precompute_db + assemble in one call.

    Fine off-thread only where no QApplication event loop matters (scripts,
    tests). The GUI uses precompute_db (GUI thread) + assemble (worker) instead.
    """
    db = precompute_db(target, plan, template_id)
    return assemble(target, sources, plan, db, log=log)

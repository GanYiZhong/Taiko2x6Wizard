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
#  reading per-song metadata (stars / genre) in the source's own DB variant
# --------------------------------------------------------------------------- #
CPOP = 8                 # C-POP song genre
CPOP_HEADER = 15         # its folder header row ("selcpp")


def musicinfo_genres(raw: bytes) -> dict:
    """{song id: genre} straight from musicinfo.bin's own rows.

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
        return {}
    t14 = struct.unpack_from("<I", raw, 0x20)[0] == MI.HEADER_SIZE_T14
    if t14:
        try:
            m = MI.parse(raw)
            rows, pool_b = m.sec0, m.pool
        except Exception:
            return {}
    else:
        hdr = struct.unpack_from("<13I", raw, 0)
        count, o0, o5 = hdr[0], hdr[7], hdr[12]
        if not count or (o0 - 0x34) % count or o5 > len(raw):
            return {}
        rs = (o0 - 0x34) // count
        if rs < 12:
            return {}
        rows = [struct.unpack_from("<3i", raw, 0x34 + i * rs) for i in range(count)]
        pool_b = raw[o5:]

    def cs(off):
        if not 0 <= off < len(pool_b):
            return None
        end = pool_b.find(b"\0", off)
        return pool_b[off:end].decode("ascii", "replace") if end > off else None

    out = {}
    for r in rows:
        sid = cs(r[0])
        if not sid:
            continue
        g = r[2]
        if 0 <= g <= 6 or g == CPOP:    # 8 = C-POP (T11T/T12T, TK14 .0001)
            out[sid] = g
    return out


def read_song_meta(archive) -> dict:
    """{sid: {"stars":[e,n,h,o], "genre":int}} read via SongManager.

    SongManager auto-detects the archive's musicinfo variant, so this is correct
    for both T8- and T14-shaped sources. Requires a QApplication (SongManager is
    a QWidget); callers on a worker thread must have created one already.
    """
    from taiko2x6.songs import song_builder
    from taiko2x6.songs.song_manager import SongManager
    from taiko2x6.bineditors import bineditor_tuning as TU
    tuning = song_builder._read_named(archive, "tuning.bin")
    sm = SongManager(
        song_builder._read_named(archive, "musicinfo.bin"),
        tuning,
        song_builder._read_named(archive, "streaminfo.bin"))
    meta = {s.id: {"stars": list(s.stars), "genre": s.genre} for s in sm._songs}
    # SongManager pairs musicinfo rows with ids by position, which drifts on
    # other builds; take each song's genre from its own musicinfo row instead.
    genres = musicinfo_genres(song_builder._read_named(archive, "musicinfo.bin"))
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


# --------------------------------------------------------------------------- #
#  harvesting one song's assets (verbatim)
# --------------------------------------------------------------------------- #
def _fumen_belongs(group_name: str, sid: str) -> bool:
    m = _FUMEN_RE.match(group_name)
    return bool(m) and m.group("id") == sid


def harvest_song(archive, sid: str) -> tuple:
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
    """
    from taiko2x6.charts import sht_t7
    new_groups = []
    extra_files = {}
    want_select = {f"select_full_{sid}", f"select_short_{sid}"}
    non_name = f"select_non_{sid}"
    for grp in archive.layout.groups:
        gn = grp["name"]
        comp = grp.get("compression", 2)
        per_song = (
            _fumen_belongs(gn, sid)
            or gn == f"sound.stream.music_{sid}"
            or any(gn == f"music_texture.{t}_{sid}" for t in _TEX_TYPES))
        if per_song:
            files = [(e["name"], archive.read_file(grp, e))
                     for e in archive.layout.files_for_group(grp)]
            g = {"name": gn, "files": files, "compression": comp}
            if gn.startswith("fumen."):
                conv = [sht_t7.ensure_gen2(d) for _n, d in files]
                if any(c for _d, c in conv):
                    g["files"] = [(n, d) for (n, _o), (d, _c) in zip(files, conv)]
                    g["converted"] = True
            new_groups.append(g)
        elif gn == "music_texture.music_select":
            for e in archive.layout.files_for_group(grp):
                if e["name"] in want_select:
                    extra_files.setdefault(gn, []).append(
                        (e["name"], archive.read_file(grp, e)))
        elif gn.startswith("music_texture.music_select_"):
            for e in archive.layout.files_for_group(grp):
                if e["name"] == non_name:
                    extra_files.setdefault(gn, []).append(
                        (e["name"], archive.read_file(grp, e)))
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
    tu = song_builder._read_named(archive, "tuning.bin")
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
        for spec, src in opened:
            meta = read_song_meta(src)
            metas[spec.label] = meta
            for sid in _ordered_ids(src):
                if sid in have:
                    continue                    # already in target (14 wins)
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
        gfix, sfix, order = _resolve_trusted(target, opened, items, metas, log)
        for sid, g in gfix.items():
            fixes.setdefault(sid, g)          # the C-POP refiling wins
        star_fixes.update(sfix)
    finally:
        for _spec, src in opened:
            src.close()
    if skipped_titles:
        log(f"same title under another id, skipped: {len(skipped_titles)}")
    return MergePlan(target_ids=target_ids, items=items, skipped_dupes=skipped,
                     skipped_titles=skipped_titles, genre_fixes=fixes,
                     star_fixes=star_fixes, order=order)


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
            return musicinfo_genres(song_builder._read_named(arc, "musicinfo.bin"))
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


def _resolve_trusted(target, opened, items, metas, log=lambda s: None):
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
    star fixes, rank) where rank[sid] = (source index, row in that source)
    orders each genre block like the games it came from. C-POP songs are
    left to the C-POP rule.
    """
    from taiko2x6.songs import song_builder

    from taiko2x6.bineditors import bineditor_tuning as TU

    def info(arc):
        try:
            ids = list(_ordered_ids(arc))
            g = musicinfo_genres(song_builder._read_named(arc, "musicinfo.bin"))
        except Exception:
            return [], {}, False
        try:
            t14 = TU.is_t14_layout(song_builder._read_named(arc, "tuning.bin"))
        except Exception:
            t14 = False
        return ids, g, t14

    if not opened:
        return {}, {}, {}
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

    rank, gfix, sfix = {}, {}, {}
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
    log(f"trust: {len(gfix)} target songs refiled, {len(sfix)} target ratings and "
        f"{regenre} added songs' folders taken from their first trusted source")
    return gfix, sfix, rank


def _ordered_ids(archive):
    from taiko2x6.songs import song_builder
    return song_builder.song_ids(archive)


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
        tuning = song_builder._read_named(target, "tuning.bin")
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
    out = set_musicinfo_genres(mi, getattr(plan, "genre_fixes", None) or {},
                               getattr(plan, "order", None) or {})
    if out != mi:
        res[MUSICINFO] = out
    star_fixes = getattr(plan, "star_fixes", None) or {}
    if star_fixes:
        from taiko2x6.songs.song_manager import TUNING
        from taiko2x6.bineditors import bineditor_tuning as TU
        tu = res.get(TUNING) or song_builder._read_named(target, "tuning.bin")
        if TU.is_t14_layout(tu):
            res[TUNING] = TU.set_t14_stars_by_id(tu, star_fixes)
    return res


def set_musicinfo_genres(raw: bytes, genres: dict, order: dict = None) -> bytes:
    """Set column 2 (genre) of the musicinfo rows whose id is in `genres`."""
    from taiko2x6.bineditors import bineditor_musicinfo as MI
    m = MI.parse(raw)
    pool = m.pool
    for rec in m.sec0:
        o = rec[0]
        sid = pool[o:pool.find(b"\0", o)].decode("ascii", "replace")
        if sid in genres:
            rec[2] = int(genres[sid])
    group_rows_by_genre(m, order)
    fix_genre_jumps(m)
    return MI.serialize(m)


SONG_GENRES = (0, 1, 2, 3, 4, 5, 6, CPOP)


def group_rows_by_genre(m, order_by_sid: dict = None) -> None:
    """Reorder T14 musicinfo rows so every genre is one contiguous block.

    After a folder is picked, song select walks the rows in file order (only
    the optional re-sort uses the col13..16 orders). Stock TK14 stores its
    rows grouped by genre; rows appended by the merge land at the end in id
    order, which scatters genres inside each folder. The layout becomes:
    folder headers, then one block per genre in the order the genres first
    appear (C-POP last if new). Inside a block rows follow `order_by_sid`
    (sid -> sort key, e.g. (source, row) from _resolve_trusted); rows without
    a key go last, and ties keep the existing order. Every row index stored in the file is remapped: col10 (ura row), col13
    and col14 (own index), SEC1, SEC4 field 0, SEC6 and SEC7.
    """
    if getattr(m, "variant", "") != "t14" or not m.sec0:
        return
    rows = m.sec0
    headers = [i for i, r in enumerate(rows) if r[2] not in SONG_GENRES]
    order = []
    for r in rows:
        g = r[2]
        if g in SONG_GENRES and g not in order:
            order.append(g)
    key = {}
    if order_by_sid:
        pool = m.pool
        last = (1 << 30,)
        for i, r in enumerate(rows):
            sid = pool[r[0]:pool.find(bytes(1), r[0])].decode("ascii", "replace")
            key[i] = order_by_sid.get(sid, last) + (i,)
    new = headers + [i for g in order
                     for i in sorted((i for i, r in enumerate(rows) if r[2] == g),
                                     key=lambda i: key.get(i, (i,)))]
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
    for genre, slot in GENRE_JUMP_SLOT.items():
        if slot >= len(sec6):
            continue
        cur = sec6[slot]
        if 0 <= cur < len(rows) and rows[cur][2] == genre:
            continue
        first = next((i for i, r in enumerate(rows) if r[2] == genre), None)
        if first is not None:
            sec6[slot] = first


def _resample_job(args):
    raw, rate, cache, mono = args
    import hashlib, os
    from taiko2x6.audio import vagtool
    path = None
    if cache:
        path = os.path.join(cache, "%s_%d%s.vag" % (hashlib.sha1(raw).hexdigest(),
                                                    rate or 0, "m" if mono else ""))
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
    out = vagtool.resample_vag(raw, rate, mono=mono)
    if path:
        with open(path + ".tmp", "wb") as f:
            f.write(out)
        os.replace(path + ".tmp", path)
    return out


def _map_parallel(func, items, workers=None, log=lambda s: None):
    """func over items in worker processes (ordered); serial if that fails
    (e.g. a frozen build without multiprocessing support)."""
    import os
    from concurrent.futures import ProcessPoolExecutor
    n = len(items)
    try:
        out = []
        with ProcessPoolExecutor(max_workers=workers or os.cpu_count() or 2) as ex:
            for i, r in enumerate(ex.map(func, items, chunksize=1), 1):
                out.append(r)
                if i % 25 == 0 or i == n:
                    log(f"  audio {i}/{n}")
        return out
    except Exception as exc:                     # pragma: no cover - fallback
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
            for it in its:
                try:
                    g, x = harvest_song(src, it.sid)
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
        if audio_cache:
            import os
            os.makedirs(audio_cache, exist_ok=True)
        outs = _map_parallel(_resample_job,
                             [(j[2], audio_rate, audio_cache, audio_mono) for j in jobs],
                             workers, log)
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

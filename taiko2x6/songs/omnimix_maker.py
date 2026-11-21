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
    with ps2hdd.write_session(str(img), undo=undo, log=log) as h:
        log(f"writing DATA.000 {len(data_bytes):,} B → {partition}…")
        h.pfs_write(partition, "/DATA.000", data_bytes)
        log(f"writing list.bin {len(list_bytes):,} B → {partition}…")
        h.pfs_write(partition, "/list.bin", list_bytes)


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


def song_id_set(archive) -> set:
    from taiko2x6.songs import song_builder
    return set(song_builder.song_ids(archive))


# --------------------------------------------------------------------------- #
#  reading per-song metadata (stars / genre) in the source's own DB variant
# --------------------------------------------------------------------------- #
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

    @property
    def total_after(self) -> int:
        return len(self.target_ids) + len(self.items)


def plan_merge(target, sources, log=lambda s: None) -> MergePlan:
    """Decide which songs to add. 14 is the base: keep every target song, and
    from the sources add only ids the target does not already have. If the same
    new id appears in several sources, the FIRST source in `sources` order wins
    (put the newest/most-complete version first).
    """
    target_ids = list(_ordered_ids(target))
    have = set(target_ids)
    items = []
    skipped = {}
    seen_new = set()
    for spec in sources:
        src = open_pair(spec.list_path, spec.data_path, spec.fmt)
        try:
            meta = read_song_meta(src)
            for sid in _ordered_ids(src):
                if sid in have:
                    continue                    # already in target (14 wins)
                if sid in seen_new:
                    skipped.setdefault(sid, []).append(spec.label)
                    continue                    # an earlier source already added it
                m = meta.get(sid, {"stars": [1, 1, 1, 1], "genre": 0})
                items.append(PlanItem(sid=sid, source_label=spec.label,
                                      stars=m["stars"], genre=m["genre"]))
                seen_new.add(sid)
            log(f"{spec.label}: +{sum(1 for it in items if it.source_label == spec.label)} new")
        finally:
            src.close()
    return MergePlan(target_ids=target_ids, items=items, skipped_dupes=skipped)


def _ordered_ids(archive):
    from taiko2x6.songs import song_builder
    return song_builder.song_ids(archive)


# --------------------------------------------------------------------------- #
#  choosing a DB template row in the target
# --------------------------------------------------------------------------- #
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
        song_builder._db_add_song(sm, it.sid, k_template, stars)
    return sm._build_result()


def assemble(target, sources, plan, precomputed_db, log=lambda s: None,
             audio_override=None) -> dict:
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

    for fn, data in precomputed_db.items():
        ent = song_builder.find_named_entry(target, fn)
        if ent:
            target.stage_replace(ent[0]["index"], ent[1]["index"], data)
    log(f"rebuilding merged DATA.000 (+{len(new_groups)} groups, "
        f"{sum(len(v) for v in extra_files.values())} select files)…")
    list_bytes, data_bytes = archive_builder.build_archive(
        target, new_groups, extra_files)
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

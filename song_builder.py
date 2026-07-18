#!/usr/bin/env python3
"""
Custom Song Builder — ties the whole toolchain together.

Given a target song slot (one of the 90 existing songs), a TJA chart, an audio
file (wav/ogg) and the title metadata, this regenerates every game asset for
that slot:

  * textures : the 8 per-song name textures (games / kenri_song / result /
               songlevel / topten / total_result / select_full / select_non)
               via songtex_all, spliced into the existing nut templates.
  * charts   : the up-to-8 sht fumen (1P/2P × easy/normal/hard/oni) via tja2sht.
  * audio    : the music_<id>/vag stream via vagtool, staged into the
               sound.stream.music_<id> group inside DATA.000 (compression 6).
  * difficulty stars : tuning.bin star ratings from the TJA #LEVEL: lines.

All asset changes (textures, charts, stars, audio) are staged into the open
archive via stage_replace; saving via the main window writes them to DATA.000.

This replaces an EXISTING slot — the practical custom-song workflow. Creating a
brand-new 91st slot would additionally require adding new groups to DATA.000,
which is out of scope here.
"""
from __future__ import annotations

import logging
from pathlib import Path

import appconfig
import tja2sht
import vagtool
import songtex_all
import tim2
import bineditor_tuning as TU

# Child of the app's "taiko" logger — inherits its console handler when the GUI
# configured one; harmless (no handler) when run headless.
logger = logging.getLogger("taiko.songbuilder")


def _tee(cb):
    """Wrap a milestone callback so each message also reaches the console log."""
    def _log(msg):
        cb(msg)
        logger.info(msg)
    return _log

# difficulty letter (fumen suffix) -> course name
_DIFF_LETTER = {"e": "easy", "n": "normal", "h": "hard", "m": "oni"}
_DIFF_INDEX = {"easy": 0, "normal": 1, "hard": 2, "oni": 3}

# Unambiguous sentinel for build-worker failures. A unique object can never be
# confused with a successful build result (bytes / tuple / dict), unlike the
# old string "ERROR" which collided structurally with the success 3-tuple.
_BUILD_ERROR = object()


def _read_tja_text(path: str) -> tuple:
    """Decode a TJA file, tolerating Shift-JIS (cp932) as well as UTF-8.

    Returns (text, warning_or_None). Many real-world TJAs are Shift-JIS; reading
    them as UTF-8 with errors='replace' silently turns metadata into U+FFFD and
    appears to succeed while writing garbage. Try real encodings first; only
    fall back to lossy UTF-8 (with a warning) if none decode cleanly.
    """
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "shift_jis", "cp932"):
        try:
            return raw.decode(enc), None
        except UnicodeDecodeError:
            continue
    text = raw.decode("utf-8", errors="replace")
    return text, f"TJA decode: no clean encoding for {Path(path).name}; used lossy UTF-8"


# --------------------------------------------------------------------------- #
#  archive asset discovery
# --------------------------------------------------------------------------- #
def song_ids(archive) -> list:
    """The 90 song ids in index order (from tuning's music_ pool)."""
    tu = TU.parse(_read_named(archive, "tuning.bin"))
    return [s.text[len("music_"):] for s in tu.strings if s.text.startswith("music_")]


def _read_named(archive, filename):
    for grp in archive.layout.groups:
        for e in archive.layout.files_for_group(grp):
            if e["name"].lower() == filename.lower():
                return archive.read_file(grp, e)
    raise FileNotFoundError(filename)


def find_named_entry(archive, filename):
    for grp in archive.layout.groups:
        for e in archive.layout.files_for_group(grp):
            if e["name"].lower() == filename.lower():
                return grp, e
    return None


def find_textures(archive, sid: str) -> list:
    """Yield (type_name, group, entry, template_bytes) for sid's name textures."""
    out = []
    for grp in archive.layout.groups:
        gn = grp["name"]
        if not gn.startswith("music_texture"):
            continue
        for e in archive.layout.files_for_group(grp):
            name = e["name"]
            tname = None
            # groups ending in _<id> hold a single 'nut'
            for t in ("games", "kenri_song", "result", "songlevel",
                      "topten", "total_result"):
                if gn == f"music_texture.{t}_{sid}" and name.lower() == "nut":
                    tname = t
            # The base music_select group holds TWO plates per song: the tall
            # select_full and the shorter select_short (both white+outline).
            if gn == "music_texture.music_select":
                if name == f"select_full_{sid}":
                    tname = "select_full"
                elif name == f"select_short_{sid}":
                    tname = "select_short"
            # select_non lives in the per-difficulty groups
            # music_texture.music_select_<easy|normal|hard|mania|music> — every
            # one carries its own copy that must be updated too.
            elif (gn.startswith("music_texture.music_select_")
                    and name == f"select_non_{sid}"):
                tname = "select_non"
            if tname:
                out.append((tname, grp, e, archive.read_file(grp, e)))
    return out


def find_charts(archive, sid: str) -> list:
    """Yield (player, diff_letter, group, entry) for sid's sht charts."""
    out = []
    for grp in archive.layout.groups:
        gn = grp["name"]
        if not gn.startswith("fumen."):
            continue
        stem = gn[len("fumen."):]                 # e.g. 10tai1p_e
        for player in ("1p", "2p"):
            for letter in ("e", "n", "h", "m"):
                if stem == f"{sid}{player}_{letter}":
                    for e in archive.layout.files_for_group(grp):
                        if e["name"].lower() == "sht":
                            out.append((player, letter, grp, e))
    return out


# --------------------------------------------------------------------------- #
#  audio/chart sync gap (auto-detect)
# --------------------------------------------------------------------------- #
def _first_note_time_ms(model) -> float | None:
    """Absolute time (ms) of the chart's first note, from a parsed sht model."""
    tracks = model.get("tracks") or []
    notes = model.get("notes") or []
    if not tracks or not notes:
        return None
    for i, t in enumerate(tracks):
        for s in t["subtracks"]:
            if s["noteCount"] > 0 and s["noteIndexSt"] <= 0 < s["noteIndexSt"] + s["noteCount"]:
                measure_ms = (tracks[i + 1]["time"] - t["time"]) if i + 1 < len(tracks) else 0.0
                pos = notes[0].get("measure", 0)
                return float(t["time"]) + (pos / 48.0) * measure_ms
    for t in tracks:
        if any(s["noteCount"] > 0 for s in t["subtracks"]):
            return float(t["time"])
    return float(tracks[0]["time"])


def prepare_tja_for_game(tja_text: str) -> dict:
    """Neutralise the TJA OFFSET: bake it into the audio, blank-lead the chart.

    The game has no OFFSET concept -- it starts the BGM and the chart together at
    t=0 -- so the sync has to live in the audio. This is the same transform as the
    community `test.py` preprocessor that the user's known-good songs were built
    with, and it is TWO HALVES OF ONE PAIR:

      audio: prepend one blank measure, then apply OFFSET
             (OFFSET>0 -> yet more silence; OFFSET<0 -> cut that much off the front)
             ...and then prepend ONE MORE blank measure
      chart: OFFSET:0, plus one empty measure inserted after every #START

    That second measure is not a typo. The pipeline the user verified in-game was
    test.py's output fed through the tool's old auto-gap, and on an OFFSET:0 TJA
    that auto-gap prepended a further `one_measure`. So the shift that actually
    works is:

        trim_ms = max(0, -(one_measure + OFFSET*1000))       # test.py's cut
        lead_ms = one_measure + max(0, one_measure + OFFSET*1000)

    i.e. 2 x one_measure + OFFSET overall. Deriving one measure from the TJA
    OFFSET convention -- as an earlier version of this function did -- desyncs
    every song by a whole measure. The convention is not the authority here; the
    in-game result is.

    Returns {tja, lead_ms, trim_ms, one_measure_ms, offset_ms} -- `tja` is the
    rewritten chart text and MUST be the one that gets converted, or the halves
    come apart.

    Do the two halves together, here, exactly once. The bug this replaces: an
    already-preprocessed TJA (OFFSET:0) fed to the old auto-gap scored
    `gap = one_measure + 0` and prepended a SECOND blank measure, desynchronising
    the song by a full measure (VICTORIA: 1297 ms late).
    """
    import re
    info = {"tja": tja_text, "lead_ms": 0.0, "trim_ms": 0.0,
            "one_measure_ms": None, "offset_ms": None}
    if not tja_text:
        return info
    bpm_m = re.search(r'BPM:\s*([\d.]+)', tja_text)
    off_m = re.search(r'OFFSET:\s*(-?[\d.]+)', tja_text)
    if not bpm_m:
        return info
    bpm = float(bpm_m.group(1))
    if bpm <= 0:
        return info
    offset_ms = (float(off_m.group(1)) if off_m else 0.0) * 1000.0
    one_measure_ms = 60000.0 / bpm * 4.0
    # Two stages, exactly as the verified pipeline ran them: test.py's own
    # blank-measure + OFFSET shift, then a second blank measure on top.
    stage1 = one_measure_ms + offset_ms
    trim = max(0.0, -stage1)
    lead = one_measure_ms + max(0.0, stage1)

    out = []
    for line in tja_text.splitlines():
        s = line.strip()
        if s.upper().startswith("OFFSET:"):
            out.append("OFFSET:0")
        else:
            out.append(line)
        # #START may carry a player arg (#START P1)
        if s.upper() == "#START" or s.upper().startswith("#START "):
            out.extend(["#BARLINEOFF", ",", "#BARLINEON"])

    info.update(tja="\n".join(out) + "\n",
                lead_ms=lead, trim_ms=trim,
                one_measure_ms=one_measure_ms, offset_ms=offset_ms)
    return info


def _sync_audio_args(sync, manual_nudge_ms: float, log) -> tuple:
    """(lead_silence_ms, trim_start_ms) for vagtool, from the computed sync.

    `manual_nudge_ms` is added on top of whatever the TJA implies -- it exists to
    taste-tune a song that feels a hair off, NOT to supply the sync itself.
    """
    lead = float(sync["lead_ms"]) if sync else 0.0
    trim = float(sync["trim_ms"]) if sync else 0.0
    if manual_nudge_ms:
        # Nudge the silence, keeping the computed trim intact -- the trim is what
        # the verified pipeline cut, and collapsing the two would silently hand
        # back music it had removed. Only if the nudge eats all the silence does
        # the remainder become extra trim.
        lead += float(manual_nudge_ms)
        if lead < 0:
            trim += -lead
            lead = 0.0
        log("audio: manual nudge %+.0fms" % manual_nudge_ms)
    if lead:
        log("audio: prepending %.0fms leading silence" % lead)
    if trim:
        log("audio: trimming %.0fms off the front of the music" % trim)
    if not lead and not trim:
        log("audio: no shift needed")
    return lead, trim


# --------------------------------------------------------------------------- #
#  build
# --------------------------------------------------------------------------- #
def build_song(archive, sid: str, title: str, lyricist: str = "", composer: str = "",
               copyright_: str = "", tja_text: str | None = None,
               audio_path: str | None = None,
               do_textures=True, do_charts=True, do_audio=True, do_stars=True,
               lead_silence_ms: float = 0.0,
               log=lambda s: None) -> dict:
    """Regenerate assets for song slot `sid`. Returns a summary dict.

    All bytes (textures, charts, stars, audio) are staged into `archive` via
    stage_replace; nothing is written to disk here. The audio VAG replaces the
    existing entry in the sound.stream.music_<id> group inside DATA.000,
    preserving that entry's original compression. `summary["warnings"]` lists
    non-fatal notes (e.g. clamped star levels); `summary["errors"]` lists
    per-asset failures without aborting the rest of the build.

    Sync is handled here, once, from the RAW TJA (see prepare_tja_for_game): the
    OFFSET is baked into the audio and the chart gets a blank lead measure. Feed
    it the original .tja/.ogg -- a chart that has already been through the
    community test.py preprocessor is ALREADY shifted, and processing it again
    desyncs it by a whole measure. `lead_silence_ms` is only a manual nudge on
    top of that, for taste.
    """
    log = _tee(log)
    logger.info("build_song %s: title=%r textures=%s charts=%s audio=%s stars=%s",
                sid, title, do_textures, do_charts, do_audio, do_stars)
    summary = {"textures": 0, "charts": 0, "audio": None, "stars": None,
               "errors": [], "warnings": []}

    # ---- sync: bake OFFSET into the audio, blank-lead the chart (as a pair) ----
    sync = prepare_tja_for_game(tja_text) if tja_text else None
    if sync and sync["one_measure_ms"] is not None:
        tja_text = sync["tja"]
        log("sync: BPM measure %.0fms, OFFSET %.0fms -> %s"
            % (sync["one_measure_ms"], sync["offset_ms"],
               ("prepend %.0fms silence" % sync["lead_ms"]) if sync["lead_ms"]
               else ("trim %.0fms off the music" % sync["trim_ms"]) if sync["trim_ms"]
               else "no shift"))
        summary["sync"] = {k: sync[k] for k in
                           ("lead_ms", "trim_ms", "one_measure_ms", "offset_ms")}
    elif tja_text:
        summary["warnings"].append(
            "no BPM in the TJA -- audio sync left alone; the chart may play early/late")
        log("sync: no BPM found, skipping offset neutralisation")

    # ---- textures ----
    if do_textures and title:
        try:
            for tname, grp, e, template in find_textures(archive, sid):
                try:
                    nut = songtex_all.render_texture(
                        tname, template, title, lyricist=lyricist,
                        composer=composer, copyright=copyright_)
                    if nut != template:
                        archive.stage_replace(grp["index"], e["index"], nut)
                        summary["textures"] += 1
                        log(f"texture: {grp['name']}")
                except Exception as exc:
                    summary["errors"].append(f"texture {tname}: {exc}")
        except Exception as exc:
            summary["errors"].append(f"textures: {exc}")

    # ---- charts ----
    tja_levels = {}
    if (do_charts or do_stars) and tja_text:
        try:
            parsed = tja2sht.parse_tja(tja_text)
            tja_levels = _course_levels(parsed)
        except Exception as exc:
            summary["errors"].append(f"tja parse: {exc}")
    if do_charts and tja_text:
        for player, letter, grp, e in find_charts(archive, sid):
            course = _DIFF_LETTER[letter]
            try:
                sht = tja2sht.convert_tja(tja_text, course, player)
                parsed_sht = tja2sht.parse_sht(sht)  # validate
                # DIFFICULTY_NOTE_LIMIT is the densest note count *observed* in the
                # retail corpus for each difficulty — a heuristic upper bound, not a
                # proven hardware buffer size. The freeze cause we actually proved is
                # the unknown2 content hash, which we now compute correctly. So over
                # the corpus-max is a WARNING, not a skip: write the chart anyway and
                # let the user confirm in-game (dense X JAPAN Easy/Normal courses can
                # legitimately exceed the retail max).
                limit = tja2sht.DIFFICULTY_NOTE_LIMIT.get(course)
                ncount = len(parsed_sht["notes"])
                if limit is not None and ncount > limit:
                    summary["warnings"].append(
                        f"chart {grp['name']}: {ncount} notes exceeds the retail "
                        f"{course} max ({limit}). Writing anyway — verify in-game; "
                        f"if it freezes, provide a lighter {course} course.")
                    log(f"chart: {grp['name']} ({ncount} notes, OVER retail "
                        f"{course} max {limit} — writing anyway)")
                else:
                    log(f"chart: {grp['name']} ({ncount} notes)")
                archive.stage_replace(grp["index"], e["index"], sht)
                summary["charts"] += 1
            except Exception as exc:
                summary["errors"].append(f"chart {grp['name']}: {exc}")

    # ---- difficulty stars (tuning.bin) ----
    if do_stars and tja_levels:
        try:
            ent = find_named_entry(archive, "tuning.bin")
            ids = song_ids(archive)
            if ent and sid in ids:
                k = ids.index(sid)
                tu_bytes = archive.read_file(*ent)
                tu = TU.parse(tu_bytes)
                changed = False
                for course, lvl in tja_levels.items():
                    di = _DIFF_INDEX.get(course)
                    if di is None:
                        continue
                    clamped = max(1, min(10, lvl))
                    if clamped != lvl:
                        summary["warnings"].append(
                            f"stars: {course} level {lvl} clamped to {clamped}")
                    for blk in (2 * k, 2 * k + 1):
                        if blk < len(tu.blocks):
                            tu.blocks[blk].records[di].values[5] = clamped
                            changed = True
                if changed:
                    archive.stage_replace(ent[0]["index"], ent[1]["index"],
                                          TU.serialize(tu))
                    summary["stars"] = tja_levels
                    log(f"stars: {tja_levels}")
        except Exception as exc:
            summary["errors"].append(f"stars: {exc}")

    # ---- audio: replace the sound.stream.music_<id> group's vag in DATA.000 ----
    if do_audio and audio_path:
        try:
            lead, trim = _sync_audio_args(sync, lead_silence_ms, log)
            vag = vagtool.convert_audio_file(audio_path, 44100,
                                             lead_silence_ms=lead,
                                             trim_start_ms=trim)
            ent = find_group_file(archive, f"sound.stream.music_{sid}", "vag")
            if ent:
                # stage_replace preserves the existing entry's per-entry
                # compression. The add path writes sound.stream as comp 6, so
                # surface the existing slot's comp to make a bad slot diagnosable.
                comp = ent[0].get("compression")
                archive.stage_replace(ent[0]["index"], ent[1]["index"], vag)
                summary["audio"] = f"sound.stream.music_{sid}/vag ({len(vag):,}B, comp {comp})"
                log(f"audio: staged sound.stream.music_{sid}/vag "
                    f"({len(vag):,}B, existing comp {comp})")
                if comp not in (None, 6):
                    summary["warnings"].append(
                        f"audio: slot comp {comp} != expected sound.stream comp 6")
            else:
                summary["errors"].append(
                    f"audio: stream group sound.stream.music_{sid} not found")
        except Exception as exc:
            summary["errors"].append(f"audio: {exc}")

    return summary


def find_group_file(archive, group_name: str, file_name: str):
    """Return (group, entry) for group_name/file_name, else None."""
    for grp in archive.layout.groups:
        if grp["name"] == group_name:
            for e in archive.layout.files_for_group(grp):
                if e["name"] == file_name:
                    return grp, e
    return None


_TEX_GROUP = {
    "games": "music_texture.games_{id}",
    "kenri_song": "music_texture.kenri_song_{id}",
    "result": "music_texture.result_{id}",
    "songlevel": "music_texture.songlevel_{id}",
    "topten": "music_texture.topten_{id}",
    "total_result": "music_texture.total_result_{id}",
}


def _db_add_song(sm, new_id: str, k_src: int, stars: list):
    """Programmatically add a song to a SongManager (reuses its add logic).

    NOTE: tightly coupled to SongManager internals — relies on the attributes
    _songs, _new_counter, _new_songs, _order, _added_anything and the _Song
    dataclass (k, sid, genre, stars, score/score3/score5, stream, stream_rec,
    _template_k, _is_new). If song_manager refactors these, update here.
    """
    from song_manager import _Song
    src = sm._songs[k_src]
    token = f"+{sm._new_counter}"
    sm._new_counter += 1
    sm._new_songs[token] = {"template_k": k_src, "template_id": src.id,
                            "id": new_id, "stars": list(stars)}
    sm._order.append(token)
    sm._added_anything = True
    ns = _Song(k=len(sm._songs), sid=new_id, genre=src.genre, stars=list(stars),
               score=src.score, score3=src.score3, score5=src.score5,
               stream=(f"music_{new_id}" if src.stream_rec is not None else ""),
               stream_rec=src.stream_rec)
    ns._template_k = k_src
    ns._is_new = True
    sm._songs.append(ns)


def resolve_new_stars(archive, k_src: int, tja_text) -> list:
    """Star ratings for a new song: TJA #LEVEL where present, else template's."""
    base = list(_template_stars(archive, k_src))
    if tja_text:
        try:
            lv = _course_levels(tja2sht.parse_tja(tja_text))
            for course, di in _DIFF_INDEX.items():
                if course in lv:
                    base[di] = max(1, min(10, lv[course]))
        except Exception:
            pass
    return base


def _assert_gui_thread(what: str) -> None:
    """Raise if not on the Qt GUI thread. SongManager is a QWidget; constructing
    it off the GUI thread is undefined behaviour. Enforces what was previously
    only caller discipline. No-op if Qt isn't importable (headless/no QApp)."""
    try:
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import QApplication
    except Exception:
        return
    app = QApplication.instance()
    if app is not None and QThread.currentThread() is not app.thread():
        raise RuntimeError(
            f"{what} must run on the GUI thread (it builds a QWidget); "
            f"call it on the main thread and pass the result into the worker")


def compute_db_add(archive, new_id: str, k_src: int, stars: list) -> dict:
    """Build the 3 DB bins for an added song. MUST run on the GUI thread (QWidget)."""
    _assert_gui_thread("compute_db_add")
    import song_manager
    sm = song_manager.SongManager(
        _read_named(archive, "musicinfo.bin"),
        _read_named(archive, "tuning.bin"),
        _read_named(archive, "streaminfo.bin"))
    _db_add_song(sm, new_id, k_src, stars)
    return sm._build_result()


def check_db_consistency(archive) -> dict:
    """Return the three DB song counts and whether they agree.

    A bootable add-new needs musicinfo.bin song records, tuning.bin ``music_``
    pool entries, and tuning.bin blocks (2 per song) to all describe the SAME
    number of songs. If they disagree the base archive is already corrupt, and
    building a new song on top of it would produce a non-bootable result.
    """
    import bineditor_musicinfo as MI
    mi = MI.parse(_read_named(archive, "musicinfo.bin"))
    tu = TU.parse(_read_named(archive, "tuning.bin"))
    mi_songs = len(mi.sec0)
    tuning_ids = sum(1 for s in tu.strings if s.text.startswith("music_"))
    tuning_songs = len(tu.blocks) // 2
    return {"musicinfo": mi_songs, "tuning_ids": tuning_ids,
            "tuning_blocks": tuning_songs,
            "consistent": mi_songs == tuning_ids == tuning_songs}


def prepare_new_song_db(archive, new_id: str, template_sid: str, tja_text,
                        stars=None) -> tuple:
    """GUI-thread prep: returns (precomputed_db, stars) for a new song.

    `stars` overrides the TJA/template-derived ratings — Gen3 songs carry their
    own in musicinfo, so they are passed straight through.
    """
    ids = song_ids(archive)
    if new_id in ids:
        raise ValueError(f"song id '{new_id}' already exists")
    if template_sid not in ids:
        raise ValueError(f"template song '{template_sid}' not found")
    con = check_db_consistency(archive)
    if not con["consistent"]:
        raise ValueError(
            "This archive's song-DB bins disagree on the song count, so adding a "
            "new song would produce a non-bootable file:\n"
            f"  musicinfo.bin songs : {con['musicinfo']}\n"
            f"  tuning.bin music_ ids: {con['tuning_ids']}\n"
            f"  tuning.bin blocks/2  : {con['tuning_blocks']}\n"
            "These must all be equal. Reload a clean DATA.000/LIST.BIN (or restore "
            "from the .bak) before adding a song. (Replacing an existing song still "
            "works on this archive.)")
    k_src = ids.index(template_sid)
    if stars is None:
        stars = resolve_new_stars(archive, k_src, tja_text)
    return compute_db_add(archive, new_id, k_src, stars), stars


def add_new_song(archive, new_id: str, title: str, template_sid: str,
                 lyricist="", composer="", copyright_="", tja_text=None,
                 audio_path=None, lead_silence_ms: float = 0.0,
                 precomputed_db=None, stars=None, charts=None, audio_vag=None,
                 log=lambda s: None) -> tuple:
    """Create a brand-new song slot `new_id` cloned from `template_sid`.

    Charts and audio can come from either of two sources:

      * TJA mode (`tja_text`, `audio_path`): charts are converted from the TJA
        and the music is re-synced against it (see prepare_tja_for_game).
      * Prepared mode (`charts`, `audio_vag`): charts arrive as ready .sht bytes
        keyed by course name ("easy"/"normal"/"hard"/"oni") and the music as
        ready VAG bytes. This is what gen3_song.load_song produces. No sync is
        applied: a Gen3 chart's measure times are already absolute against its
        own audio, so there is nothing to neutralise.

    The two are mutually exclusive per asset; `charts` wins over `tja_text` for
    the chart groups, `audio_vag` wins over `audio_path` for the music.

    Adds: DB entry (musicinfo+tuning+streaminfo), new texture groups, new chart
    groups, select_full/select_non files, and a new sound.stream.music_<id>
    audio group (compression 6) — all inside DATA.000. Returns
    (list_bytes, data_bytes, summary) — a full rebuilt archive ready to write.

    The DB-build step constructs a SongManager QWidget, so it must run on the
    GUI thread. When called from a worker thread, pass `precomputed_db` (built
    on the GUI thread via prepare_new_song_db) so no QWidget is created here.
    """
    import archive_builder
    log = _tee(log)
    logger.info("add_new_song %s (template %s): title=%r audio=%s",
                new_id, template_sid, title, bool(audio_path))
    summary = {"groups": 0, "extra_files": 0, "audio": None,
               "errors": [], "warnings": [], "db": []}

    # ---- sync: same matched pair as build_song (see prepare_tja_for_game) ----
    # Safe to do before the star/DB steps: the transform touches only OFFSET and
    # the measure list, never the LEVEL: headers those read.
    sync = prepare_tja_for_game(tja_text) if tja_text else None
    if sync and sync["one_measure_ms"] is not None:
        tja_text = sync["tja"]
        log("sync: BPM measure %.0fms, OFFSET %.0fms -> %s"
            % (sync["one_measure_ms"], sync["offset_ms"],
               ("prepend %.0fms silence" % sync["lead_ms"]) if sync["lead_ms"]
               else ("trim %.0fms off the music" % sync["trim_ms"]) if sync["trim_ms"]
               else "no shift"))
        summary["sync"] = {k: sync[k] for k in
                           ("lead_ms", "trim_ms", "one_measure_ms", "offset_ms")}
    elif tja_text:
        summary["warnings"].append(
            "no BPM in the TJA -- audio sync left alone; the chart may play early/late")
        log("sync: no BPM found, skipping offset neutralisation")

    ids = song_ids(archive)
    if new_id in ids:
        raise ValueError(f"song id '{new_id}' already exists")
    if template_sid not in ids:
        raise ValueError(f"template song '{template_sid}' not found")
    k_src = ids.index(template_sid)

    if stars is None:
        stars = resolve_new_stars(archive, k_src, tja_text)

    # ---- 1) DB add (musicinfo/tuning/streaminfo) ----
    # SongManager is a QWidget, so its construction must happen on the GUI thread.
    # When called from a worker thread, `precomputed_db` is passed in instead.
    try:
        if precomputed_db is None:
            precomputed_db = compute_db_add(archive, new_id, k_src, stars)
        for fn, data in precomputed_db.items():
            ent = find_named_entry(archive, fn)
            if ent:
                archive.stage_replace(ent[0]["index"], ent[1]["index"], data)
                summary["db"].append(fn)
        log(f"DB: added song {new_id} (stars {stars}); updated {summary['db']}")
    except Exception as exc:
        summary["errors"].append(f"db add: {exc}")

    # ---- 2) new groups: textures + charts ----
    new_groups = []
    extra_files = {}
    for tname, grp, e, template in find_textures(archive, template_sid):
        try:
            nut = songtex_all.render_texture(tname, template, title, lyricist=lyricist,
                                             composer=composer, copyright=copyright_)
            if tname in _TEX_GROUP:
                new_groups.append({"name": _TEX_GROUP[tname].format(id=new_id),
                                   "files": [("nut", nut)], "compression": 2})
            elif tname == "select_full":
                extra_files.setdefault(grp["name"], []).append((f"select_full_{new_id}", nut))
            elif tname == "select_short":
                extra_files.setdefault(grp["name"], []).append((f"select_short_{new_id}", nut))
            elif tname == "select_non":
                extra_files.setdefault(grp["name"], []).append((f"select_non_{new_id}", nut))
        except Exception as exc:
            summary["errors"].append(f"texture {tname}: {exc}")

    if charts or tja_text:
        for player, letter, grp, e in find_charts(archive, template_sid):
            course = _DIFF_LETTER[letter]
            try:
                if charts:
                    sht = charts.get(course)
                    if sht is None:
                        # The template has this difficulty but the source song
                        # does not. Skipping leaves the slot without a chart,
                        # so say so rather than writing an empty one.
                        summary["warnings"].append(
                            f"no {course} chart in the source; "
                            f"{player}_{letter} left empty")
                        continue
                else:
                    sht = tja2sht.convert_tja(tja_text, course, player)
                tja2sht.parse_sht(sht)
                new_groups.append({"name": f"fumen.{new_id}{player}_{letter}",
                                   "files": [("sht", sht)], "compression": 2})
            except Exception as exc:
                summary["errors"].append(f"chart {player}_{letter}: {exc}")
    summary["groups"] = len(new_groups)
    summary["extra_files"] = sum(len(v) for v in extra_files.values())
    log(f"new groups: {len(new_groups)}  extra select files: {summary['extra_files']}")

    # ---- 3) audio: add a new sound.stream.music_<id> group (comp 6, raw) ----
    if audio_vag or audio_path:
        try:
            if audio_vag:
                vag = audio_vag
                log(f"audio: using prepared VAG ({len(vag):,}B, no sync applied)")
            else:
                lead, trim = _sync_audio_args(sync, lead_silence_ms, log)
                vag = vagtool.convert_audio_file(audio_path, 44100,
                                                 lead_silence_ms=lead,
                                                 trim_start_ms=trim)
            new_groups.append({"name": f"sound.stream.music_{new_id}",
                               "files": [("vag", vag)], "compression": 6})
            summary["audio"] = f"sound.stream.music_{new_id}/vag ({len(vag):,}B in DATA.000)"
            log(f"audio: new group sound.stream.music_{new_id}/vag ({len(vag):,}B, comp 6)")
        except Exception as exc:
            summary["errors"].append(f"audio: {exc}")

    # ---- 4) rebuild full archive (applies staged DB edits + new groups + extra) ----
    log(f"rebuilding DATA.000 (+{len(new_groups)} groups)…")
    import time
    _t = time.perf_counter()
    list_bytes, data_bytes = archive_builder.build_archive(archive, new_groups, extra_files)
    logger.info("rebuild done: list %d B, DATA %d B in %.1fs",
                len(list_bytes), len(data_bytes), time.perf_counter() - _t)
    return list_bytes, data_bytes, summary


def _template_stars(archive, k: int) -> list:
    """Read the 4 course star ratings for slot `k` from tuning.bin.

    Mirrors the writer's bounds guard in build_song: on a short/corrupt tuning
    file (block index, record index, or value index out of range) this returns
    a sane default rather than raising IndexError up through prepare_new_song_db.
    """
    tu = TU.parse(_read_named(archive, "tuning.bin"))
    blk = 2 * k
    if blk >= len(tu.blocks):
        return [1, 1, 1, 1]
    block = tu.blocks[blk]
    out = []
    for di in range(4):
        try:
            out.append(block.records[di].values[5])
        except (IndexError, AttributeError):
            out.append(1)
    return out


def _course_levels(parsed) -> dict:
    """Extract {course_name: level} from a parsed TJA (tja2sht dict shape).

    parse_tja -> {'meta':..., 'courses': {course_index: {'meta': {'LEVEL':...}, ...}}}
    where course_index 0=easy 1=normal 2=hard 3=oni 4=ura/oni.
    """
    levels = {}
    courses = parsed.get("courses") if isinstance(parsed, dict) else getattr(parsed, "courses", None)
    if not isinstance(courses, dict):
        return levels
    # The engine has only 4 star slots, so Ura (cidx 4) and Oni (cidx 3) share
    # the single "oni" slot. Prefer Oni (3); only let Ura (4) fill "oni" when no
    # Oni level was produced, instead of relying on iteration order (last-wins).
    idxmap = {0: "easy", 1: "normal", 2: "hard", 3: "oni", 4: "oni"}
    oni_from_3 = False
    for cidx, c in sorted(courses.items(), key=lambda kv: (not isinstance(kv[0], int), kv[0])):
        meta = c.get("meta", {}) if isinstance(c, dict) else {}
        lvl = meta.get("LEVEL")
        if isinstance(cidx, int):
            course = idxmap.get(cidx)
        else:
            course = str(cidx).lower()
        if lvl is None or course not in _DIFF_INDEX:
            continue
        # Once Oni (cidx 3) has set "oni", don't let Ura (cidx 4) overwrite it.
        if course == "oni" and cidx == 4 and oni_from_3:
            continue
        try:
            levels[course] = int(lvl)
        except (TypeError, ValueError):
            continue
        if course == "oni" and cidx == 3:
            oni_from_3 = True
    return levels


# --------------------------------------------------------------------------- #
#  Qt wizard dialog
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtCore import Qt, QThread, Signal
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLineEdit,
        QPushButton, QFileDialog, QCheckBox, QPlainTextEdit, QLabel, QMessageBox,
        QWidget, QProgressDialog, QSpinBox, QDialogButtonBox,
    )

    class StarsDialog(QDialog):
        """Ask for the 4 course star ratings.

        Needed because 291 of the Gen3 songs are absent from musicinfo and so
        carry no ratings of their own. Silently keeping the template's would
        put visibly wrong difficulties on the song select screen, and there is
        nothing in the chart data to derive a rating from — only a person can
        say how hard a chart is.
        """

        def __init__(self, sid, initial, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Star ratings — %s" % sid)
            v = QVBoxLayout(self)
            v.addWidget(QLabel(
                "This song is not in the game's song list, so it has no star\n"
                "ratings. Set them here (1–10). Pre-filled from the template."))
            form = QFormLayout()
            self._spins = []
            for course, label in (("easy", "easy 簡單:"),
                                  ("normal", "normal 普通:"),
                                  ("hard", "hard 困難:"),
                                  ("oni", "oni 魔王:")):
                sp = QSpinBox()
                sp.setRange(1, 10)
                sp.setValue(int(initial[_DIFF_INDEX[course]]))
                form.addRow(label, sp)
                self._spins.append(sp)
            v.addLayout(form)
            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)
            v.addWidget(bb)

        def stars(self):
            return [sp.value() for sp in self._spins]

    class _BuildWorker(QThread):
        """Runs a slow build function off the UI thread."""
        log_sig = Signal(str)
        # result, or (_BUILD_ERROR, exc, traceback) on failure
        done_sig = Signal(object)

        def __init__(self, fn):
            super().__init__()
            self._fn = fn

        def run(self):
            try:
                self.done_sig.emit(self._fn(lambda s: self.log_sig.emit(s)))
            except Exception as exc:
                import traceback
                self.done_sig.emit((_BUILD_ERROR, exc, traceback.format_exc()))

    class SongBuilderDialog(QDialog):
        def __init__(self, archive, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Custom Song Builder")
            self.resize(640, 560)
            self.archive = archive
            self.changed = False
            self.new_archive = None      # (list_bytes, data_bytes) when a new song is built
            self._build_ui()

        def _build_ui(self):
            lay = QVBoxLayout(self)
            form = QFormLayout()
            self.cb_song = QComboBox()
            try:
                self.cb_song.addItems(song_ids(self.archive))
            except Exception as exc:
                QMessageBox.warning(self, "Song Builder", f"Could not list songs: {exc}")
            self.ed_title = QLineEdit()
            self.ed_lyr = QLineEdit()
            self.ed_comp = QLineEdit()
            self.ed_copy = QLineEdit("© 20XX")
            self.ck_new = QCheckBox("Add as NEW song (clone selected as template)")
            self.ck_new.setToolTip(
                "Off: replace the selected song slot.\n"
                "On: create a brand-new song slot (new groups in DATA.000); the\n"
                "selected song is used as the template for texture/chart layout.")
            self.ed_newid = QLineEdit(); self.ed_newid.setPlaceholderText("new song id, e.g. mysong")
            self.ed_newid.setEnabled(False)
            self.ck_new.toggled.connect(self.ed_newid.setEnabled)
            form.addRow(self.ck_new)
            form.addRow("new song id:", self.ed_newid)
            form.addRow("target / template (id):", self.cb_song)
            form.addRow("title 曲名:", self.ed_title)
            form.addRow("作詞 lyricist:", self.ed_lyr)
            form.addRow("作曲 composer:", self.ed_comp)
            form.addRow("© copyright:", self.ed_copy)

            self.cb_mode = QComboBox()
            self.cb_mode.addItems(["TJA + audio file", "Gen3 (Nijiiro) song folder"])
            self.cb_mode.setToolTip(
                "TJA: convert a .tja chart and a wav/ogg, syncing the music to "
                "the TJA's BPM/OFFSET.\n"
                "Gen3: point at a Nijiiro song folder (…/fumen/<id>). Charts, "
                "title, star ratings and the nus3bank music are all read from "
                "the game's own data; nothing needs syncing.")
            self.cb_mode.currentIndexChanged.connect(self._mode_changed)
            form.addRow("source mode:", self.cb_mode)

            self.ed_tja = QLineEdit(appconfig.last_existing("tja")); b_tja = QPushButton("TJA…")
            b_tja.clicked.connect(lambda: self._pick(self.ed_tja, "TJA charts (*.tja)"))
            self.ed_audio = QLineEdit(appconfig.last_existing("wav")); b_aud = QPushButton("Audio…")
            b_aud.clicked.connect(lambda: self._pick(self.ed_audio, "Audio (*.wav *.ogg)"))
            self.row_tja = self._row(self.ed_tja, b_tja)
            self.row_audio = self._row(self.ed_audio, b_aud)
            form.addRow("chart .tja:", self.row_tja)
            form.addRow("audio wav/ogg:", self.row_audio)
            self.lbl_tja = form.labelForField(self.row_tja)
            self.lbl_audio = form.labelForField(self.row_audio)

            self.ed_gen3 = QLineEdit(); b_g3 = QPushButton("Folder…")
            self.ed_gen3.setPlaceholderText(
                r"…\Data\x64\fumen\<song id>   (e.g. …\fumen\yyhero)")
            b_g3.clicked.connect(self._pick_gen3)
            self.row_gen3 = self._row(self.ed_gen3, b_g3)
            form.addRow("Gen3 song folder:", self.row_gen3)
            self.lbl_gen3 = form.labelForField(self.row_gen3)

            self.ed_gap = QLineEdit("0")
            self.ed_gap.setToolTip(
                "Leave at 0. Sync is computed from the TJA's BPM/OFFSET and baked "
                "in automatically — feed the ORIGINAL .tja/.ogg, not files already "
                "run through test.py. Use this only to taste-tune a song that "
                "feels a hair off: + moves the music later, − earlier.")
            self.row_gap_label = "sync nudge (ms, optional):"
            form.addRow(self.row_gap_label, self.ed_gap)
            self.lbl_gap = form.labelForField(self.ed_gap)
            lay.addLayout(form)
            self._mode_changed()

            opts = QHBoxLayout()
            self.ck_tex = QCheckBox("textures"); self.ck_tex.setChecked(True)
            self.ck_chart = QCheckBox("charts"); self.ck_chart.setChecked(True)
            self.ck_audio = QCheckBox("audio"); self.ck_audio.setChecked(True)
            self.ck_stars = QCheckBox("difficulty stars"); self.ck_stars.setChecked(True)
            for w in (self.ck_tex, self.ck_chart, self.ck_audio, self.ck_stars):
                opts.addWidget(w)
            opts.addStretch(1)
            lay.addLayout(opts)

            self.log = QPlainTextEdit(readOnly=True)
            lay.addWidget(QLabel("log:"))
            lay.addWidget(self.log, 1)

            btns = QHBoxLayout(); btns.addStretch(1)
            self.b_build = QPushButton("Build → stage"); self.b_build.clicked.connect(self._build)
            b_close = QPushButton("Close"); b_close.clicked.connect(self.accept)
            btns.addWidget(self.b_build); btns.addWidget(b_close)
            lay.addLayout(btns)

        def _row(self, edit, btn):
            w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(edit, 1); h.addWidget(btn)
            return w

        def _is_gen3(self):
            return self.cb_mode.currentIndex() == 1

        def _mode_changed(self, *_):
            """Show only the rows the selected source actually uses."""
            g3 = self._is_gen3()
            for w in (self.row_tja, self.lbl_tja, self.row_audio, self.lbl_audio,
                      self.ed_gap, self.lbl_gap):
                if w is not None:
                    w.setVisible(not g3)
            for w in (self.row_gen3, self.lbl_gen3):
                if w is not None:
                    w.setVisible(g3)
            # Gen3 songs carry their own id/title/stars, so those are filled in
            # from the game data rather than typed.
            for w in (self.ed_title, self.ed_newid):
                w.setReadOnly(g3)
            # Gen3 can only add a new slot, so the checkbox is forced on -- but
            # remember what the user had chosen and give it back on the way out,
            # rather than silently leaving their TJA build in "new song" mode.
            if g3:
                if not getattr(self, "_new_forced", False):
                    self._new_saved = self.ck_new.isChecked()
                    self._new_forced = True
                self.ck_new.setChecked(True)
                self.ck_new.setEnabled(False)
            else:
                if getattr(self, "_new_forced", False):
                    self.ck_new.setChecked(self._new_saved)
                    self._new_forced = False
                self.ck_new.setEnabled(True)

        def _pick_gen3(self):
            d = QFileDialog.getExistingDirectory(
                self, "Choose a Gen3 song folder (…/fumen/<id>)",
                self.ed_gen3.text())
            if not d:
                return
            self.ed_gen3.setText(d)
            self._load_gen3_preview(d)

        def _load_gen3_preview(self, folder):
            """Fill id/title/stars from the game's own data, and say what came
            back so a wrong folder is obvious before a 1-minute rebuild."""
            try:
                import gen3_song
                song = gen3_song.load_song(folder)
            except Exception as exc:
                self.log.appendPlainText("Gen3: %s" % exc)
                QMessageBox.warning(self, "Gen3 song", str(exc))
                return
            self._gen3 = song
            self._gen3_folder = folder
            self.ed_newid.setText(song["sid"])
            self.ed_title.setText(song["title"] or song["sid"])
            # Songs missing from musicinfo have no title and no stars, so those
            # fields must stay typeable instead of locked to placeholder text.
            editable = not song.get("known", True)
            self.ed_title.setReadOnly(not editable)
            self.ed_newid.setReadOnly(not editable)
            self.log.clear()
            self.log.appendPlainText(
                "Gen3 song %s — %r\n  stars: %s\n  charts: %s\n  audio: %s"
                % (song["sid"], song["title"],
                   song["stars"] or "(unknown — Build will ask you for them)",
                   ", ".join(sorted(song["sht"])) or "(none)",
                   song["audio_path"] or "(none)"))
            for w in song["warnings"]:
                self.log.appendPlainText("WARNING: " + w)

        def _pick(self, edit, filt):
            # remembers the last-used path per file type in config.ini
            p = appconfig.pick_open(self, appconfig.key_for_filter(filt),
                                    "Choose file", filt)
            if p:
                edit.setText(p)

        # The old "Auto gap" button is gone on purpose: build_song now derives the
        # sync from the TJA itself, so pre-filling the same figure here applied it
        # twice and put the whole song a measure out.

        def _build(self):
            sid = self.cb_song.currentText()
            if not sid:
                return
            if self._is_gen3():
                self._build_gen3(sid)
                return
            self.log.clear()
            tja_text = None
            tja_path = self.ed_tja.text().strip()
            if tja_path:
                if not Path(tja_path).exists():
                    QMessageBox.warning(self, "Song Builder",
                                        f"TJA file not found:\n{tja_path}")
                    return
                try:
                    tja_text, tja_warn = _read_tja_text(tja_path)
                    if tja_warn:
                        self.log.appendPlainText("WARNING: " + tja_warn)
                except Exception as exc:
                    QMessageBox.critical(self, "Song Builder",
                                         f"Could not read TJA:\n{exc}")
                    return
            audio_path = self.ed_audio.text().strip()
            if audio_path and not Path(audio_path).exists():
                QMessageBox.warning(self, "Song Builder",
                                    f"Audio file not found:\n{audio_path}")
                return
            audio_path = audio_path or None
            try:
                gap_ms = float(self.ed_gap.text().strip() or "0")
            except ValueError:
                gap_ms = 0.0
            new_mode = self.ck_new.isChecked()
            if new_mode:
                new_id = self.ed_newid.text().strip()
                if not new_id:
                    QMessageBox.warning(self, "Add new song", "Enter a new song id.")
                    return
                msg = "Building new song — rebuilding DATA.000 (~1 min). Please wait…"
                # DB step builds a SongManager (QWidget) — must run on the GUI thread.
                try:
                    db_pre, stars = prepare_new_song_db(self.archive, new_id, sid, tja_text)
                except Exception as exc:
                    QMessageBox.critical(self, "Add new song", str(exc))
                    return

                def task(log):
                    return add_new_song(
                        self.archive, new_id, title=self.ed_title.text() or new_id,
                        template_sid=sid, lyricist=self.ed_lyr.text(),
                        composer=self.ed_comp.text(), copyright_=self.ed_copy.text(),
                        tja_text=tja_text, audio_path=audio_path,
                        lead_silence_ms=gap_ms,
                        precomputed_db=db_pre, stars=stars, log=log)
            else:
                msg = "Building — generating assets…"

                def task(log):
                    return build_song(
                        self.archive, sid, title=self.ed_title.text() or sid,
                        lyricist=self.ed_lyr.text(), composer=self.ed_comp.text(),
                        copyright_=self.ed_copy.text(), tja_text=tja_text,
                        audio_path=audio_path, lead_silence_ms=gap_ms,
                        do_textures=self.ck_tex.isChecked(), do_charts=self.ck_chart.isChecked(),
                        do_audio=self.ck_audio.isChecked(), do_stars=self.ck_stars.isChecked(),
                        log=log)

            # run off the UI thread so the window stays responsive
            self.b_build.setEnabled(False)
            self._prog = QProgressDialog(msg, None, 0, 0, self)
            self._prog.setWindowModality(Qt.WindowModal)
            self._prog.setCancelButton(None)
            self._prog.setMinimumDuration(0)
            self._prog.show()
            self._worker = _BuildWorker(task)
            self._worker.log_sig.connect(self.log.appendPlainText)
            self._worker.done_sig.connect(lambda res: self._on_built(res, new_mode))
            self._worker.start()

        def _build_gen3(self, template_sid):
            """Add a whole Gen3 song: charts, title, stars and music come from
            the game's own data, so nothing here is typed or synced."""
            folder = self.ed_gen3.text().strip()
            if not folder:
                QMessageBox.warning(self, "Gen3 song", "Choose a Gen3 song folder.")
                return
            # Reload only when the FOLDER changed — keying this off the id field
            # would wipe an id the user typed for a song musicinfo doesn't know.
            song = getattr(self, "_gen3", None)
            if song is None or getattr(self, "_gen3_folder", None) != folder:
                self._load_gen3_preview(folder)
                song = getattr(self, "_gen3", None)
                if song is None:
                    return

            # For a song missing from musicinfo the id/title fields are typeable,
            # so take whatever is in them.
            new_id = self.ed_newid.text().strip() or song["sid"]
            if new_id in song_ids(self.archive):
                QMessageBox.warning(
                    self, "Gen3 song",
                    "This archive already has a song called '%s'. Remove it "
                    "first, or use Song Manager to replace it." % new_id)
                return

            import gen3_song
            # None when musicinfo has no entry — prepare_new_song_db then falls
            # back to the template's ratings rather than inventing any.
            stars = gen3_song.stars_list(song)
            if stars is None:
                # No ratings anywhere, and none are derivable from the chart —
                # ask rather than ship the template's under this song's name.
                k_src = song_ids(self.archive).index(template_sid)
                dlg = StarsDialog(song["sid"], _template_stars(self.archive, k_src),
                                  self)
                if dlg.exec() != QDialog.Accepted:
                    self.log.appendPlainText("cancelled: no star ratings set.")
                    return
                stars = dlg.stars()
                self.log.appendPlainText("stars: set by hand -> %s" % stars)
            # DB step builds a SongManager (QWidget) — GUI thread only.
            try:
                db_pre, stars = prepare_new_song_db(
                    self.archive, new_id, template_sid, None, stars=stars)
            except Exception as exc:
                QMessageBox.critical(self, "Gen3 song", str(exc))
                return

            title = self.ed_title.text().strip() or song["title"] or new_id

            def task(log):
                kw = gen3_song.to_builder_kwargs(song, log=log)
                return add_new_song(
                    self.archive, new_id, title=title,
                    template_sid=template_sid,
                    lyricist=self.ed_lyr.text(), composer=self.ed_comp.text(),
                    copyright_=self.ed_copy.text(),
                    charts=kw["charts"], audio_vag=kw["audio_vag"],
                    precomputed_db=db_pre, stars=stars, log=log)

            self.b_build.setEnabled(False)
            self._prog = QProgressDialog(
                "Building %s from Gen3 data — decoding audio and rebuilding "
                "DATA.000 (~1 min). Please wait…" % new_id, None, 0, 0, self)
            self._prog.setWindowModality(Qt.WindowModal)
            self._prog.setCancelButton(None)
            self._prog.setMinimumDuration(0)
            self._prog.show()
            self._worker = _BuildWorker(task)
            self._worker.log_sig.connect(self.log.appendPlainText)
            self._worker.done_sig.connect(lambda res: self._on_built(res, True))
            self._worker.start()

        def _on_built(self, res, new_mode):
            self._prog.close()
            self.b_build.setEnabled(True)
            # Release the finished worker so a subsequent build gets a fresh
            # QThread and the old one isn't GC'd while still referenced.
            worker = self._worker
            if worker is not None:
                worker.deleteLater()
                self._worker = None
            if isinstance(res, tuple) and len(res) == 3 and res[0] is _BUILD_ERROR:
                self.log.appendPlainText("FAILED:\n" + res[2])
                QMessageBox.critical(self, "Build failed", str(res[1]))
                return
            if new_mode:
                lb, db, summ = res
                self.new_archive = (lb, db)
                self.log.appendPlainText(
                    f"\nNEW SONG built: {summ['groups']} groups, {summ['extra_files']} "
                    f"select files, audio={'yes' if summ['audio'] else 'no'}, db={summ['db']}")
                if summ.get("warnings"):
                    self.log.appendPlainText("WARNINGS:\n  " + "\n  ".join(summ["warnings"]))
                if summ["errors"]:
                    self.log.appendPlainText("ERRORS:\n  " + "\n  ".join(summ["errors"]))
                self.log.appendPlainText("\nClose this dialog to write DATA.000 + LIST.BIN.")
            else:
                summ = res
                self.changed = (self.changed or bool(summ["textures"]) or bool(summ["charts"])
                                or bool(summ["stars"]) or bool(summ["audio"]))
                self.log.appendPlainText(
                    f"\nDONE: {summ['textures']} textures, {summ['charts']} charts staged; "
                    f"audio={'staged' if summ['audio'] else 'no'}; stars={summ['stars']}")
                if summ.get("warnings"):
                    self.log.appendPlainText("WARNINGS:\n  " + "\n  ".join(summ["warnings"]))
                if summ["errors"]:
                    self.log.appendPlainText("ERRORS:\n  " + "\n  ".join(summ["errors"]))
                self.log.appendPlainText("\nStaged edits — use the main window's Save to write DATA.000.")

except ImportError:
    SongBuilderDialog = None  # type: ignore

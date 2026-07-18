#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
tja2sht.py  --  TJA  ->  .sht (Gen2 / SYSTEM256 PS2-arcade fumen) converter.

This module does three things:

  1. parse_sht(data)  -> model (dict)         exact round-trippable parser
     serialize_sht(model) -> bytes            inverse; serialize(parse(d)) == d
  2. parse_tja(text)  -> structured courses   focused TJA parser
  3. convert_tja(...) / tja_to_all_charts(...)  TJA -> sht bytes

----------------------------------------------------------------------------
THE sht (Gen2) FORMAT  (little-endian) -- confirmed byte-exact vs 720 real
charts in the SYSTEM256 arcade archive (DATA.000 / list.bin).

Header (16 bytes):
    0x00 u32 trackCount     number of "track" (measure) records
    0x04 u32 noteOffset     file offset where the 16-byte note-event table starts
    0x08 u32 noteCount
    0x0C i32 padding        (the reference's Header._; always 0 in the corpus)

Track record.  Two variants exist in the reference implementation; the size is
detected from (noteOffset-16)/trackCount:
    NEW variant  = 136 bytes  (no _unk field)   <-- ALL 720 corpus charts
    OLD variant  = 140 bytes  (extra i32 _unk after gogoFlag)
  Fields:
    float  time           ms timestamp of the measure start
    float  bpm
    int    trackLine       1 = draw the bar-line, 0 = hidden
    int    gogoFlag        1 = go-go time
    [int   _unk]           OLD variant only
    int    bunkis[6]       branch markers, -1 = none
    float  scrollSpeeds[6] HS / scroll per sub-track
    {                      subtracks[6]
       int noteIndexSt     start index into the note table
       int noteCount       number of notes in this sub-track of this measure
       int pointGain       score value
    } * 6

Note record (16 bytes), noteCount of them starting at noteOffset:
    int type               note-type enum (see NOTE_* below)
    int measure / position offset of the note WITHIN its measure, in 1/48 units
                           (observed range 0..47)
    int balloonHitCount    hit count for balloon/kusudama notes, else -1
    int longNoteLength     duration (in `time` units) for rolls/balloons, else -1

NOTE-TYPE ENUM (derived by correlating the corpus; histogram across 720 charts):
    1  = don            (small red)        172543
    2  = ka             (small blue)       120467
    3  = bigDon         (large red)         24885
    4  = bigKa          (large blue)        87097
    5  = drumroll body marker (small renda) 71401   (bal/long = -1)
    6  = big drumroll        (long = duration)5121   (bal = -1)
    7  = balloon start                      18890   (bal/long = -1)
    8  = balloon end / renda end             5826   (bal/long = -1)
    9  = kusudama            (long = duration) 570   (bal = -1)
    10 = balloon-with-count  (bal = hits, long = duration) 1508
    11 = note-spacing / density marker        2740   (bal/long = -1)
    12 = balloon-with-count (variant)          314   (bal & long set)
    13 = spacing marker (variant)              564   (bal/long = -1)
  Note events in this format appear DUPLICATED (each logical note is written
  twice consecutively -- once per the two active sub-track halves of a measure).
  The parser/serializer preserve this verbatim; the TJA converter reproduces it.

ROUND-TRIP RESULT:  serialize_sht(parse_sht(d)) == d  for 720 / 720 charts.
"""

import struct
import math

# ---------------------------------------------------------------------------
# Note type enum
# ---------------------------------------------------------------------------
# Correct in-game enum (verified in-game + matches TJA2WII's Wii5 mapping). An
# earlier corpus-histogram guess (ka=2, bigka=4, roll_end=8) was WRONG: in-game
# our old ka(2) drew RED, bigka(4) drew small-ka, and the fake end-note(8) drew a
# stray BIG-KA. The real values:
NOTE_DON          = 1
NOTE_KA           = 4    # small ka (blue)
NOTE_BIGDON       = 7    # big don (red)   = bigka - 1
NOTE_BIGKA        = 8    # big ka (blue)
NOTE_DRUMROLL     = 6    # small renda (long = duration)
NOTE_BIGDRUMROLL  = 9    # big renda (long = duration)
NOTE_BALLOON      = 10   # balloon (bal = hits, long = duration)
NOTE_KUSUDAMA     = 12   # kusudama (long = duration)
NOTE_DON2HAND     = 11   # 2-hand don
NOTE_KA2HAND      = 13   # 2-hand ka
# There is NO separate roll/balloon END note: the TJA '8' terminator only sets the
# START note's longNoteLength (duration). A leftover end note renders as a big-ka.

# Granularity of in-measure note position used by this format (1/48 of a measure)
POS_DIV = 48

# NOTE: a measure-splitting feature used to live here (emitting k track records
# per measure to reach finer grids). It was REVERTED: it made charts play wrong
# in-game (1/24 measures that were previously fine came out broken). The premise
# -- that manufacturing extra records is transparent to the game -- was never
# validated; the corpus only proves 48 units span a record, not that the engine
# accepts records we invent. Do not reintroduce without in-game evidence first.

# Per-difficulty note-table capacity. The game allocates a FIXED note buffer per
# difficulty; loading a chart whose note count (both sub-track copies, i.e. the
# header noteCount) exceeds it overflows the buffer and FREEZES at chart load.
# These are the max header noteCounts observed across all 712 corpus charts of
# each difficulty (proven-safe upper bounds). Putting a dense Oni chart into the
# Easy/Normal slot (the fallback when a TJA lacks those courses) is the classic
# way to blow past the Easy(610)/Normal(834) limits.
DIFFICULTY_NOTE_LIMIT = {"easy": 610, "normal": 834, "hard": 3430, "oni": 4668}

TRACK_NEW_SIZE = 136
TRACK_OLD_SIZE = 140
HEADER_SIZE    = 16
NOTE_SIZE      = 16


# ===========================================================================
# STEP 1 -- exact sht parser / serializer
# ===========================================================================
def _detect_is_old(track_count, note_offset):
    """Return True if the OLD (140-byte, with _unk) track variant is in use.

    Detection is purely from (noteOffset - 16) / trackCount. If that region
    does not divide evenly into either the NEW (136) or OLD (140) record size
    we cannot safely read the track table, so raise instead of silently
    reading garbage offsets.
    """
    if track_count == 0:
        return False
    region = note_offset - HEADER_SIZE
    if region < 0:
        raise ValueError(
            "noteOffset (%d) is before the track table (header=%d)"
            % (note_offset, HEADER_SIZE))
    if region % track_count == 0:
        rec = region // track_count
        if rec == TRACK_OLD_SIZE:
            return True
        if rec == TRACK_NEW_SIZE:
            return False
    raise ValueError(
        "unrecognized track record size: (noteOffset-16)=%d does not divide "
        "evenly into %d tracks as %d- or %d-byte records"
        % (region, track_count, TRACK_NEW_SIZE, TRACK_OLD_SIZE))


def parse_sht(data):
    """Parse Gen2 .sht bytes into a plain-dict model.

    The model is fully sufficient to reproduce the original bytes via
    serialize_sht(). Layout-significant header fields (noteOffset, padding)
    are preserved verbatim so unusual files still round-trip.
    """
    if len(data) < HEADER_SIZE:
        raise ValueError("file too small to be a Gen2 sht (%d bytes)" % len(data))

    track_count, note_offset, note_count, padding = struct.unpack_from("<IIii", data, 0)
    is_old = _detect_is_old(track_count, note_offset)
    trec = TRACK_OLD_SIZE if is_old else TRACK_NEW_SIZE

    # Bounds validation so truncated / corrupt files raise a clear ValueError
    # instead of an opaque struct.error or silent garbage reads.
    track_end = HEADER_SIZE + track_count * trec
    if track_end > note_offset:
        raise ValueError(
            "track table (%d bytes) overruns noteOffset (%d)"
            % (track_end, note_offset))
    if note_count < 0:
        raise ValueError("negative noteCount (%d)" % note_count)
    note_end = note_offset + note_count * NOTE_SIZE
    if note_end > len(data):
        raise ValueError(
            "note table runs past end of file: needs %d bytes, have %d"
            % (note_end, len(data)))

    tracks = []
    off = HEADER_SIZE
    for _ in range(track_count):
        time, bpm, track_line, gogo = struct.unpack_from("<ffii", data, off)
        p = off + 16
        if is_old:
            unk = struct.unpack_from("<i", data, p)[0]
            p += 4
        else:
            unk = 0
        bunkis = list(struct.unpack_from("<6i", data, p)); p += 24
        scrolls = list(struct.unpack_from("<6f", data, p)); p += 24
        subs = []
        for _s in range(6):
            st, cnt, pts = struct.unpack_from("<iii", data, p); p += 12
            subs.append({"noteIndexSt": st, "noteCount": cnt, "pointGain": pts})
        tracks.append({
            "time": time, "bpm": bpm, "trackLine": track_line, "gogoFlag": gogo,
            "_unk": unk, "bunkis": bunkis, "scrollSpeeds": scrolls, "subtracks": subs,
        })
        off += trec

    notes = []
    no = note_offset
    for _ in range(note_count):
        t, m, bal, lng = struct.unpack_from("<iiii", data, no)
        notes.append({"type": t, "measure": m,
                      "balloonHitCount": bal, "longNoteLength": lng})
        no += NOTE_SIZE

    return {
        "noteOffset": note_offset,
        "padding": padding,
        "isOld": is_old,
        "tracks": tracks,
        "notes": notes,
        # any bytes between the end of the track table and noteOffset, and any
        # trailing bytes after the note table, are preserved for exactness
        "_gap": data[HEADER_SIZE + track_count * trec: note_offset],
        "_tail": data[note_offset + note_count * NOTE_SIZE:],
    }


def serialize_sht(model):
    """Inverse of parse_sht: rebuild the exact byte stream."""
    is_old = model.get("isOld", False)
    trec = TRACK_OLD_SIZE if is_old else TRACK_NEW_SIZE
    tracks = model["tracks"]
    notes = model["notes"]
    track_count = len(tracks)
    note_count = len(notes)

    gap = model.get("_gap", b"")
    tail = model.get("_tail", b"")

    # noteOffset: prefer the stored value (exact round-trip); else compute.
    note_offset = model.get("noteOffset")
    if note_offset is None:
        note_offset = HEADER_SIZE + track_count * trec + len(gap)

    out = bytearray()
    out += struct.pack("<IIii", track_count, note_offset, note_count,
                       model.get("padding", 0))

    for t in tracks:
        out += struct.pack("<ffii", t["time"], t["bpm"],
                           t["trackLine"], t["gogoFlag"])
        if is_old:
            out += struct.pack("<i", t.get("_unk", 0))
        bunkis = list(t["bunkis"]) + [-1] * (6 - len(t["bunkis"]))
        out += struct.pack("<6i", *bunkis[:6])
        scrolls = list(t["scrollSpeeds"]) + [1.0] * (6 - len(t["scrollSpeeds"]))
        out += struct.pack("<6f", *scrolls[:6])
        subs = t["subtracks"]
        for s in range(6):
            if s < len(subs):
                sd = subs[s]
                out += struct.pack("<iii", sd["noteIndexSt"], sd["noteCount"],
                                   sd["pointGain"])
            else:
                out += struct.pack("<iii", 0, 0, 0)

    # gap (between track table and note table)
    out += gap
    # pad with zeros if the stored noteOffset is beyond current position
    if len(out) < note_offset:
        out += b"\x00" * (note_offset - len(out))

    for n in notes:
        out += struct.pack("<iiii", n["type"], n["measure"],
                           n["balloonHitCount"], n["longNoteLength"])

    out += tail
    return bytes(out)


# ===========================================================================
# STEP 2 -- TJA parser
# ===========================================================================
_COURSE_ALIASES = {
    "easy": 0, "0": 0,
    "normal": 1, "1": 1,
    "hard": 2, "2": 2,
    "oni": 3, "3": 3, "master": 3,
    "edit": 4, "ura": 4, "4": 4,
}
_DIFF_NAMES = {0: "easy", 1: "normal", 2: "hard", 3: "oni", 4: "edit"}

# Branch path tags for parsed measures. -1/COMMON measures (outside any
# branch, or between #BRANCHSTART and the first path marker) always play;
# N/E/M tag the three parallel paths that share one time span. convert_tja
# keeps COMMON plus exactly one selected path so time stays correct.
BRANCH_COMMON = -1
BRANCH_NORMAL = 0
BRANCH_EXPERT = 1
BRANCH_MASTER = 2
# Which branch path the converter renders. The master (clear) path is the
# canonical "full" chart in Taiko.
SELECTED_BRANCH = BRANCH_MASTER


def _to_float(s, default=0.0):
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def parse_tja(text):
    """Parse a TJA file into a dict:
        {"meta": {...global...},
         "courses": { course_idx: {"meta": {...}, "events": [...]} }}

    Each course's "events" is the raw, ordered list of measures, where a
    measure is a dict:
        {"notes": "1020...", "bpm": float, "scroll": float, "gogo": bool,
         "branch": int, "measure_num": int, "measure_den": int}
    and inline balloon counts live in course meta "balloon" list.
    """
    meta = {}
    courses = {}
    cur_course = None

    # per-course running state
    state = None

    def new_course(idx):
        return {
            "meta": dict(meta),  # inherit globals
            "measures": [],
            "balloon": [],
        }

    def new_state():
        return {
            "bpm": _to_float(meta.get("BPM", "120")),
            "scroll": 1.0,
            "gogo": False,
            "measure_num": 4,
            "measure_den": 4,
            "buffer": "",      # accumulated note chars for current measure
            "branch": BRANCH_COMMON,
            "delay_ms": 0.0,   # accumulated #DELAY for the next measure(s)
        }

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    in_chart = False
    for raw in lines:
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue

        if line.startswith("#"):
            # command
            parts = line[1:].split(None, 1)
            cmd = parts[0].upper()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd == "START":
                in_chart = True
                if cur_course is None:
                    cur_course = 0
                    courses[cur_course] = new_course(cur_course)
                state = new_state()
                continue
            if cmd == "END":
                # flush any partial buffer as a final measure
                if state is not None and state["buffer"]:
                    _flush_measure(courses[cur_course], state)
                in_chart = False
                state = None
                continue
            if state is None:
                # commands outside START (rare); ignore
                continue

            if cmd == "BPMCHANGE":
                state["bpm"] = _to_float(arg, state["bpm"])
            elif cmd == "SCROLL":
                state["scroll"] = _to_float(arg, 1.0)
            elif cmd == "GOGOSTART":
                state["gogo"] = True
            elif cmd == "GOGOEND":
                state["gogo"] = False
            elif cmd == "DELAY":
                # DELAY shifts subsequent notes later by N seconds.
                state["delay_ms"] = state.get("delay_ms", 0.0) + _to_float(arg) * 1000.0
            elif cmd == "MEASURE":
                if "/" in arg:
                    a, b = arg.split("/", 1)
                    state["measure_num"] = int(_to_float(a, 4))
                    state["measure_den"] = int(_to_float(b, 4))
            elif cmd in ("BRANCHSTART", "N", "E", "M", "BRANCHEND"):
                # Branch notation emits three parallel measure streams (N/E/M)
                # covering the SAME time span. We must not lay them end to end
                # or the chart becomes 2-3x too long. Flush any pending buffer
                # at every branch boundary so a partial measure can't leak
                # across paths, then tag which path subsequent measures belong
                # to. convert_tja keeps only the selected path.
                if state["buffer"]:
                    _flush_measure(courses[cur_course], state)
                if cmd == "N":
                    state["branch"] = BRANCH_NORMAL
                elif cmd == "E":
                    state["branch"] = BRANCH_EXPERT
                elif cmd == "M":
                    state["branch"] = BRANCH_MASTER
                elif cmd == "BRANCHSTART":
                    # condition args (p,x,y) are intentionally not modelled; the
                    # measures up to the first #N/#E/#M default to common path.
                    state["branch"] = BRANCH_COMMON
                elif cmd == "BRANCHEND":
                    state["branch"] = BRANCH_COMMON
            elif cmd == "BARLINEOFF":
                state["barline"] = 0
            elif cmd == "BARLINEON":
                state["barline"] = 1
            # other commands (SECTION, LEVELHOLD, ...) intentionally ignored
            continue

        if ":" in line and not in_chart:
            # header field   KEY:value
            key, val = line.split(":", 1)
            key = key.strip().upper()
            val = val.strip()
            if key == "COURSE":
                idx = _COURSE_ALIASES.get(val.strip().lower(), None)
                if idx is None:
                    idx = _COURSE_ALIASES.get(val.strip(), 3)
                cur_course = idx
                courses[cur_course] = new_course(cur_course)
                courses[cur_course]["meta"]["COURSE"] = val
            elif key == "BALLOON":
                vals = [int(_to_float(x)) for x in val.replace(" ", "").split(",") if x != ""]
                if cur_course is not None and cur_course in courses:
                    courses[cur_course]["balloon"] = vals
                else:
                    meta["BALLOON"] = vals
            else:
                if cur_course is not None and cur_course in courses:
                    courses[cur_course]["meta"][key] = val
                else:
                    meta[key] = val
            continue

        # otherwise: note data within a chart
        if in_chart and state is not None:
            # Guard against stray header-style "KEY:value" lines (or any other
            # non-chart text) appearing between #START and #END: only digits,
            # commas and whitespace are legal chart data. Anything else would
            # silently corrupt the measure buffer, so skip it.
            if any(c not in "0123456789, \t" for c in line):
                continue
            # a line may contain a trailing ',' ending a measure; may have
            # multiple commas; accumulate
            seg = line
            while "," in seg:
                pre, seg = seg.split(",", 1)
                state["buffer"] += pre
                _flush_measure(courses[cur_course], state)
            state["buffer"] += seg

    return {"meta": meta, "courses": courses}


def _flush_measure(course, state):
    notes = state["buffer"]
    state["buffer"] = ""
    # A pending #DELAY is applied once, to this measure, then cleared.
    delay_ms = state.get("delay_ms", 0.0)
    state["delay_ms"] = 0.0
    course["measures"].append({
        "notes": notes,
        "bpm": state["bpm"],
        "scroll": state["scroll"],
        "gogo": state["gogo"],
        "branch": state["branch"],
        "measure_num": state["measure_num"],
        "measure_den": state["measure_den"],
        "barline": state.get("barline", 1),
        "delay_ms": delay_ms,
    })


# TJA note char -> sht note type
_TJA_NOTE_MAP = {
    "1": NOTE_DON,
    "2": NOTE_KA,
    "3": NOTE_BIGDON,
    "4": NOTE_BIGKA,
    # 5 = drumroll start, 6 = big drumroll start, 7 = balloon, 8 = roll/balloon end,
    # 9 = kusudama  -> handled specially below
}


# ===========================================================================
# STEP 3 -- TJA -> sht conversion
# ===========================================================================
def convert_tja(tja_text, course, player="1p"):
    """Convert one TJA course to Gen2 .sht bytes.

    course in {'easy','normal','hard','oni'} (also accepts 'edit'/index).
    player is accepted for API symmetry; 2p charts mirror 1p here.

    Produces a STRUCTURALLY VALID sht: correct header, a track record per
    measure with correct time/bpm/scroll/gogo and sub-track note indices, and
    a note table with notes placed at the correct in-measure positions.

    Branches (#BRANCHSTART/#N/#E/#M/#BRANCHEND) are collapsed to the common
    path plus one selected path (SELECTED_BRANCH) so the timeline length stays
    correct. Roll/balloon start markers (5/6/7/9) are paired with their 8 end
    marker and back-patched with the elapsed ms in longNoteLength. Each
    measure's note block is written twice (sub-track 0 + sub-track 3) to match
    the corpus duplication invariant.
    """
    parsed = parse_tja(tja_text)
    courses = parsed["courses"]

    cidx = _COURSE_ALIASES.get(str(course).lower(), None)
    if cidx is None:
        cidx = _COURSE_ALIASES.get(str(course), 3)
    if cidx not in courses:
        # fall back to any available course
        if not courses:
            raise ValueError("TJA contains no charts")
        cidx = sorted(courses.keys())[0]

    cdata = courses[cidx]
    gmeta = parsed["meta"]
    meta = cdata["meta"]

    global_bpm = _to_float(meta.get("BPM", gmeta.get("BPM", "120")), 120.0)
    if global_bpm <= 0:
        global_bpm = 120.0
    offset = _to_float(meta.get("OFFSET", gmeta.get("OFFSET", "0")), 0.0)
    balloon_counts = list(cdata.get("balloon", gmeta.get("BALLOON", [])))
    bal_iter = iter(balloon_counts)

    # Branch handling: keep only COMMON measures plus the one selected path.
    # Laying N/E/M end to end (as the naive walk did) makes the chart 2-3x too
    # long and mistimes every note after the first branch.
    src_measures = [m for m in cdata["measures"]
                    if m.get("branch", BRANCH_COMMON) in (BRANCH_COMMON, SELECTED_BRANCH)]

    tracks = []
    notes = []

    # running time in ms; TJA OFFSET is seconds before the first measure
    cur_time_ms = -offset * 1000.0

    # First pass: compute each measure's start time and per-measure note records
    # (without long durations yet), so rolls/balloons can be paired start->8
    # across measures and back-patched with their true ms duration.
    measure_recs = []        # list of (track_meta, [note dicts])
    open_roll = None         # (note_dict, start_time_ms) for an unpaired 5/6/7/9
    note_seq = 0             # running index of logical notes for balloon assoc.

    for m in src_measures:
        bpm = m["bpm"] if (m["bpm"] and m["bpm"] > 0) else global_bpm
        num = max(0, m["measure_num"])
        den = m["measure_den"] or 4
        beats = 4.0 * (num / float(den))
        measure_ms = (60000.0 / bpm) * beats
        if measure_ms < 0:
            measure_ms = 0.0

        # #DELAY shifts this measure (and everything after) later in time.
        cur_time_ms += m.get("delay_ms", 0.0)
        measure_start = cur_time_ms

        note_chars = [c for c in m["notes"] if not c.isspace()]
        n = len(note_chars)

        recs = []
        used_pos = set()
        for i, ch in enumerate(note_chars):
            if ch == "0":
                continue
            # In-measure position in 1/48 units. Clamp to the valid range and
            # nudge off collisions so two distinct notes (e.g. in a 5/7-tuplet
            # that 1/48 can't represent exactly) never silently stack.
            pos = int(round(i * POS_DIV / n)) if n > 0 else 0
            if pos >= POS_DIV:
                pos = POS_DIV - 1
            if pos < 0:
                pos = 0
            while pos in used_pos and pos < POS_DIV - 1:
                pos += 1
            used_pos.add(pos)

            note_time = measure_start + (pos / float(POS_DIV)) * measure_ms

            if ch in _TJA_NOTE_MAP:
                rec = {"type": _TJA_NOTE_MAP[ch], "measure": pos,
                       "balloonHitCount": -1, "longNoteLength": -1}
                recs.append(rec)
            elif ch in ("5", "6"):
                t = NOTE_DRUMROLL if ch == "5" else NOTE_BIGDRUMROLL
                rec = {"type": t, "measure": pos,
                       "balloonHitCount": -1, "longNoteLength": -1}
                recs.append(rec)
                open_roll = (rec, note_time)
            elif ch in ("7", "9"):
                # 7 = balloon, 9 = kusudama. Both are type 10 / 12 and carry a hit
                # count (from the TJA BALLOON: header) + a duration (set at the '8').
                cnt = next(bal_iter, None)
                t = NOTE_BALLOON if ch == "7" else NOTE_KUSUDAMA
                bal = cnt if cnt is not None else -1
                rec = {"type": t, "measure": pos,
                       "balloonHitCount": bal, "longNoteLength": -1}
                recs.append(rec)
                open_roll = (rec, note_time)
            elif ch == "8":
                # Roll/balloon END: this format has NO separate end note. The '8'
                # only closes the open roll/balloon and sets the START note's
                # longNoteLength (duration). Writing an actual type-8 note draws a
                # stray big-ka in-game. ALL roll/balloon kinds carry the duration.
                if open_roll is not None:
                    start_rec, start_time = open_roll
                    dur = int(round(note_time - start_time))
                    if dur < 1:
                        dur = 1
                    start_rec["longNoteLength"] = dur
                    open_roll = None
            # other characters ignored

        measure_recs.append((m, measure_start, bpm, recs))
        cur_time_ms = measure_start + measure_ms

    # Second pass: lay out the note table. The Gen2 format stores each measure's
    # note block TWICE consecutively -- sub-track 0 points at the first copy and
    # sub-track 3 at the second, matching every real corpus chart. Reproduce
    # that so converter output is structurally identical to genuine charts.
    #
    # The 3rd sub-track field ("pointGain") is NOT a constant: across all 712
    # corpus charts it is the count of notes (one copy) REMAINING AFTER this
    # measure, i.e. total_one_copy_notes - cumulative_notes_through_this_measure.
    # The game uses this remaining-note countdown (soul-gauge / progress); a
    # constant value freezes it at chart load. Slots 0 & 3 carry the countdown
    # even for empty measures; the unused slots stay 0.
    total_notes = sum(len(recs) for (_m, _s, _b, recs) in measure_recs)
    remaining = total_notes
    for (m, measure_start, bpm, recs) in measure_recs:
        copy0_start = len(notes)
        for r in recs:
            notes.append(dict(r))
        copy1_start = len(notes)
        for r in recs:
            notes.append(dict(r))
        cnt = len(recs)
        remaining -= cnt                      # notes left after this measure

        # The 6 sub-track noteIndexSt values MUST be contiguous in slot order
        # (true for all 712 corpus charts): each slot points at the running note
        # cumulative, whether or not it holds notes. So the empty slots BETWEEN
        # the two copies (1,2) point at copy1_start, and the empty slots AFTER
        # them (4,5) point past both copies. Pointing every empty slot at
        # len(notes) breaks the game's note iteration and HANGS it at chart load.
        end = len(notes)                          # after both copies
        subs = []
        for s in range(6):
            if s == 0:
                subs.append({"noteIndexSt": copy0_start, "noteCount": cnt,
                             "pointGain": remaining})
            elif s == 3:
                subs.append({"noteIndexSt": copy1_start, "noteCount": cnt,
                             "pointGain": remaining})
            elif s in (1, 2):
                subs.append({"noteIndexSt": copy1_start,   # boundary between copies
                             "noteCount": 0, "pointGain": 0})
            else:                                          # slots 4, 5: after both copies
                subs.append({"noteIndexSt": end,
                             "noteCount": 0, "pointGain": 0})

        tracks.append({
            "time": measure_start,
            "bpm": bpm,
            "trackLine": m.get("barline", 1),
            "gogoFlag": 1 if m["gogo"] else 0,
            "_unk": 0,
            "bunkis": [-1] * 6,
            "scrollSpeeds": [float(m["scroll"])] * 6,
            "subtracks": subs,
        })

    # Ensure at least one track so the file is valid even for empty charts.
    if not tracks:
        tracks.append({
            "time": cur_time_ms, "bpm": global_bpm, "trackLine": 1,
            "gogoFlag": 0, "_unk": 0, "bunkis": [-1] * 6,
            "scrollSpeeds": [1.0] * 6,
            "subtracks": [{"noteIndexSt": 0, "noteCount": 0, "pointGain": 0}
                          for _ in range(6)],
        })

    note_offset = HEADER_SIZE + len(tracks) * TRACK_NEW_SIZE
    model = {
        "noteOffset": note_offset,
        "padding": 0,
        "isOld": False,
        "tracks": tracks,
        "notes": notes,
        "_gap": b"",
        "_tail": b"",
    }
    return serialize_sht(model)


def tja_to_all_charts(tja_text):
    """Convert every course present in the TJA to sht bytes.

    Returns {'<diff>': sht_bytes, '<diff>_2p': sht_bytes, ...}.
    2p charts mirror 1p (same bytes) -- the Gen2 format does not encode a
    player-side flip inside the chart, that is selected by file name/slot.
    """
    parsed = parse_tja(tja_text)
    out = {}
    for cidx in sorted(parsed["courses"].keys()):
        diff = _DIFF_NAMES.get(cidx, "oni")
        data = convert_tja(tja_text, diff, "1p")
        out[diff] = data
        out[diff + "_2p"] = data  # mirror
    return out


# ===========================================================================
# Self-test
# ===========================================================================
_SAMPLE_TJA = """TITLE:Test Song
BPM:160
OFFSET:-1.5
WAVE:test.ogg

COURSE:Oni
LEVEL:8
BALLOON:7,12
#START
1020,
1212,
#BPMCHANGE 180
#GOGOSTART
3030,
#GOGOEND
#SCROLL 1.5
7008,
1111111100000000,
#MEASURE 3/4
101010,
#END

COURSE:Easy
LEVEL:3
#START
1010,
2020,
#END
"""


def _corpus_roundtrip():
    """Round-trip all 720 corpus charts; return (passed, total, failures)."""
    try:
        from pathlib import Path
        import taiko256_explorer_gui6 as g
    except Exception as e:  # pragma: no cover
        print("  (corpus unavailable: %s)" % e)
        return (0, 0, [])

    a = g.Archive(Path("list.bin"), Path("DATA.000"), fmt=2)
    groups = [grp for grp in a.layout.groups if grp["name"].startswith("fumen.")]
    passed = 0
    total = 0
    failures = []
    for grp in groups:
        entries = a.layout.files_for_group(grp)
        for e in entries:
            if e["name"] != "sht":
                continue
            total += 1
            data = a.read_file(grp, e)
            try:
                model = parse_sht(data)
                out = serialize_sht(model)
                if out == data:
                    passed += 1
                else:
                    failures.append((grp["name"], "bytes differ",
                                     len(data), len(out)))
            except Exception as ex:  # pragma: no cover
                failures.append((grp["name"], repr(ex), len(data), 0))
    return passed, total, failures


def main():
    ok = True

    print("=" * 64)
    print("STEP 1: corpus round-trip  serialize_sht(parse_sht(d)) == d")
    print("=" * 64)
    passed, total, failures = _corpus_roundtrip()
    print("  round-trip: %d/%d charts byte-exact" % (passed, total))
    if failures:
        ok = False
        for f in failures[:10]:
            print("    FAIL", f)
    if total > 0 and passed != total:
        ok = False

    print()
    print("=" * 64)
    print("STEP 2/3: TJA -> sht conversion + re-parse sanity check")
    print("=" * 64)
    charts = tja_to_all_charts(_SAMPLE_TJA)
    for diff in sorted(charts):
        if diff.endswith("_2p"):
            continue
        data = charts[diff]
        try:
            model = parse_sht(data)
        except Exception as ex:
            ok = False
            print("  %-8s RE-PARSE FAILED: %r" % (diff, ex))
            continue
        ntracks = len(model["tracks"])
        nnotes = len(model["notes"])
        bpms = sorted(set(round(t["bpm"], 2) for t in model["tracks"]))
        times = [t["time"] for t in model["tracks"]]
        monotonic = all(times[i] <= times[i + 1] for i in range(len(times) - 1))
        rt = serialize_sht(model) == data
        sane = (ntracks > 0 and monotonic and rt)
        if diff == "oni":
            sane = sane and (nnotes > 0)
        ok = ok and sane
        print("  %-8s tracks=%d notes=%d bpms=%s monotonic=%s roundtrip=%s  %s"
              % (diff, ntracks, nnotes, bpms, monotonic, rt,
                 "OK" if sane else "BAD"))

    # write a sample tja to scratchpad (allowed)
    try:
        import os
        scratch = os.environ.get(
            "TMP",
            r"C:\Users\User\AppData\Local\Temp\claude\D--\scratchpad")
        path = os.path.join(scratch, "sample.tja")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_SAMPLE_TJA)
    except Exception:
        pass

    print()
    print("=" * 64)
    verdict = "PASS" if ok else "FAIL"
    print("OVERALL: %s" % verdict)
    print("=" * 64)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

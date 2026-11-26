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
    int measure / position offset of the note WITHIN its measure, in 1/48 of
                           a 4/4 bar (240000/bpm ms) -- NOT of the measure, so
                           a 3/4 measure uses 0..35 (observed range 0..47)
    int balloonHitCount    hit count for balloon/kusudama notes, else -1
    int longNoteLength     duration (in `time` units) for rolls/balloons, else -1

NOTE TYPES (the same numbers in Gen2 and Gen3; the NOTE_* constants below):
    1/2/3 = don (three sounds), 4/5 = ka, 7 = big don, 8 = big ka,
    6 = drumroll and 9 = big drumroll (longNoteLength = duration),
    10 = balloon and 12 = kusudama (balloonHitCount = hits, plus duration),
    11 / 13 = two-hand don / ka (TJA A / B).
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

# Granularity of in-measure note position: 1/48 of a 4/4 BAR at the track's bpm
POS_DIV = 48

# NOTE: a measure-splitting feature used to live here (emitting k track records
# per measure to reach FINER grids). It was REVERTED: it made charts play wrong
# in-game (1/24 measures that were previously fine came out broken). Do not
# reintroduce that. What convert_tja does now is different and is what retail
# does: a measure LONGER than 4/4 becomes one 4/4 track plus a remainder track
# without a bar line -- T14 ships that exact pattern 680 times (4/4 + 1/4,
# i.e. 5/4), 214 times as 4/4 + 3/4 and 136 times as 4/4 + 2/4.

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
# whole track record: time,bpm,line,gogo [,unk] 6 bunkis, 6 scrolls, 6x(st,cnt,pts)
_TRACK_NEW_ST = struct.Struct("<ffii6i6f18i")
_TRACK_OLD_ST = struct.Struct("<ffiii6i6f18i")
_NOTE_ST = struct.Struct("<iiii")


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
    tstruct = _TRACK_OLD_ST if is_old else _TRACK_NEW_ST
    unpack_from = tstruct.unpack_from
    for _ in range(track_count):
        v = unpack_from(data, off)
        if is_old:
            unk = v[4]
            o = 5
        else:
            unk = 0
            o = 4
        subs = [{"noteIndexSt": v[q], "noteCount": v[q + 1], "pointGain": v[q + 2]}
                for q in range(o + 12, o + 30, 3)]
        tracks.append({
            "time": v[0], "bpm": v[1], "trackLine": v[2], "gogoFlag": v[3],
            "_unk": unk, "bunkis": list(v[o:o + 6]),
            "scrollSpeeds": list(v[o + 6:o + 12]), "subtracks": subs,
        })
        off += trec

    notes = [{"type": t, "measure": m, "balloonHitCount": bal, "longNoteLength": lng}
             for t, m, bal, lng in struct.iter_unpack(
                 "<iiii", memoryview(data)[note_offset:note_end])]

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

    gap_end = HEADER_SIZE + track_count * trec + len(gap)
    note_start = max(gap_end, note_offset)
    out = bytearray(note_start + note_count * NOTE_SIZE + len(tail))
    struct.pack_into("<IIii", out, 0, track_count, note_offset, note_count,
                     model.get("padding", 0))

    tstruct = _TRACK_OLD_ST if is_old else _TRACK_NEW_ST
    pack_into = tstruct.pack_into
    zero_sub = (0, 0, 0)
    off = HEADER_SIZE
    for t in tracks:
        bunkis = list(t["bunkis"]) + [-1] * (6 - len(t["bunkis"]))
        scrolls = list(t["scrollSpeeds"]) + [1.0] * (6 - len(t["scrollSpeeds"]))
        subs = t["subtracks"]
        flat = []
        for s in range(6):
            if s < len(subs):
                sd = subs[s]
                flat += (sd["noteIndexSt"], sd["noteCount"], sd["pointGain"])
            else:
                flat += zero_sub
        if is_old:
            pack_into(out, off, t["time"], t["bpm"], t["trackLine"], t["gogoFlag"],
                      t.get("_unk", 0), *bunkis[:6], *scrolls[:6], *flat)
        else:
            pack_into(out, off, t["time"], t["bpm"], t["trackLine"], t["gogoFlag"],
                      *bunkis[:6], *scrolls[:6], *flat)
        off += trec

    # gap (between track table and note table); the zero padding up to
    # noteOffset is already in the zeroed buffer
    out[off:off + len(gap)] = gap

    off = note_start
    pack_into = _NOTE_ST.pack_into
    for n in notes:
        pack_into(out, off, n["type"], n["measure"],
                  n["balloonHitCount"], n["longNoteLength"])
        off += NOTE_SIZE

    out[off:off + len(tail)] = tail
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
_SKIP_COURSE = -99      # parse_tja slot for courses this tool can't use
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


class _NoteLineTable(dict):
    """str.translate table: whitespace dropped, anything that is not a note
    digit / A / B / comma becomes "0". Filled lazily per character seen."""
    def __missing__(self, k):
        c = chr(k)
        v = None if c.isspace() else (c if c in "0123456789AB," else "0")
        self[k] = v
        return v


_NOTE_LINE_TABLE = _NoteLineTable()


def _to_float(s, default=0.0):
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def parse_tja(text):
    """Parse a TJA file into a dict:
        {"meta": {...global...},
         "courses": { course_idx: {"meta": {...}, "measures": [...],
                                   "balloon": [...], "branch_conds": {...}}}}

    Each course's "measures" is the ordered list of measures, where a
    measure is a dict:
        {"notes": "1020...", "bpm": float, "scroll": float, "gogo": bool,
         "branch": int, "measure_num": int, "measure_den": int, ...}
    A measure cut by a mid-measure command is stored as several pieces with
    a "beats" length. Courses this tool can't use (Tower, Dan) are dropped,
    and only one chart per course is kept (P1 of a STYLE:Double block).
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
            "balloon": list(meta.get("BALLOON", [])),
        }

    def new_state(cmeta):
        return {
            "bpm": _to_float(cmeta.get("BPM", meta.get("BPM", "120")), 120.0),
            "scroll": 1.0,
            "gogo": False,
            "measure_num": 4,
            "measure_den": 4,
            "buffer": "",      # accumulated note chars for current measure
            "pieces": [],      # parts of this measure cut by mid-measure commands
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
                    # a TJA without COURSE: is an Oni chart
                    cur_course = 3
                    courses[cur_course] = new_course(cur_course)
                # Keep one chart per course: the single-player one, or P1 of a
                # STYLE:Double block. A second #START (P2, or the double chart
                # after the single one) would otherwise be laid end to end.
                if (arg.upper().replace(" ", "") == "P2"
                        or courses[cur_course].get("started")):
                    state = None
                    continue
                courses[cur_course]["started"] = True
                state = new_state(courses[cur_course]["meta"])
                continue
            if cmd == "END":
                # flush any partial buffer as a final measure
                if state is not None and (state["buffer"] or state["pieces"]):
                    _flush_measure(courses[cur_course], state)
                in_chart = False
                state = None
                continue
            if state is None:
                # commands outside START (rare); ignore
                continue

            if cmd in ("BPMCHANGE", "SCROLL", "GOGOSTART", "GOGOEND", "DELAY") \
                    and state["buffer"]:
                # A change in the middle of a measure: the notes so far become
                # their own piece with the old state (see _flush_measure).
                _cut_piece(state)

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
                if state["buffer"] or state["pieces"]:
                    _flush_measure(courses[cur_course], state)
                if cmd == "N":
                    state["branch"] = BRANCH_NORMAL
                elif cmd == "E":
                    state["branch"] = BRANCH_EXPERT
                elif cmd == "M":
                    state["branch"] = BRANCH_MASTER
                elif cmd == "BRANCHSTART":
                    # The condition (p = accuracy %, r = drumroll hits, s =
                    # score; then two thresholds) is kept per branch block;
                    # convert_tja turns it into the track's bunkis.
                    state["branch"] = BRANCH_COMMON
                    state["branch_seq"] = state.get("branch_seq", 0) + 1
                    cp = [x.strip() for x in arg.split(",")]
                    kind = (cp[0][:1] or "p").lower()
                    vals = [_to_float(x, 0.0) for x in cp[1:3]] + [0.0, 0.0]
                    courses[cur_course].setdefault("branch_conds", {})[
                        state["branch_seq"]] = (kind, vals[0], vals[1])
                elif cmd == "BRANCHEND":
                    state["branch"] = BRANCH_COMMON
            elif cmd == "BARLINEOFF":
                state["barline"] = 0
            elif cmd == "BARLINEON":
                state["barline"] = 1
            elif cmd == "SECTION":
                # resets the accuracy count the next branch condition uses
                if state["buffer"] or state["pieces"]:
                    _flush_measure(courses[cur_course], state)
                state["section"] = True
            # other commands (LEVELHOLD, ...) intentionally ignored
            continue

        if ":" in line and not in_chart:
            # header field   KEY:value
            key, val = line.split(":", 1)
            key = key.strip().upper()
            val = val.strip()
            if key == "COURSE":
                # unknown courses (Tower, Dan, ...) are parsed into a slot
                # that is dropped at the end, so they can't replace Oni
                idx = _COURSE_ALIASES.get(val.strip().lower(), _SKIP_COURSE)
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
            # Skip stray header-style "KEY:value" lines between #START and
            # #END. Other note letters (OpenTaiko C/D/F/G...) become blanks so
            # the measure keeps its length and its comma.
            if ":" in line:
                continue
            seg = line.upper().translate(_NOTE_LINE_TABLE)
            # a line may contain a trailing ',' ending a measure; may have
            # multiple commas; accumulate
            while "," in seg:
                pre, seg = seg.split(",", 1)
                state["buffer"] += pre
                _flush_measure(courses[cur_course], state)
            state["buffer"] += seg

    courses.pop(_SKIP_COURSE, None)
    for c in courses.values():
        c.pop("started", None)
    return {"meta": meta, "courses": courses}


def _cut_piece(state):
    """End the current piece of a measure at a mid-measure command."""
    state["pieces"].append(_measure_dict(state, state["buffer"]))
    state["buffer"] = ""


def _flush_measure(course, state):
    """End a measure at its comma. A measure cut by mid-measure commands
    (#BPMCHANGE, #SCROLL, #GOGO*, #DELAY) is stored as its pieces: each
    takes its share of the measure's beats by note count, and the ones after
    the first have no bar line -- one track each in the sht, the way
    tja2fumen and the official charts split them."""
    pieces = state["pieces"]
    state["pieces"] = []
    last = _measure_dict(state, state["buffer"])
    state["buffer"] = ""
    if not pieces:
        course["measures"].append(last)
        return
    if last["notes"] or last["delay_ms"]:
        pieces.append(last)
    total = sum(len(p["notes"]) for p in pieces)
    beats = 4.0 * max(0, pieces[0]["measure_num"]) / float(
        pieces[0]["measure_den"] or 4)
    for i, p in enumerate(pieces):
        p["beats"] = beats * len(p["notes"]) / float(total)
        if i:
            p["barline"] = 0
        course["measures"].append(p)


def _measure_dict(state, notes):
    # A pending #DELAY is applied once, to this measure, then cleared.
    delay_ms = state.get("delay_ms", 0.0)
    state["delay_ms"] = 0.0
    return {
        "notes": notes,
        "bpm": state["bpm"],
        "scroll": state["scroll"],
        "gogo": state["gogo"],
        "branch": state["branch"],
        "measure_num": state["measure_num"],
        "measure_den": state["measure_den"],
        "barline": state.get("barline", 1),
        "delay_ms": delay_ms,
        # which #BRANCHSTART block a branch measure belongs to
        "bseq": (state.get("branch_seq", 0)
                 if state["branch"] != BRANCH_COMMON else None),
        "section": state.pop("section", False),
    }


# TJA note char -> sht note type
_TJA_NOTE_MAP = {
    "1": NOTE_DON,
    "2": NOTE_KA,
    "3": NOTE_BIGDON,
    "4": NOTE_BIGKA,
    "A": NOTE_DON2HAND,      # two-hand big don / ka (OpenTaiko)
    "B": NOTE_KA2HAND,
    # 5 = drumroll start, 6 = big drumroll start, 7 = balloon, 8 = roll/balloon end,
    # 9 = kusudama  -> handled specially below
}


# ===========================================================================
# STEP 3 -- TJA -> sht conversion
# ===========================================================================
def convert_tja(tja_text, course, player="1p", branches=True, warn=None):
    """Convert one TJA course to Gen2 .sht bytes.

    course in {'easy','normal','hard','oni'} (also accepts 'edit'/index).
    player is accepted for API symmetry; 2p charts mirror 1p here.

    Produces a STRUCTURALLY VALID sht: correct header, a track record per
    measure with correct time/bpm/scroll/gogo and sub-track note indices, and
    a note table with notes placed at the correct in-measure positions.

    Branches (#BRANCHSTART/#N/#E/#M/#BRANCHEND) keep all three routes in the
    official branched layout, with the #BRANCHSTART thresholds as bunkis.
    branches=False (or routes of different lengths) collapses them to the
    common path plus SELECTED_BRANCH instead. Roll/balloon start markers (5/6/7/9) are paired with their 8 end
    marker and back-patched with the elapsed ms in longNoteLength. `warn`,
    if given, is called with a message when something can't be kept (routes
    that don't line up, score conditions). Each
    measure's note block is written twice (sub-track 0 + sub-track 3) to match
    the corpus duplication invariant.
    """
    return _convert_parsed(parse_tja(tja_text), course, player, branches, warn)


def _convert_parsed(parsed, course, player="1p", branches=True, warn=None):
    """convert_tja on an already parsed TJA (parse_tja output is only read)."""
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
    all_balloons = list(cdata.get("balloon", gmeta.get("BALLOON", [])))
    branched = branches and any(m.get("branch", BRANCH_COMMON) != BRANCH_COMMON
                                for m in cdata["measures"])

    def route_balloons(route):
        """Balloon counts met on `route`. A per-route list (OpenTaiko) is
        exactly that; a plain BALLOON list runs over every balloon in file
        order, all routes included, so keep the ones on this route."""
        if not any(m.get("branch", BRANCH_COMMON) != BRANCH_COMMON
                   for m in cdata["measures"]):
            return list(all_balloons)
        per = meta.get({BRANCH_NORMAL: "BALLOONNOR", BRANCH_EXPERT: "BALLOONEXP",
                        BRANCH_MASTER: "BALLOONMAS"}[route])
        if per:
            return [int(_to_float(x)) for x in
                    per.replace(" ", "").split(",") if x != ""]
        it, kept = iter(all_balloons), []
        for m in cdata["measures"]:
            keep = m.get("branch", BRANCH_COMMON) in (BRANCH_COMMON, route)
            ns = m["notes"]
            if "7" not in ns and "9" not in ns:
                continue
            for ch in ns:
                if ch in "79":
                    v = next(it, None)
                    if keep and v is not None:
                        kept.append(v)
        return kept

    ceil = math.ceil

    def layout(route):
        """First pass for one route: (measure, start ms, bpm, note recs) per
        track. COMMON measures plus the route's own -- laying N/E/M end to
        end would make the chart 2-3x too long."""
        src_measures = [m for m in cdata["measures"]
                        if m.get("branch", BRANCH_COMMON) in (BRANCH_COMMON, route)]
        bal_iter = iter(route_balloons(route))
        # running time in ms; TJA OFFSET is seconds before the first measure
        cur_time_ms = -offset * 1000.0
        # Notes are recorded without long durations first, so rolls/balloons
        # can be paired start->8 across measures and back-patched with their
        # true ms duration.
        measure_recs = []
        open_roll = None     # (note_dict, start_time_ms) for an unpaired 5/6/7/9

        def close_roll(t_ms):
            """Give the open roll/balloon its length, ending at t_ms. Called
            at its '8', at a new roll that starts before one (no '8'), and at
            the end of the chart."""
            nonlocal open_roll
            if open_roll is not None:
                start_rec, start_time = open_roll
                start_rec["longNoteLength"] = max(1, int(round(t_ms - start_time)))
                open_roll = None
        for m in src_measures:
            bpm = m["bpm"] if (m["bpm"] and m["bpm"] > 0) else global_bpm
            num = max(0, m["measure_num"])
            den = m["measure_den"] or 4
            beats = m.get("beats", 4.0 * (num / float(den)))
            measure_ms = (60000.0 / bpm) * beats
            if measure_ms < 0:
                measure_ms = 0.0

            # #DELAY shifts this measure (and everything after) later in time.
            cur_time_ms += m.get("delay_ms", 0.0)
            measure_start = cur_time_ms

            note_chars = "".join(m["notes"].split())
            n = len(note_chars)

            # The Gen2 position grid is 1/48 of a 4/4 BAR at this tempo, not 1/48
            # of the measure: official charts put a 3/4 measure on 0..35 (verified
            # 99.96% on the 390 chart pairs shipped by both T14 and Nijiiro). A
            # measure longer than 4/4 cannot fit 0..47, so -- like Nijiiro does --
            # it becomes several tracks of one bar each, the extra ones without a
            # bar line.
            bar_ms = 240000.0 / bpm
            span = POS_DIV * beats / 4.0                 # grid units it occupies
            nseg = max(1, int(math.ceil(span / POS_DIV - 1e-9)))
            seg_recs = [[] for _ in range(nseg)]
            used_pos = set()
            top0 = min(POS_DIV, int(ceil(span - 1e-9)))
            top1 = max(top0, 1) - 1
            for i, ch in enumerate(note_chars):
                if ch == "0":
                    continue
                # Position in 1/48-bar units, clamped to the valid range. Two
                # notes that 1/48 can't tell apart (a 5/7-tuplet) move to a free
                # neighbouring slot; only when both neighbours are taken (a
                # measure denser than the grid) do they share one, so a dense
                # run can't push the rest of the measure off its time.
                q = int(round(i * span / n)) if n > 0 else 0
                if nseg == 1:
                    # single-bar fast path (same values as the general case)
                    seg = 0
                    top = top0
                    pos = q if q > 0 else 0
                    if pos > top1:
                        pos = top1
                else:
                    seg = min(max(q, 0) // POS_DIV, nseg - 1)
                    top = min(POS_DIV, int(ceil(span - seg * POS_DIV - 1e-9)))
                    pos = min(max(q - seg * POS_DIV, 0), max(top, 1) - 1)
                if ch != "8":
                    # '8' only ends a roll: it takes no slot and needs no nudge.
                    key = seg * 64 + pos          # pos < 64: one int per slot
                    if key in used_pos:
                        for cand in (pos + 1, pos - 1):
                            if (0 <= cand < top
                                    and seg * 64 + cand not in used_pos):
                                pos = cand
                                break
                    used_pos.add(seg * 64 + pos)
                recs = seg_recs[seg]

                note_time = measure_start + (seg + pos / float(POS_DIV)) * bar_ms
                if ch == "8" and n > 0:
                    # exact end time: a TJA may place it finer than the 1/48 grid
                    note_time = measure_start + (i / float(n)) * measure_ms

                if ch in _TJA_NOTE_MAP:
                    close_roll(note_time)   # a roll left open ends at a note
                    rec = {"type": _TJA_NOTE_MAP[ch], "measure": pos,
                           "balloonHitCount": -1, "longNoteLength": -1}
                    recs.append(rec)
                elif ch in ("5", "6"):
                    close_roll(note_time)
                    t = NOTE_DRUMROLL if ch == "5" else NOTE_BIGDRUMROLL
                    rec = {"type": t, "measure": pos,
                           "balloonHitCount": -1, "longNoteLength": -1}
                    recs.append(rec)
                    open_roll = (rec, note_time)
                elif ch in ("7", "9"):
                    # 7 = balloon, 9 = kusudama. Both are type 10 / 12 and carry a hit
                    # count (from the TJA BALLOON: header) + a duration (set at the '8').
                    close_roll(note_time)
                    cnt = next(bal_iter, None)
                    t = NOTE_BALLOON if ch == "7" else NOTE_KUSUDAMA
                    # a balloon the BALLOON: list has no count for gets 5
                    bal = cnt if cnt is not None and cnt > 0 else 5
                    rec = {"type": t, "measure": pos,
                           "balloonHitCount": bal, "longNoteLength": -1}
                    recs.append(rec)
                    open_roll = (rec, note_time)
                elif ch == "8":
                    # Roll/balloon END: this format has NO separate end note. The '8'
                    # only closes the open roll/balloon and sets the START note's
                    # longNoteLength (duration). Writing an actual type-8 note draws a
                    # stray big-ka in-game. ALL roll/balloon kinds carry the duration.
                    close_roll(note_time)
                # other characters ignored

            for j, recs in enumerate(seg_recs):
                mj = m if j == 0 else dict(m, barline=0)
                measure_recs.append((mj, measure_start + j * bar_ms, bpm, recs))
            cur_time_ms = measure_start + measure_ms
        close_roll(cur_time_ms)
        return measure_recs

    tracks = []
    notes = []
    if branched:
        routes3 = [layout(r) for r in (BRANCH_NORMAL, BRANCH_EXPERT, BRANCH_MASTER)]
        if len({len(r) for r in routes3}) == 1 and all(
                abs(routes3[0][k][1] - routes3[b][k][1]) < 1.0
                for b in (1, 2) for k in range(len(routes3[0]))):
            conds = cdata.get("branch_conds", {})
            if warn and any(c[0] == "s" for c in conds.values()):
                warn("score branch conditions (#BRANCHSTART s,...) have no "
                     "Gen2 equivalent; those branches stay on Normal")
            tracks, notes = _diverge_layout(routes3, conds)
            return _finish_sht(tracks, notes, cur_time_ms=-offset * 1000.0,
                               global_bpm=global_bpm)
        if warn:
            warn("branch routes have different lengths or timing; only the "
                 "Master route was kept")
        # routes whose measures don't line up cannot share one track list:
        # keep one
    measure_recs = layout(SELECTED_BRANCH)

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

    return _finish_sht(tracks, notes, cur_time_ms=-offset * 1000.0,
                       global_bpm=global_bpm)


def limit_note_count(model):
    """The note count to hold against DIFFICULTY_NOTE_LIMIT. Those limits
    were measured on unbranched charts, which store every note twice; a
    branched chart stores each of its three routes twice (six copies), so
    it is judged by its busiest route, twice -- as if that route were the
    whole chart. (The shipped branched butou3 Oni has 4772 stored notes,
    over the 4668 limit, and loads fine.)"""
    tr = model["tracks"]
    branched = (any(list(t["bunkis"]) != [-1] * 6 for t in tr) or
                any(t["subtracks"][s]["noteCount"] for t in tr for s in (1, 2)))
    if not branched:
        return len(model["notes"])
    return max(2 * sum(t["subtracks"][b]["noteCount"] for t in tr)
               for b in range(3))


def _finish_sht(tracks, notes, cur_time_ms, global_bpm):
    # Ensure at least one track so the file is valid even for empty charts.
    if not tracks:
        tracks.append({
            "time": cur_time_ms, "bpm": global_bpm, "trackLine": 1,
            "gogoFlag": 0, "_unk": 0, "bunkis": [-1] * 6,
            "scrollSpeeds": [1.0] * 6,
            "subtracks": [{"noteIndexSt": 0, "noteCount": 0, "pointGain": 0}
                          for _ in range(6)],
        })
    return serialize_sht({
        "noteOffset": HEADER_SIZE + len(tracks) * TRACK_NEW_SIZE,
        "padding": 0,
        "isOld": False,
        "tracks": tracks,
        "notes": notes,
        "_gap": b"",
        "_tail": b"",
    })


# Diverge points per note, as the game counts them for an accuracy (p)
# branch condition: 20 per don/ka of either size, 30 per balloon/kusudama,
# none for drumrolls (sht_export uses the same values).
def _diverge_points(recs):
    return sum(30 if r["type"] in (NOTE_BALLOON, NOTE_KUSUDAMA) else
               0 if r["type"] in (NOTE_DRUMROLL, NOTE_BIGDRUMROLL) else 20
               for r in recs)


def _bunki_values(cond, points):
    """A #BRANCHSTART condition -> the six Gen2 thresholds (a pair per route).

    p: percent of the diverge points each route offered since the last
    branch point or #SECTION -- the shipped charts store the same percentages
    times each route's own points, so the three pairs differ. 0 always
    passes; over 100, or no points to count, never does (999). r: drumroll
    hits, the same on every route. s (score) has no Gen2 equivalent and
    becomes 999 (stay on Normal)."""
    kind, a, b = cond

    def one(v, pts):
        if v == -1:                 # "no decision here" (sht_export)
            return -1
        if kind == "s":
            # a score threshold has no diverge-point equivalent: stay on
            # the normal route rather than guess
            return 999
        if kind == "p":
            if v <= 0:
                return 0
            if v > 100 or pts <= 0:
                # over 100%, or nothing to count before the branch point
                # (a TJA player stays on Normal there): never
                return 999
            v = int(pts * v / 100.0 + 1e-6)
        else:
            v = max(0, int(round(v)))
        # 999 means "never" to the game; a real threshold of 999 is 1000
        return 1000 if v == 999 else v
    return [one(x, points[r]) for r in range(3) for x in (a, b)]


def _diverge_layout(routes3, conds):
    """Tracks and notes for a branched chart, laid out like the official
    ones (T14 butou*, alrpri): per measure the three routes' notes stored as
    SIX copies [n, p, m, n, p, m], sub-tracks 0-2 / 3-5 pointing at them,
    pointGain a per-route remaining-note countdown, scroll per route, and the
    thresholds in bunkis on the measure where a branch block starts."""
    K = len(routes3[0])
    tracks, notes = [], []
    extend = notes.extend
    remaining = [sum(len(rec[3]) for rec in r) for r in routes3]
    prev_seq, since = None, 0
    ra, rb, rc = routes3
    for k in range(K):
        recs3 = (ra[k], rb[k], rc[k])
        m, start, bpm, _ = recs3[2]
        seqs = [x[0].get("bseq") for x in recs3]
        seq = next((x for x in seqs if x is not None), None)
        bunkis = [-1] * 6
        if seq is not None and seq != prev_seq:
            # A #SECTION right before #BRANCHSTART resets the count for the
            # NEXT branch point, so this one is counted first.
            pts = [sum(_diverge_points(routes3[r][j][3]) for j in range(since, k))
                   for r in range(3)]
            bunkis = _bunki_values(conds.get(seq, ("p", 101.0, 101.0)), pts)
            since = k
        if (recs3[0][0].get("section") or recs3[1][0].get("section")
                or recs3[2][0].get("section")):
            since = k
        if seq is not None:
            prev_seq = seq
        lists = (recs3[0][3], recs3[1][3], recs3[2][3])
        counts = (len(lists[0]), len(lists[1]), len(lists[2]))
        n_before = len(notes)
        starts = [n_before, n_before + counts[0],
                  n_before + counts[0] + counts[1]]
        starts += [n_before + sum(counts) + (x - n_before) for x in starts]
        for _copy in (0, 1):
            for lst in lists:
                if lst:
                    extend([r.copy() for r in lst])
        rem = remaining
        for b in range(3):
            rem[b] -= counts[b]
        subs = [{"noteIndexSt": starts[s], "noteCount": counts[s % 3],
                 "pointGain": rem[s % 3]} for s in range(6)]
        tracks.append({
            "time": start,
            "bpm": bpm,
            "trackLine": m.get("barline", 1),
            "gogoFlag": 1 if m["gogo"] else 0,
            "_unk": 0,
            "bunkis": bunkis,
            "scrollSpeeds": [float(recs3[s % 3][0]["scroll"])
                             for s in range(6)],
            "subtracks": subs,
        })
    return tracks, notes


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
        data = _convert_parsed(parsed, diff, "1p")
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
        from taiko2x6.archive import archive_model as g
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

    # write a sample tja to the samples output folder
    try:
        import os
        from taiko2x6.app import apppaths
        scratch = os.environ.get(
            "TMP",
            str(apppaths.samples_dir() / "out"))
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

#!/usr/bin/env python3
"""
SHT validator — check a Gen2 .sht chart against the official on-disc format.

Every rule here was mined from the 712 genuine charts in the game: rules that
ALL of them satisfy are ERRORs when violated (the chart is not official and can
crash / freeze the game at load); rules only most satisfy are WARNINGs.

Usage:
    python sht_validator.py <file.sht> [easy|normal|hard|oni]
    from sht_validator import validate_sht           # -> list[Issue]
GUI: ShtValidatorDialog (also wired into the Explorer Tools menu).
"""
from __future__ import annotations

import logging
import math
import struct
from dataclasses import dataclass

import tja2sht

logger = logging.getLogger("taiko.shtvalidator")

ERROR, WARN, INFO, OK = "ERROR", "WARN", "INFO", "OK"

# Positions (in-measure, 1/48 units) actually used across the whole corpus.
# Values outside this set are never seen (WARN); >=48 or <0 is invalid (ERROR).
CORPUS_POSITIONS = {0, 2, 3, 4, 6, 8, 9, 10, 12, 14, 15, 16, 18, 20, 21, 22, 24,
                    26, 27, 28, 30, 31, 32, 33, 34, 36, 38, 39, 40, 42, 44, 45, 46}
# Only these note types carry a value in longNoteLength; all others must be -1.
LEN_CARRYING_TYPES = {6, 9, 10, 12}
VALID_NOTE_TYPES = set(range(1, 14))                 # 1..13 seen in the corpus


@dataclass
class Issue:
    severity: str            # ERROR / WARN / INFO
    rule: str
    detail: str

    def __str__(self):
        return f"[{self.severity}] {self.rule}: {self.detail}"


def validate_sht(data: bytes, difficulty: str | None = None) -> list[Issue]:
    """Return a list of Issues; an empty list (or only INFO) means it's valid.

    `difficulty` (easy/normal/hard/oni) enables the per-difficulty note-buffer
    check — a chart over that limit freezes the game on that difficulty.
    """
    issues: list[Issue] = []
    E = lambda r, d: issues.append(Issue(ERROR, r, d))
    W = lambda r, d: issues.append(Issue(WARN, r, d))
    I = lambda r, d: issues.append(Issue(INFO, r, d))

    # ---- header (raw, independent of parse) --------------------------------
    if len(data) < 16:
        E("header", f"file too small ({len(data)} bytes) for a 16-byte header")
        return issues
    track_count, note_offset, note_count, padding = struct.unpack_from("<IIii", data, 0)
    if padding != 0:
        W("header.padding", f"header padding is {padding}, expected 0")
    region = note_offset - 16
    trec = None
    if track_count == 0:
        E("header.trackCount", "trackCount is 0 (no measures)")
    elif region < 0 or region % track_count != 0:
        E("header.noteOffset",
          f"noteOffset({note_offset})-16 not divisible by trackCount({track_count})")
    else:
        rec = region // track_count
        if rec == tja2sht.TRACK_NEW_SIZE:
            trec = rec
        elif rec == tja2sht.TRACK_OLD_SIZE:
            trec = rec
            I("format", "OLD 140-byte track variant")
        else:
            E("header.trackSize",
              f"track record size {rec} is neither {tja2sht.TRACK_NEW_SIZE} nor "
              f"{tja2sht.TRACK_OLD_SIZE}")
    note_end = note_offset + note_count * 16
    if note_end != len(data):
        (E if note_end > len(data) else W)(
            "header.noteCount",
            f"note table ends at {note_end} but file is {len(data)} bytes "
            f"(noteCount={note_count})")

    # ---- structured parse --------------------------------------------------
    try:
        m = tja2sht.parse_sht(data)
    except Exception as exc:
        E("parse", f"parse_sht failed: {exc}")
        return issues
    tracks, notes = m["tracks"], m["notes"]

    # bpm / time / scroll sanity
    for i, t in enumerate(tracks):
        if not (t["bpm"] > 0) or math.isnan(t["bpm"]) or math.isinf(t["bpm"]):
            E("track.bpm", f"track {i}: bpm={t['bpm']} (must be > 0, finite)")
        if math.isnan(t["time"]) or math.isinf(t["time"]):
            E("track.time", f"track {i}: time={t['time']} not finite")
        for s in t["scrollSpeeds"]:
            if math.isnan(s) or math.isinf(s):
                E("track.scroll", f"track {i}: non-finite scroll {s}")
                break
        if t["trackLine"] not in (0, 1):
            W("track.trackLine", f"track {i}: trackLine={t['trackLine']} (usually 0/1)")
        if t["gogoFlag"] not in (0, 1):
            W("track.gogo", f"track {i}: gogoFlag={t['gogoFlag']} (usually 0/1)")

    times = [t["time"] for t in tracks]
    if times and times[0] < 0:
        E("track.firstTime", f"first measure time {times[0]} is negative")
    if any(times[i] > times[i + 1] for i in range(len(times) - 1)):
        # Rare but present in official charts (e.g. bou1p_e) — negative #DELAY /
        # branch artefacts — so it's a warning, not a hard error.
        W("track.timeOrder", "measure times decrease somewhere (unusual, seen in corpus)")
    elif any(times[i] == times[i + 1] for i in range(len(times) - 1)):
        W("track.timeOrder", "some consecutive measures share the same time")

    # ---- sub-track invariants (the big freeze causes) ----------------------
    expect = 0
    contiguous = True
    for ti, t in enumerate(tracks):
        for si, s in enumerate(t["subtracks"]):
            if s["noteIndexSt"] != expect:
                contiguous = False
            expect += s["noteCount"]
            if s["pointGain"] < 0:
                E("subtrack.pointGain", f"track {ti} slot {si}: negative pointGain")
    if not contiguous:
        E("subtrack.contiguous",
          "sub-track noteIndexSt values are NOT contiguous — the game mis-indexes "
          "the note table and FREEZES at load")
    if expect != len(notes):
        E("subtrack.coverage",
          f"sub-tracks reference {expect} notes but the table has {len(notes)}")

    # notes sorted within each active sub-track
    for ti, t in enumerate(tracks):
        for si, s in enumerate(t["subtracks"]):
            if s["noteCount"] > 0:
                seg = notes[s["noteIndexSt"]: s["noteIndexSt"] + s["noteCount"]]
                pos = [n["measure"] for n in seg]
                if pos != sorted(pos):
                    E("note.order", f"track {ti} slot {si}: notes not sorted by position")
                    break

    # pointGain must be the remaining-note countdown (total_one_copy - cumulative)
    total = sum(t["subtracks"][0]["noteCount"] for t in tracks)
    rem = total
    pg_ok = True
    for t in tracks:
        rem -= t["subtracks"][0]["noteCount"]
        for si in (0, 3):
            if t["subtracks"][si]["pointGain"] != rem:
                pg_ok = False
        for si in (1, 2, 4, 5):
            if t["subtracks"][si]["pointGain"] != 0:
                pg_ok = False
    if not pg_ok:
        # ~15% of official charts (those with rolls/balloons) use a different
        # remaining-note count, so a mismatch is a soft note, not an error.
        W("subtrack.pointGainCountdown",
          "pointGain differs from the common remaining-note countdown "
          "(slot0/3 = total_one_copy - cumulative); tolerated by the game")

    # duplicated copies (slot 0 vs slot 3) identical — usual but not universal
    dup_ok = True
    for t in tracks:
        s0, s3 = t["subtracks"][0], t["subtracks"][3]
        if s0["noteCount"] != s3["noteCount"]:
            dup_ok = False
            continue
        a = notes[s0["noteIndexSt"]: s0["noteIndexSt"] + s0["noteCount"]]
        b = notes[s3["noteIndexSt"]: s3["noteIndexSt"] + s3["noteCount"]]
        if [(x["type"], x["measure"]) for x in a] != [(x["type"], x["measure"]) for x in b]:
            dup_ok = False
    if not dup_ok:
        W("subtrack.duplication", "sub-track 0 and 3 copies differ (usually identical)")

    # ---- notes -------------------------------------------------------------
    bad_types = sorted({n["type"] for n in notes} - VALID_NOTE_TYPES)
    if bad_types:
        E("note.type", f"unknown note types present: {bad_types}")
    if any(n["measure"] >= 48 or n["measure"] < 0 for n in notes):
        E("note.position", "some note positions are outside 0..47")
    off_grid = sorted({n["measure"] for n in notes if 0 <= n["measure"] < 48}
                      - CORPUS_POSITIONS)
    if off_grid:
        W("note.position", f"positions never seen in official charts: {off_grid}")
    for n in notes:
        want_len = n["type"] in LEN_CARRYING_TYPES
        if not want_len and n["longNoteLength"] != -1:
            E("note.longLength",
              f"type {n['type']} has longNoteLength={n['longNoteLength']} "
              f"(must be -1 for this type)")
            break
        if want_len and n["longNoteLength"] < 0:
            W("note.longLength", f"type {n['type']} has no longNoteLength (-1)")
            break

    # roll/balloon start (5/6/7/9) should have a matching type-8 end after it
    depth = 0
    for n in notes:
        if n["type"] in (5, 6, 7, 9):
            depth += 1
        elif n["type"] == 8 and depth > 0:
            depth -= 1
    # (depth can stay >0 legitimately across the two copies; only flag negative)

    # ---- per-difficulty note-buffer limit ----------------------------------
    if difficulty:
        limit = tja2sht.DIFFICULTY_NOTE_LIMIT.get(difficulty.lower())
        if limit is not None:
            if len(notes) > limit:
                E("difficulty.limit",
                  f"{len(notes)} notes exceeds the {difficulty} buffer ({limit}) "
                  f"— FREEZES on this difficulty")
            else:
                I("difficulty.limit", f"{len(notes)}/{limit} notes ({difficulty}) — ok")

    I("summary", f"{len(tracks)} measures, {len(notes)} notes, "
                 f"first={times[0]:.0f}ms last={times[-1]:.0f}ms" if times else "empty")
    return issues


def verdict(issues: list[Issue]) -> str:
    if any(i.severity == ERROR for i in issues):
        return "INVALID (would crash/freeze)"
    if any(i.severity == WARN for i in issues):
        return "VALID with warnings"
    return "VALID — matches official format"


# ===========================================================================
# GUI
# ===========================================================================
try:
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QFileDialog, QLabel,
        QPlainTextEdit, QComboBox, QWidget,
    )

    class ShtValidatorDialog(QDialog):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("SHT Validator — check official format")
            self.resize(720, 560)
            lay = QVBoxLayout(self)
            row = QHBoxLayout()
            b_open = QPushButton("Open .sht…"); b_open.clicked.connect(self._open)
            row.addWidget(b_open)
            row.addWidget(QLabel("difficulty:"))
            self.cb = QComboBox(); self.cb.addItems(["(auto/none)", "easy", "normal", "hard", "oni"])
            row.addWidget(self.cb)
            self.lbl = QLabel(""); row.addWidget(self.lbl, 1)
            lay.addLayout(row)
            self.out = QPlainTextEdit(readOnly=True)
            lay.addWidget(self.out, 1)
            b_close = QPushButton("Close"); b_close.clicked.connect(self.accept)
            lay.addWidget(b_close)

        def _open(self):
            import appconfig                       # last-used-path memory
            paths = appconfig.pick_open_many(self, "sht", "SHT file(s)",
                                             "SHT (*.sht sht);;All (*)")
            if not paths:
                return
            self.out.clear()
            diff = self.cb.currentText()
            diff = None if diff.startswith("(") else diff
            worst = OK
            for p in paths:
                from pathlib import Path
                data = Path(p).read_bytes()
                # infer difficulty from the folder/file name if not chosen
                d = diff
                if d is None:
                    nm = Path(p).parent.name + Path(p).name
                    for suf, c in (("_e", "easy"), ("_n", "normal"), ("_h", "hard"), ("_m", "oni")):
                        if suf in nm:
                            d = c; break
                issues = validate_sht(data, d)
                v = verdict(issues)
                self.out.appendPlainText(f"=== {Path(p).name}  ({Path(p).parent.name})  diff={d or '?'} ===")
                self.out.appendPlainText("VERDICT: " + v)
                for it in issues:
                    self.out.appendPlainText("  " + str(it))
                self.out.appendPlainText("")
                if "INVALID" in v:
                    worst = ERROR
                elif "warnings" in v and worst != ERROR:
                    worst = WARN
            self.lbl.setText({OK: "✓ all valid", WARN: "⚠ warnings",
                              ERROR: "✗ invalid"}.get(worst, ""))

except ImportError:
    ShtValidatorDialog = None  # type: ignore


# ===========================================================================
# CLI
# ===========================================================================
def main():
    import sys
    from pathlib import Path
    if len(sys.argv) < 2:
        print("usage: python sht_validator.py <file.sht> [easy|normal|hard|oni]")
        return 2
    diff = sys.argv[2] if len(sys.argv) > 2 else None
    data = Path(sys.argv[1]).read_bytes()
    issues = validate_sht(data, diff)
    print("VERDICT:", verdict(issues))
    for it in issues:
        print(" ", it)
    return 1 if any(i.severity == ERROR for i in issues) else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

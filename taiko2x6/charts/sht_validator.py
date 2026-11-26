#!/usr/bin/env python3
"""
SHT validator — check a Gen2 .sht chart against the official on-disc format.

Every rule here was mined from the 712 genuine charts in the game: rules that
ALL of them satisfy are ERRORs when violated (the chart is not official and can
crash / freeze the game at load); rules only most satisfy are WARNINGs.

Usage:
    python -m taiko2x6.charts.sht_validator <file.sht> [easy|normal|hard|oni]
    from taiko2x6.charts.sht_validator import validate_sht           # -> list[Issue]
GUI: ShtValidatorDialog (also wired into the Explorer Tools menu).
"""
from __future__ import annotations

import logging
import math
import struct
from dataclasses import dataclass

from taiko2x6.charts import tja2sht
from taiko2x6.app.i18n import tr
from taiko2x6.app.dialog_polish import polish

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

    # pointGain must be the remaining-note countdown (total_one_copy - cumulative);
    # a branched chart keeps one countdown per route (slots 0-2, repeated in 3-5)
    branched = (any(list(t["bunkis"]) != [-1] * 6 for t in tracks) or
                any(t["subtracks"][si]["noteCount"] for t in tracks for si in (1, 2)))
    pg_ok = True
    for r in (range(3) if branched else (0,)):
        rem = sum(t["subtracks"][r]["noteCount"] for t in tracks)
        for t in tracks:
            rem -= t["subtracks"][r]["noteCount"]
            for si in (r, r + 3):
                if t["subtracks"][si]["pointGain"] != rem:
                    pg_ok = False
    if not branched:
        for t in tracks:
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
        if not s0["noteCount"] or s0["noteIndexSt"] == s3["noteIndexSt"]:
            continue                     # nothing to compare / the same slice
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
    positions = {n["measure"] for n in notes}
    if any(p >= 48 or p < 0 for p in positions):
        E("note.position", "some note positions are outside 0..47")
    off_grid = sorted({p for p in positions if 0 <= p < 48} - CORPUS_POSITIONS)
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

    # ---- per-difficulty note-buffer limit ----------------------------------
    if difficulty:
        limit = tja2sht.DIFFICULTY_NOTE_LIMIT.get(difficulty.lower())
        if limit is not None:
            # branched charts count by their busiest route (see
            # tja2sht.limit_note_count)
            n = tja2sht.limit_note_count({"tracks": tracks, "notes": notes})
            if n > limit:
                E("difficulty.limit",
                  f"{n} notes exceeds the {difficulty} buffer ({limit}) "
                  f"— FREEZES on this difficulty")
            else:
                I("difficulty.limit", f"{n}/{limit} notes ({difficulty}) — ok")

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
# Presentation helpers (display only — they never change what is validated)
# ===========================================================================
import re as _re

_TRACK_RE = _re.compile(r"\btrack (\d+)")


def issue_measure(issue: Issue) -> int | None:
    """0-based measure an issue points at, when its text names one."""
    m = _TRACK_RE.search(issue.detail)
    return int(m.group(1)) if m else None


def suggestion_for(issue: Issue) -> str:
    """A short, translated hint on what to do about the issue."""
    r = issue.rule
    if issue.severity == INFO:
        return ""
    if r.startswith("header"):
        return tr("The file is damaged or not a Gen2 .sht. Convert it again from the TJA.")
    if r == "parse":
        return tr("The file cannot be read as a chart. Convert it again from the TJA.")
    if r in ("track.bpm", "track.time", "track.scroll"):
        return tr("Fix the #BPMCHANGE / #SCROLL / #DELAY command near this measure in the TJA.")
    if r == "track.firstTime":
        return tr("Check OFFSET and the first #DELAY in the TJA.")
    if r == "track.timeOrder":
        return tr("Look for a negative #DELAY or a branch that rewinds the time.")
    if r.startswith("subtrack"):
        if r == "subtrack.pointGainCountdown":
            return tr("Usually harmless; the game tolerates it.")
        return tr("Rebuild the chart with the converter instead of editing the file by hand.")
    if r.startswith("note"):
        return tr("Check the notes in this measure; positions should stay on the normal 1/48 grid.")
    if r == "difficulty.limit":
        return tr("Remove notes (or branch less) until the chart fits the buffer.")
    return tr("Compare with an official chart of the same difficulty.")


def note_limit_info(data: bytes, difficulty: str | None):
    """(count, limit, busiest route index, branched) or None."""
    if not difficulty:
        return None
    limit = tja2sht.DIFFICULTY_NOTE_LIMIT.get(difficulty.lower())
    if limit is None:
        return None
    try:
        m = tja2sht.parse_sht(data)
    except Exception:
        return None
    trk = m["tracks"]
    n = tja2sht.limit_note_count(m)
    per = [sum(t["subtracks"][b]["noteCount"] for t in trk) for b in range(3)]
    branched = (any(list(t["bunkis"]) != [-1] * 6 for t in trk) or
                any(t["subtracks"][s]["noteCount"] for t in trk for s in (1, 2)))
    route = max(range(3), key=lambda b: per[b]) if branched else 0
    return n, limit, route, branched


def verdict_text(issues: list[Issue]) -> str:
    if any(i.severity == ERROR for i in issues):
        return tr("INVALID (would crash/freeze)")
    if any(i.severity == WARN for i in issues):
        return tr("VALID with warnings")
    return tr("VALID — matches official format")


_SUFFIX_DIFF = (("_e", "easy"), ("_n", "normal"), ("_h", "hard"), ("_m", "oni"))


def charts_from_path(path) -> list[tuple[str, str | None, bytes]]:
    """Read-only: [(label, difficulty guess, sht bytes)] for an .sht / .bin /
    .tja file. TJA and Gen3 .bin are converted in memory only for checking."""
    from pathlib import Path
    from taiko2x6.charts import chart_convert
    p = Path(path)
    kind = chart_convert.detect(p)
    if kind is None or kind == "audio":
        raise ValueError(tr("{f}: not a TJA, sht or Gen3 fumen").format(f=p.name))
    if kind == "sht":
        nm = p.parent.name + p.name
        diff = next((c for suf, c in _SUFFIX_DIFF if suf in nm), None)
        return [(p.name, diff, p.read_bytes())]
    piece = chart_convert.load(p)
    return [(f"{p.name} [{c}]", c, b) for c, b in piece["charts"].items()]


# ===========================================================================
# GUI
# ===========================================================================
try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QBrush
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox,
        QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView,
        QSplitter,
    )
    from taiko2x6.charts.chart_timeline import (ChartTimelineView, SEV_COLOURS,
                                                fmt_time, lane_names)

    _SEV_RANK = {ERROR: 0, WARN: 1, INFO: 2}
    _SEV_GLYPH = {ERROR: "✖", WARN: "⚠", INFO: "ℹ"}

    class _SortItem(QTableWidgetItem):
        """Sorts by a numeric key kept in UserRole instead of the text."""
        def __lt__(self, other):
            a, b = self.data(Qt.UserRole), other.data(Qt.UserRole)
            if a is not None and b is not None:
                return a < b
            return super().__lt__(other)

    class ShtValidatorDialog(QDialog):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle(tr("SHT Validator — check official format"))
            self.resize(860, 680)
            self.setAcceptDrops(True)
            self._results: list[dict] = []
            self._load_errors: list[str] = []
            lay = QVBoxLayout(self)
            row = QHBoxLayout()
            self.b_open = QPushButton(tr("Open chart…"))
            self.b_open.clicked.connect(self._open)
            row.addWidget(self.b_open)
            row.addWidget(QLabel(tr("difficulty:")))
            self.cb = QComboBox()
            self.cb.addItems([tr("(auto/none)"), "easy", "normal", "hard", "oni"])
            self.cb.currentIndexChanged.connect(self._recheck)
            row.addWidget(self.cb)
            row.addWidget(QLabel(tr("show:")))
            self.cb_sev = QComboBox()
            self.cb_sev.addItems([tr("all"), tr("errors"), tr("errors + warnings")])
            self.cb_sev.currentIndexChanged.connect(self._fill)
            row.addWidget(self.cb_sev)
            self.lbl = QLabel("")
            row.addWidget(self.lbl, 1)
            lay.addLayout(row)

            self.cb_file = QComboBox()
            self.cb_file.currentIndexChanged.connect(self._fill)
            self.cb_file.setVisible(False)
            lay.addWidget(self.cb_file)
            self.lbl_verdict = QLabel(tr("Drop .sht / .bin / .tja files here, or press Open."))
            self.lbl_verdict.setWordWrap(True)
            lay.addWidget(self.lbl_verdict)
            self.lbl_limit = QLabel("")
            lay.addWidget(self.lbl_limit)

            split = QSplitter(Qt.Vertical)
            self.table = QTableWidget(0, 5)
            self.table.setHorizontalHeaderLabels(
                [tr("Severity"), tr("Measure"), tr("Time"), tr("Problem"), tr("Suggestion")])
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.table.setSelectionMode(QAbstractItemView.SingleSelection)
            self.table.verticalHeader().setVisible(False)
            self.table.setWordWrap(True)
            self.table.setSortingEnabled(True)
            hh = self.table.horizontalHeader()
            hh.setSectionResizeMode(3, QHeaderView.Stretch)
            hh.setSectionResizeMode(4, QHeaderView.Stretch)
            self.table.cellDoubleClicked.connect(self._jump)
            self.table.itemSelectionChanged.connect(self._jump_selected)
            split.addWidget(self.table)
            self.view = ChartTimelineView()
            self.view.measureClicked.connect(self._select_measure)
            split.addWidget(self.view)
            split.setStretchFactor(0, 3)
            split.setStretchFactor(1, 2)
            lay.addWidget(split, 1)
            b_close = QPushButton(tr("Close"))
            b_close.clicked.connect(self.accept)
            lay.addWidget(b_close)
            polish(self, primary=self.b_open)

        # -------------------------------------------------- drag and drop
        def dragEnterEvent(self, e):
            if e.mimeData().hasUrls():
                e.acceptProposedAction()

        def dropEvent(self, e):
            paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
            if paths:
                self.load_paths(paths)
                e.acceptProposedAction()

        # ----------------------------------------------------- loading
        def _open(self):
            from taiko2x6.app import appconfig                       # last-used-path memory
            paths = appconfig.pick_open_many(
                self, "sht", tr("Chart file(s)"),
                tr("Charts (*.sht sht *.bin *.tja);;All (*)"))
            if paths:
                self.load_paths(paths)

        def _chosen_difficulty(self):
            return None if self.cb.currentIndex() == 0 else self.cb.currentText()

        def load_paths(self, paths):
            """Check files (read-only; TJA / Gen3 are converted in memory)."""
            self._results = []
            self._load_errors = []
            for p in paths:
                try:
                    for label, guess, data in charts_from_path(p):
                        self._results.append({"label": label, "guess": guess,
                                              "data": data})
                except Exception as exc:
                    self._load_errors.append(f"{p}: {exc}")
            for r in self._results:
                self._check(r)
            self.cb_file.blockSignals(True)
            self.cb_file.clear()
            self.cb_file.addItems([r["label"] for r in self._results])
            self.cb_file.blockSignals(False)
            self.cb_file.setVisible(len(self._results) > 1)
            self._fill()

        def _check(self, r):
            diff = self._chosen_difficulty() or r["guess"]
            r["diff"] = diff
            r["issues"] = validate_sht(r["data"], diff)
            try:
                r["model"] = tja2sht.parse_sht(r["data"])
            except Exception:
                r["model"] = None

        def _recheck(self):
            for r in self._results:
                self._check(r)
            self._fill()

        # ------------------------------------------------------ display
        def _current(self):
            i = self.cb_file.currentIndex()
            return self._results[i] if 0 <= i < len(self._results) else None

        def _summary_label(self):
            if not self._results:
                return ""
            worst = OK
            for r in self._results:
                v = verdict(r["issues"])
                if "INVALID" in v:
                    worst = ERROR
                    break
                if "warnings" in v:
                    worst = WARN
            return {OK: tr("✓ all valid"), WARN: tr("⚠ warnings"),
                    ERROR: tr("✗ invalid")}[worst]

        def _fill(self, *_):
            r = self._current()
            self.table.setSortingEnabled(False)
            self.table.setRowCount(0)
            self.lbl.setText(self._summary_label())
            if r is None:
                if self._load_errors:
                    self.lbl_verdict.setText("\n".join(self._load_errors))
                self.lbl_limit.setText("")
                self.view.set_model(None)
                self.table.setSortingEnabled(True)
                return
            self.lbl_verdict.setText(
                tr("{f}   diff={d}   VERDICT: {v}").format(
                    f=r["label"], d=r["diff"] or "?", v=verdict_text(r["issues"])))
            info = note_limit_info(r["data"], r["diff"])
            if info is None:
                self.lbl_limit.setText(tr("Note limit: choose a difficulty to check it."))
            else:
                n, limit, route, branched = info
                txt = tr("Note limit: {n} / {limit}").format(n=n, limit=limit)
                if branched:
                    txt += "  " + tr("(busiest route: {r})").format(
                        r=lane_names(True)[route])
                self.lbl_limit.setText(txt)
            model = r["model"]
            self.view.set_model(model)
            keep = {0: {ERROR, WARN, INFO}, 1: {ERROR},
                    2: {ERROR, WARN}}[self.cb_sev.currentIndex()]
            marks: dict[int, str] = {}
            rows = [it for it in r["issues"] if it.severity in keep]
            self.table.setRowCount(len(rows))
            for row, it in enumerate(rows):
                meas = issue_measure(it)
                if model is not None and meas is not None and not 0 <= meas < len(model["tracks"]):
                    meas = None
                if model is None:
                    meas = None
                if meas is not None and (meas not in marks
                                         or _SEV_RANK[it.severity] < _SEV_RANK[marks[meas]]):
                    marks[meas] = it.severity
                sev = _SortItem(f"{_SEV_GLYPH.get(it.severity, '')} {it.severity}")
                sev.setData(Qt.UserRole, _SEV_RANK.get(it.severity, 9))
                if it.severity in SEV_COLOURS:
                    sev.setForeground(QBrush(SEV_COLOURS[it.severity]))
                m_item = _SortItem(str(meas + 1) if meas is not None else "—")
                m_item.setData(Qt.UserRole, meas if meas is not None else -1)
                m_item.setData(Qt.UserRole + 1, meas)
                ms = model["tracks"][meas]["time"] if meas is not None else None
                t_item = _SortItem(fmt_time(ms) if ms is not None else "—")
                t_item.setData(Qt.UserRole, ms if ms is not None else -1.0)
                cells = [sev, m_item, t_item,
                         QTableWidgetItem(f"{it.rule}: {it.detail}"),
                         QTableWidgetItem(suggestion_for(it))]
                for c, item in enumerate(cells):
                    self.table.setItem(row, c, item)
            self.view.set_markers(marks)
            self.table.setSortingEnabled(True)
            self.table.sortByColumn(0, Qt.AscendingOrder)
            self.table.resizeRowsToContents()

        # ------------------------------------------------- navigation
        def _row_measure(self, row):
            it = self.table.item(row, 1)
            return it.data(Qt.UserRole + 1) if it is not None else None

        def _jump(self, row, _col=0):
            m = self._row_measure(row)
            if m is not None:
                self.view.show_measure(m)

        def _jump_selected(self):
            rows = self.table.selectionModel().selectedRows()
            if rows:
                self._jump(rows[0].row())

        def _select_measure(self, i):
            """Clicking a measure selects the first issue that names it."""
            for row in range(self.table.rowCount()):
                if self._row_measure(row) == i:
                    self.table.selectRow(row)
                    return

except ImportError:
    ShtValidatorDialog = None  # type: ignore


# ===========================================================================
# CLI
# ===========================================================================
def main():
    import sys
    from pathlib import Path
    if len(sys.argv) < 2:
        print("usage: python -m taiko2x6.charts.sht_validator <file.sht> [easy|normal|hard|oni]")
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

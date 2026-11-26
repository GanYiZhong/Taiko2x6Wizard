"""One chart converter: TJA, Gen2 .sht and Gen3 (Nijiiro) fumen .bin, any
direction, plus Gen3 song_<id>.nus3bank audio -> game VAG.

Every input is first read into Gen2 sht charts, then written out in the
chosen formats:

    TJA          -> tja2sht.convert_tja            (all three branch routes)
    Gen3 .bin    -> gen3_convert.gen3_to_sht       (branches kept)
    .sht         -> as is
    sht          -> sht_export.sht_to_tja / sht_to_gen3_bin

Inputs are grouped into songs by id (the TJA file name, or the id in
`<id>_m.bin` / `<id>1p_m/sht`), so the four difficulties of a song come out
as one TJA. Going through sht snaps notes onto its 1/48-bar grid, which is
lossless for shipped charts and rounds finer TJA tuplets.
"""
from __future__ import annotations

import pathlib
import re

from taiko2x6.charts import gen3_convert, sht_export, tja2sht

COURSES = ("easy", "normal", "hard", "oni", "ura")
LETTER = {"easy": "e", "normal": "n", "hard": "h", "oni": "m", "ura": "x"}
_BY_LETTER = {v: k for k, v in LETTER.items()}
_TJA_IDX = {0: "easy", 1: "normal", 2: "hard", 3: "oni", 4: "ura"}
_STAR_KEY = {"easy": "starEasy", "normal": "starNormal", "hard": "starHard",
             "oni": "starMania", "ura": "starUra"}


def read_text(path):
    raw = pathlib.Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "shift_jis", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


# Nijiiro double-play charts (<id>_m_1.bin / _m_2.bin): not converted
_DUET = re.compile(r"_[enhmx]_[12]$", re.I)


def detect(path):
    """'tja', 'sht', 'bin' (Gen3 fumen), 'audio' (nus3bank) or None."""
    p = pathlib.Path(path)
    suf = p.suffix.lower()
    if suf == ".bin" and _DUET.search(p.stem):
        return None
    if suf == ".tja":
        return "tja"
    if suf == ".nus3bank":
        return "audio"
    if p.name.lower() == "sht" or suf == ".sht":
        return "sht"
    if suf == ".bin":
        try:
            gen3_convert.parse_gen3(gen3_convert.decrypt_fumen(p))
            return "bin"
        except ImportError:
            raise                   # no AES library: say so, not "unknown"
        except Exception:
            pass
        try:
            tja2sht.parse_sht(p.read_bytes())
            return "sht"
        except Exception:
            return None
    return None


def _course_of(name):
    return sht_export.course_from_name(name)


def _gen3_stars(path, sid, course):
    """Star level from the Nijiiro musicinfo next to a fumen, if present."""
    try:
        root = pathlib.Path(path).resolve().parent.parent.parent
        if not (root / "datatable" / "musicinfo.bin").is_file():
            return None
        from taiko2x6.charts import gen3_song
        info = gen3_song.music_info(root, sid) or {}
        return info.get(_STAR_KEY[course]) or None
    except Exception:
        return None


def _branch_line(text, score):
    """1-based line of the first #BRANCHSTART (of a score condition when
    `score`), counted the way parse_tja splits lines; None if absent."""
    for n, raw in enumerate(
            text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        parts = raw.split("//", 1)[0].strip().split(None, 1)
        if not parts or parts[0].upper() != "#BRANCHSTART":
            continue
        if not score or (len(parts) > 1
                         and parts[1].strip().lower().startswith("s")):
            return n
    return None


def _note(piece, text, course, w):
    piece["warnings"].append("%s: %s" % (course, w))
    piece["warn_items"].append(
        (course, w, _branch_line(text, w.startswith("score branch"))))


def load(path):
    """Read one input. Returns a song piece:
    {"id", "kind", "title", "wave", "charts": {course: sht}, "levels": {},
     "audio": path or None, "warnings": [],
     "warn_items": [(course, message, tja_line or None)]}."""
    p = pathlib.Path(path)
    kind = detect(p)
    if kind is None:
        raise ValueError("%s: not a TJA, sht, Gen3 fumen or nus3bank" % p.name)
    piece = {"id": None, "kind": kind, "title": "", "wave": "", "charts": {},
             "levels": {}, "audio": None, "warnings": [],
             "warn_items": []}
    if kind == "tja":
        text = read_text(p)
        parsed = tja2sht.parse_tja(text)
        piece["id"] = p.stem
        piece["title"] = parsed["meta"].get("TITLE", "") or p.stem
        piece["wave"] = parsed["meta"].get("WAVE", "")
        for idx, cdata in sorted(parsed["courses"].items()):
            course = _TJA_IDX.get(idx)
            if course is None or not cdata.get("measures"):
                continue
            piece["charts"][course] = tja2sht.convert_tja(
                text, course, warn=lambda w, c=course: _note(piece, text, c, w))
            lv = cdata["meta"].get("LEVEL")
            try:
                if lv:
                    piece["levels"][course] = int(float(lv))
            except (ValueError, OverflowError):
                pass
        if not piece["charts"]:
            raise ValueError("%s: no charts (no #START ... #END with notes)"
                             % p.name)
        return piece
    if kind == "audio":
        name = p.stem
        piece["id"] = name[5:] if name.lower().startswith("song_") else name
        piece["audio"] = str(p)
        return piece
    piece["id"] = sht_export.song_id_of(p)
    course = _course_of(p)
    if course is None:
        raise ValueError("%s: cannot tell the difficulty from the name "
                         "(expected …_e/_n/_h/_m/_x)" % p.name)
    if kind == "sht":
        piece["charts"][course] = p.read_bytes()
        tja2sht.parse_sht(piece["charts"][course])
    else:
        meas = gen3_convert.parse_gen3(gen3_convert.decrypt_fumen(p))
        piece["charts"][course] = gen3_convert.gen3_to_sht(meas)
        st = _gen3_stars(p, piece["id"], course)
        if st:
            piece["levels"][course] = int(st)
    return piece


def group(pieces, out_id=None, warn=None):
    """Merge pieces with the same id into songs (first title/wave wins).
    With `out_id` every piece belongs to that one song -- e.g. a TJA and the
    game's song_<id>.nus3bank. A course given twice keeps the first one."""
    songs = {}
    for pc in pieces:
        sid = out_id or pc["id"]
        s = songs.setdefault(sid, {"id": sid, "title": "", "wave": "",
                                   "charts": {}, "levels": {},
                                   "audio": None, "sources": set()})
        s["title"] = s["title"] or pc["title"]
        s["wave"] = s["wave"] or pc["wave"]
        for course, sht in pc["charts"].items():
            if course in s["charts"]:
                if warn:
                    warn("%s: %s given twice; kept the first" % (sid, course))
                continue
            s["charts"][course] = sht
            if course in pc["levels"]:
                s["levels"][course] = pc["levels"][course]
        s["audio"] = s["audio"] or pc["audio"]
        s["sources"].add(pc["kind"])
    return list(songs.values())


def _route_notes(sht):
    """Notes a player meets on each route ([n] for an unbranched chart)."""
    m = tja2sht.parse_sht(sht)
    _div, rts = sht_export.routes(m)
    if not sht_export.is_branched(m):
        return [sum(len(r[0]) for r in rts)]
    return [sum(len(r[b]) for r in rts) for b in range(3)]


def write(song, out_dir, formats, players=("1p",), out_id=None, log=print,
          written=None, errors=None, protect=()):
    """Write a song in each of `formats` ('sht', 'tja', 'bin', 'vag').
    Returns the written paths (also appended to `written` as they land, so
    a later failure does not hide what was already written). Each file is
    tried on its own: a failure goes to `errors` (raised when None) and the
    rest still get written. Paths in `protect` (the inputs) are never
    overwritten."""
    out = pathlib.Path(out_dir)
    sid = out_id or song["id"]
    written = [] if written is None else written
    raise_errors = errors is None
    errors = [] if errors is None else errors
    keep = {pathlib.Path(x).resolve() for x in protect}
    charts = song["charts"]

    def put(dst, make, what):
        try:
            if dst.resolve() in keep:
                raise ValueError("would overwrite the input %s; pick another "
                                 "output folder" % dst)
            data = make()
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            written.append(dst)
            return True
        except Exception as e:
            if raise_errors:
                raise
            errors.append("%s %s: %s" % (sid, what, e))
            log("FAILED %s" % errors[-1])
            return False

    if "sht" in formats:
        for course, sht in charts.items():
            ok = all([put(out / ("%s%s_%s" % (sid, pl, LETTER[course])) / "sht",
                          lambda: sht, "sht " + course) for pl in players])
            if not ok:
                continue
            per = _route_notes(sht)
            limit = tja2sht.DIFFICULTY_NOTE_LIMIT.get(course)
            note = ""
            if limit is not None and tja2sht.limit_note_count(
                    tja2sht.parse_sht(sht)) > limit:
                note = " — over the retail %s max (%d); check in game" % (
                    course, limit)
            log("sht  %s %s: %s notes%s" % (
                sid, course, "/".join(map(str, per)), note))
    if "tja" in formats and charts:
        dst = out / (sid + ".tja")
        if put(dst, lambda: sht_export.sht_to_tja(
                charts, title=song["title"] or sid,
                wave=song["wave"] or (sid + ".ogg"),
                levels=song["levels"]).encode("utf-8-sig"), "tja"):
            log("tja  %s: %s" % (sid, dst))
    if "bin" in formats:
        for course, sht in charts.items():
            dst = out / sid / ("%s_%s.bin" % (sid, LETTER[course]))
            if put(dst, lambda: sht_export.sht_to_gen3_bin(
                    sht, course=course, stars=song["levels"].get(course)),
                    "bin " + course):
                log("bin  %s %s: %s" % (sid, course, dst))
    if "vag" in formats and song.get("audio"):
        dst = out / (sid + ".vag")
        if put(dst, lambda: gen3_convert.convert_audio(song["audio"]), "vag"):
            log("vag  %s: %s" % (sid, dst))
    if raise_errors and errors:
        raise RuntimeError("; ".join(errors))
    return written


def _report_rows(pieces, out_id, outputs, song_errors, dups, failed):
    """One row per input file: status, outputs of its song, courses, notes."""
    rows = list(failed)
    for pc in pieces:
        sid = out_id or pc["id"]
        outs = [str(x) for x in outputs.get(sid, [])]
        msgs = [{"level": "warning", "text": "%s: %s" % (c, m), "line": ln}
                for c, m, ln in pc["warn_items"]]
        msgs += [{"level": "warning", "text": d, "line": None}
                 for d in dups if d.startswith(sid + ":")]
        errs = song_errors.get(sid, [])
        if sid not in outputs:                  # cancelled before this song
            msgs.append({"level": "warning", "line": None,
                         "text": "not converted (cancelled)"})
        msgs += [{"level": "error", "text": e, "line": None} for e in errs]
        if errs and not outs:
            status = "error"
        elif errs or any(m["level"] == "warning" for m in msgs):
            status = "warning"
        else:
            status = "ok"
        rows.append({"input": pc["path"], "status": status, "outputs": outs,
                     "courses": [c for c in COURSES if c in pc["charts"]],
                     "messages": msgs})
    return rows


def convert(paths, out_dir, formats, players=("1p",), out_id=None, log=print,
            cancel=None, report=None):
    """Load, group and write. Returns (songs, written paths, errors).
    `out_id` makes every input one song with that id.

    `cancel()` is polled between input files and between songs (never inside
    a song's write), so a cancel leaves only whole files behind: the songs
    already written stay, the rest are not started.

    `report`, when a list, receives one dict per input file: {"input",
    "status" ok/warning/error, "outputs", "courses", "messages": [{"level",
    "text", "line"}]} -- the data behind the results table."""
    pieces, errors, failed = [], [], []
    for p in paths:
        if cancel is not None and cancel():
            log("cancelled")
            if report is not None:
                report.extend(failed)
            return [], [], errors
        try:
            pc = load(p)
        except Exception as e:
            errors.append("%s: %s" % (pathlib.Path(p).name, e))
            log("skip %s" % errors[-1])
            failed.append({"input": str(p), "status": "error", "outputs": [],
                           "courses": [], "messages": [
                               {"level": "error", "text": str(e), "line": None}]})
            continue
        pc["path"] = str(p)
        for w in pc["warnings"]:
            log("note %s: %s" % (pathlib.Path(p).name, w))
        pieces.append(pc)
    dups = []
    songs = group(pieces, out_id=out_id,
                  warn=lambda w: (dups.append(w), log("note " + w)))
    written = []
    outputs, song_errors = {}, {}
    for s in songs:
        if cancel is not None and cancel():
            log("cancelled before %s" % s["id"])
            break
        n0, e0 = len(written), len(errors)
        write(s, out_dir, formats, players=players, log=log,
              written=written, errors=errors, protect=paths)
        outputs[s["id"]] = written[n0:]
        if len(written) == n0 and len(errors) == e0:
            errors.append("%s: nothing to write as %s (%s)" % (
                s["id"], "/".join(sorted(formats)),
                "no charts" if not s["charts"] else "no nus3bank audio"))
            log("FAILED %s" % errors[-1])
        song_errors[s["id"]] = errors[e0:]
    if report is not None:
        report.extend(_report_rows(pieces, out_id, outputs, song_errors,
                                   dups, failed))
    return songs, written, errors


# --- GUI -------------------------------------------------------------------

try:
    from PySide6.QtCore import QThread, Signal
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
        QPlainTextEdit, QFileDialog, QMessageBox, QListWidget, QCheckBox,
        QGroupBox, QAbstractItemView, QTableWidget, QTableWidgetItem,
        QComboBox, QToolButton, QApplication, QStyle, QHeaderView)
    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtGui import QDesktopServices
except ImportError:                                            # headless / CLI
    QDialog = None


if QDialog is not None:
    from taiko2x6.app.i18n import tr
    from taiko2x6.app.dialog_polish import polish
    from taiko2x6.app import jobs

    class _ConvertWorker(QThread):
        """Runs convert() off the UI thread (vgmstream and decrypting many
        fumens take a while); log lines come back through a signal."""
        line = Signal(str)
        done = Signal(object)

        def __init__(self, kwargs, parent=None):
            super().__init__(parent)
            self.kwargs = kwargs
            self._stop = False

        def cancel(self):
            self._stop = True

        def run(self):
            try:
                self.done.emit(convert(log=self.line.emit,
                                       cancel=lambda: self._stop, **self.kwargs))
            except Exception as e:                  # report, never vanish
                self.done.emit(e)

    def _quick_kind(p):
        """Cheap filter for folder drops: by name only (load() checks the
        contents). Gen3 fumens must look like <id>_<e/n/h/m/x>.bin, so a
        datatable/*.bin or a duet <id>_m_1.bin isn't picked up."""
        suf, name = p.suffix.lower(), p.name.lower()
        if suf in (".tja", ".sht", ".nus3bank") or name == "sht":
            return True
        return suf == ".bin" and sht_export.course_from_name(p) is not None

    class ChartConverterDialog(QDialog):
        """Drop in TJA / sht / Gen3 .bin / nus3bank files, tick the formats
        to write, convert."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle(tr("Chart converter (TJA / sht / Gen3 .bin)"))
            self.resize(780, 600)
            self.setAcceptDrops(True)
            v = QVBoxLayout(self)
            lab = QLabel(tr(
                "Add TJA, .sht or Gen3 (Nijiiro) fumen .bin files — or drop "
                "them here — and pick what to write. Files of the same song "
                "(same id) are converted together, so a song's difficulties "
                "become one TJA. A Gen3 song_<id>.nus3bank becomes a game VAG "
                "(needs vgmstream). Branches are kept in every direction."))
            lab.setWordWrap(True)
            v.addWidget(lab)

            self.lst = QListWidget()
            self.lst.setSelectionMode(QAbstractItemView.ExtendedSelection)
            self.lst.setToolTip(tr(
                "Queued files. Select and press Delete to remove."))
            v.addWidget(self.lst, 1)
            row = QHBoxLayout()
            b = QPushButton(tr("Add files…"))
            b.setAutoDefault(False)
            b.clicked.connect(self._add)
            row.addWidget(b)
            self.b_rm = QPushButton(tr("Remove"))
            self.b_rm.setAutoDefault(False)
            self.b_rm.clicked.connect(self._remove)
            row.addWidget(self.b_rm)
            self.b_clear = QPushButton(tr("Clear"))
            self.b_clear.setAutoDefault(False)
            self.b_clear.clicked.connect(self.lst.clear)
            row.addWidget(self.b_clear)
            self.b_check = QPushButton(tr("Check TJA…"))
            self.b_check.setAutoDefault(False)
            self.b_check.setToolTip(tr(
                "Look for common mistakes in the queued TJA files (or the "
                "selected ones) and save a fixed copy."))
            self.b_check.clicked.connect(self._check_tja)
            row.addWidget(self.b_check)
            self.lb_count = QLabel()
            row.addWidget(self.lb_count)
            row.addStretch(1)
            v.addLayout(row)

            box = QGroupBox(tr("Write"))
            h = QHBoxLayout(box)
            self.ck = {}
            for key, label in (("sht", tr(".sht (PS2 arcade)")),
                               ("tja", "TJA"),
                               ("bin", tr("Gen3 fumen .bin (Nijiiro)")),
                               ("vag", tr("VAG audio (from nus3bank)"))):
                c = QCheckBox(label)
                self.ck[key] = c
                h.addWidget(c)
            self.ck["tja"].setChecked(True)
            self.ck["vag"].setChecked(True)
            h.addStretch(1)
            v.addWidget(box)

            row = QHBoxLayout()
            self.ck_1p = QCheckBox("1P")
            self.ck_1p.setChecked(True)
            self.ck_2p = QCheckBox("2P")
            self.ck_2p.setChecked(True)
            row.addWidget(QLabel(tr(".sht players:")))
            row.addWidget(self.ck_1p)
            row.addWidget(self.ck_2p)
            row.addSpacing(24)
            row.addWidget(QLabel(tr("Song ID:")))
            self.ed_id = QLineEdit()
            self.ed_id.setPlaceholderText(tr("from the file name"))
            self.ed_id.setToolTip(tr(
                "Output name when converting one song, e.g. the game's id "
                "(10tai). Left empty, each song keeps its own id."))
            row.addWidget(self.ed_id, 1)
            v.addLayout(row)

            row = QHBoxLayout()
            row.addWidget(QLabel(tr("Output folder:")))
            self.ed_out = QLineEdit()
            row.addWidget(self.ed_out, 1)
            b = QPushButton(tr("Browse…"))
            b.setAutoDefault(False)
            b.clicked.connect(self._browse_out)
            row.addWidget(b)
            self.b_open = QPushButton(tr("Open output folder"))
            self.b_open.setAutoDefault(False)
            self.b_open.setToolTip(tr(
                "Show the output folder in the file manager."))
            self.b_open.clicked.connect(self._open_out)
            row.addWidget(self.b_open)
            v.addLayout(row)

            # results table (filled after a run) + the raw log under Details
            self.lb_summary = QLabel()
            v.addWidget(self.lb_summary)
            row = QHBoxLayout()
            self.lb_filter = QLabel(tr("Show:"))
            row.addWidget(self.lb_filter)
            self.cb_filter = QComboBox()
            for key, label in (("all", tr("All")), ("ok", tr("OK")),
                               ("warning", tr("Warnings")),
                               ("error", tr("Errors"))):
                self.cb_filter.addItem(label, key)
            self.cb_filter.currentIndexChanged.connect(self._apply_filter)
            row.addWidget(self.cb_filter)
            self.b_copy = QPushButton(tr("Copy report"))
            self.b_copy.setAutoDefault(False)
            self.b_copy.setToolTip(tr(
                "Copy the shown rows as text (for a bug report or notes)."))
            self.b_copy.clicked.connect(self._copy_report)
            row.addWidget(self.b_copy)
            self.b_open2 = QPushButton(tr("Open output folder"))
            self.b_open2.setAutoDefault(False)
            self.b_open2.clicked.connect(self._open_out)
            row.addWidget(self.b_open2)
            row.addStretch(1)
            v.addLayout(row)
            self.tbl = QTableWidget(0, 5)
            self.tbl.setHorizontalHeaderLabels([
                tr("Status"), tr("Input"), tr("Output"), tr("Courses"),
                tr("Message")])
            self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.tbl.verticalHeader().setVisible(False)
            hh = self.tbl.horizontalHeader()
            hh.setStretchLastSection(True)
            hh.setSectionResizeMode(1, QHeaderView.Interactive)
            self.tbl.setColumnWidth(1, 180)
            self.tbl.setColumnWidth(2, 200)
            self.tbl.setToolTip(tr(
                "Double-click a row to open the input file; click an output "
                "to show it in the file manager."))
            self.tbl.cellDoubleClicked.connect(self._row_open)
            self.tbl.cellClicked.connect(self._cell_click)
            v.addWidget(self.tbl, 1)
            self.b_details = QToolButton()
            self.b_details.setText(tr("Details"))
            self.b_details.setCheckable(True)
            self.b_details.setArrowType(Qt.RightArrow)
            self.b_details.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            self.b_details.toggled.connect(self._toggle_details)
            v.addWidget(self.b_details)
            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setVisible(False)
            v.addWidget(self.log, 1)
            self._rows = []
            self._report = []
            self._show_results_widgets(False)

            row = QHBoxLayout()
            row.addStretch(1)
            self.b_go = QPushButton(tr("Convert"))
            self.b_go.setDefault(True)
            self.b_go.clicked.connect(self._run)
            self.lb_elapsed = jobs.ElapsedLabel()
            row.addWidget(self.lb_elapsed)
            self.b_cancel = QPushButton(tr("Cancel"))
            self.b_cancel.setAutoDefault(False)
            self.b_cancel.setEnabled(False)
            self.b_cancel.setToolTip(tr(
                "Stops after the song being written; finished files stay."))
            self.b_cancel.clicked.connect(self._cancel)
            row.addWidget(self.b_cancel)
            row.addWidget(self.b_go)
            close = QPushButton(tr("Close"))
            close.setAutoDefault(False)
            close.clicked.connect(self.reject)
            row.addWidget(close)
            v.addLayout(row)

            self._drop_style = "QListWidget { border: 2px dashed palette(highlight); }"
            self._load_settings()
            self.lst.model().rowsInserted.connect(self._sync)
            self.lst.model().rowsRemoved.connect(self._sync)
            self.lst.model().modelReset.connect(self._sync)
            self.ed_out.textChanged.connect(self._sync)
            for c in self.ck.values():
                c.toggled.connect(self._sync)
            self._sync()
            polish(self)

        # -- settings -------------------------------------------------------
        def _load_settings(self):
            from taiko2x6.app import appconfig
            g = lambda k: appconfig.get("chart_convert", k)
            self.ed_out.setText(g("out_dir"))
            fm = g("formats")
            if fm:
                have = set(fm.split(","))
                for k, c in self.ck.items():
                    c.setChecked(k in have)
            pl = g("players")
            if pl:
                self.ck_1p.setChecked("1p" in pl.split(","))
                self.ck_2p.setChecked("2p" in pl.split(","))

        def _save_settings(self, out, formats, players):
            from taiko2x6.app import appconfig
            appconfig.set_value("chart_convert", "out_dir", out)
            appconfig.set_value("chart_convert", "formats",
                                ",".join(sorted(formats)))
            appconfig.set_value("chart_convert", "players", ",".join(players))

        def _sync(self, *_):
            n = self.lst.count()
            self.lb_count.setText(tr("{n} file(s) queued").format(n=n)
                                  if n else "")
            w = getattr(self, "_worker", None)
            busy = w is not None and w.isRunning()
            ready = (n > 0 and bool(self.ed_out.text().strip())
                     and any(c.isChecked() for c in self.ck.values()))
            self.b_go.setEnabled(ready and not busy)
            self.b_cancel.setEnabled(busy and not w._stop)
            self.b_go.setToolTip("" if ready else tr(
                "Add files, tick a format and pick an output folder first."))
            self.b_clear.setEnabled(n > 0)
            self.b_rm.setEnabled(n > 0)
            self.b_open.setEnabled(bool(self.ed_out.text().strip()))
            self.b_open2.setEnabled(self.b_open.isEnabled())

        def keyPressEvent(self, e):
            if e.key() == Qt.Key_Delete and self.lst.hasFocus():
                self._remove()
                return
            super().keyPressEvent(e)

        def _open_out(self):
            d = pathlib.Path(self.ed_out.text().strip())
            if not d.is_dir():
                QMessageBox.information(self, self.windowTitle(), tr(
                    "The output folder does not exist yet."))
                return
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

        # -- inputs ---------------------------------------------------------
        def add_paths(self, paths):
            have = {self.lst.item(i).text() for i in range(self.lst.count())}
            for p in paths:
                p = str(pathlib.Path(p))
                if p not in have:
                    self.lst.addItem(p)
                    have.add(p)
            if self.lst.count() and not self.ed_out.text():
                first = pathlib.Path(self.lst.item(0).text())
                base = first.parent.parent if first.name.lower() == "sht" \
                    else first.parent
                self.ed_out.setText(str(base / "converted"))

        def _add(self):
            from taiko2x6.app import appconfig
            ps = appconfig.pick_open_many(
                self, "chart_convert_in", tr("Add charts"),
                tr("Charts and audio (*.tja *.sht *.bin sht *.nus3bank);;"
                   "All files (*)"))
            self.add_paths(ps)

        def _check_tja(self):
            items = self.lst.selectedItems() or [
                self.lst.item(i) for i in range(self.lst.count())]
            paths = [it.text() for it in items
                     if it.text().lower().endswith(".tja")]
            from taiko2x6.charts.tja_check_dialog import TjaCheckDialog
            dlg = TjaCheckDialog(paths, self)
            dlg.exec()
            dlg.deleteLater()

        def _remove(self):
            for it in self.lst.selectedItems():
                self.lst.takeItem(self.lst.row(it))

        def dragEnterEvent(self, e):
            if e.mimeData().hasUrls():
                self.lst.setStyleSheet(self._drop_style)
                e.acceptProposedAction()

        def dragLeaveEvent(self, e):
            self.lst.setStyleSheet("")

        def dropEvent(self, e):
            self.lst.setStyleSheet("")
            before = self.lst.count()
            paths = []
            for u in e.mimeData().urls():
                p = pathlib.Path(u.toLocalFile())
                if p.is_dir():
                    paths += [str(x) for x in sorted(p.rglob("*"))
                              if x.is_file() and _quick_kind(x)]
                elif p.is_file():
                    paths.append(str(p))
            self.add_paths(paths)
            n = self.lst.count() - before
            self._say(tr("Added {n} file(s) from the drop.").format(n=n))

        def _browse_out(self):
            p = QFileDialog.getExistingDirectory(self, tr("Output folder"),
                                                 self.ed_out.text())
            if p:
                self.ed_out.setText(p)

        # -- run ------------------------------------------------------------
        def _run(self):
            paths = [self.lst.item(i).text() for i in range(self.lst.count())]
            formats = {k for k, c in self.ck.items() if c.isChecked()}
            out = self.ed_out.text().strip()
            if not paths:
                QMessageBox.warning(self, self.windowTitle(),
                                    tr("Add at least one file."))
                return
            if not formats or (formats == {"vag"} and not any(
                    p.lower().endswith(".nus3bank") for p in paths)):
                QMessageBox.warning(self, self.windowTitle(),
                                    tr("Tick at least one format to write."))
                return
            if not out:
                QMessageBox.warning(self, self.windowTitle(),
                                    tr("Pick an output folder."))
                return
            players = tuple(p for p, c in (("1p", self.ck_1p), ("2p", self.ck_2p))
                            if c.isChecked()) or ("1p",)
            self._save_settings(out, formats, players)
            self.log.clear()
            self.set_results([])
            self.lb_summary.setText(tr("Converting…"))
            self.lb_summary.setVisible(True)
            self._report = []
            self._worker = _ConvertWorker(dict(
                paths=paths, out_dir=out, formats=formats, players=players,
                out_id=self.ed_id.text().strip() or None,
                report=self._report), self)
            self._worker.line.connect(self._say)
            self._worker.done.connect(self._finished)
            self._worker.start()
            self.lb_elapsed.start()
            self._sync()

        def _cancel(self):
            if self._worker is not None and self._worker.isRunning():
                self._worker.cancel()
                self._say(tr("Cancelling after the current song…"))
                self._sync()

        def _finished(self, res):
            self._worker.wait()
            self.lb_elapsed.stop()
            self._sync()
            if isinstance(res, Exception):
                self._say("FAILED %r" % res)
                self.lb_summary.setText(tr("Conversion failed."))
                QMessageBox.critical(self, self.windowTitle(), str(res))
                return
            songs, written, errors = res
            self.set_results(self._report)
            if self._worker._stop:
                self._say(tr("Cancelled. Files already written were kept."))
            self._say(tr("done: {s} songs, {f} files written, {e} problems")
                      .format(s=len(songs), f=len(written), e=len(errors)))
            for er in errors:
                self._say("  ! %s" % er)

        def reject(self):
            w = getattr(self, "_worker", None)
            if w is not None and w.isRunning():
                QMessageBox.information(self, self.windowTitle(), tr(
                    "Still converting; close the window when it is done."))
                return
            super().reject()

        def _say(self, s):
            self.log.appendPlainText(str(s))

        # -- results table --------------------------------------------------
        _STATUS = {"ok": QStyle.SP_DialogApplyButton,
                   "warning": QStyle.SP_MessageBoxWarning,
                   "error": QStyle.SP_MessageBoxCritical}

        def _status_text(self, st):
            return {"ok": tr("OK"), "warning": tr("Warning"),
                    "error": tr("Error")}[st]

        def _show_results_widgets(self, on):
            for w in (self.lb_filter, self.cb_filter, self.b_copy,
                      self.b_open2, self.tbl):
                w.setVisible(on)
            self.lb_summary.setVisible(on)

        def _toggle_details(self, on):
            self.log.setVisible(on)
            self.b_details.setArrowType(Qt.DownArrow if on else Qt.RightArrow)

        @staticmethod
        def _message_text(m):
            if m.get("line"):
                return tr("line {n}: {msg}").format(n=m["line"], msg=m["text"])
            return m["text"]

        def set_results(self, rows):
            """Fill the table from convert(report=...) rows."""
            self._rows = list(rows)
            self.tbl.setRowCount(0)
            self.cb_filter.blockSignals(True)
            self.cb_filter.setCurrentIndex(0)
            self.cb_filter.blockSignals(False)
            style = self.style()
            for r in self._rows:
                i = self.tbl.rowCount()
                self.tbl.insertRow(i)
                it = QTableWidgetItem(style.standardIcon(self._STATUS[r["status"]]),
                                      self._status_text(r["status"]))
                it.setData(Qt.UserRole, r["status"])
                self.tbl.setItem(i, 0, it)
                name = pathlib.Path(r["input"])
                it = QTableWidgetItem(name.name)
                lines = [m["line"] for m in r["messages"] if m.get("line")]
                tip = r["input"]
                if lines:
                    tip += "\n" + tr("TJA line {n}").format(n=lines[0])
                it.setToolTip(tip)
                it.setData(Qt.UserRole, r["input"])
                self.tbl.setItem(i, 1, it)
                outs = r["outputs"]
                it = QTableWidgetItem(
                    ", ".join(pathlib.Path(o).name for o in outs))
                it.setToolTip("\n".join(outs))
                it.setData(Qt.UserRole, outs)
                self.tbl.setItem(i, 2, it)
                self.tbl.setItem(i, 3, QTableWidgetItem(
                    ", ".join(r["courses"])))
                msgs = [self._message_text(m) for m in r["messages"]]
                it = QTableWidgetItem("; ".join(msgs))
                it.setToolTip("\n".join(msgs))
                self.tbl.setItem(i, 4, it)
            counts = {k: sum(1 for r in self._rows if r["status"] == k)
                      for k in self._STATUS}
            self.lb_summary.setText(tr(
                "{ok} ok, {w} with warnings, {e} failed").format(
                    ok=counts["ok"], w=counts["warning"], e=counts["error"]))
            self._show_results_widgets(bool(self._rows))
            self._apply_filter()

        def _apply_filter(self, *_):
            want = self.cb_filter.currentData()
            for i in range(self.tbl.rowCount()):
                st = self.tbl.item(i, 0).data(Qt.UserRole)
                self.tbl.setRowHidden(i, want != "all" and st != want)

        def report_text(self):
            """The shown rows as plain text."""
            out = []
            for i, r in enumerate(self._rows):
                if self.tbl.isRowHidden(i):
                    continue
                head = "[%s] %s" % (self._status_text(r["status"]), r["input"])
                if r["outputs"]:
                    head += " -> " + ", ".join(r["outputs"])
                if r["courses"]:
                    head += " (%s)" % ", ".join(r["courses"])
                out.append(head)
                out += ["    - " + self._message_text(m) for m in r["messages"]]
            return "\n".join(out)

        def _copy_report(self):
            QApplication.clipboard().setText(self.report_text())

        def _row_open(self, row, _col):
            path = self.tbl.item(row, 1).data(Qt.UserRole)
            if path and pathlib.Path(path).exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))

        def _cell_click(self, row, col):
            if col != 2:
                return
            outs = self.tbl.item(row, 2).data(Qt.UserRole) or []
            if outs:
                self._reveal(outs[0])

        def _reveal(self, path):
            """Show `path` selected in the file manager (parent folder
            where selecting is not supported)."""
            import subprocess
            import sys
            p = pathlib.Path(path)
            if sys.platform == "win32" and p.exists():
                subprocess.Popen(["explorer", "/select,", str(p)])
            elif p.parent.is_dir():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent)))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Convert charts between TJA, Gen2 .sht and Gen3 fumen "
                    ".bin (and nus3bank audio to VAG).")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--to", required=True,
                    help="comma list of sht,tja,bin,vag")
    ap.add_argument("-o", "--out", required=True, help="output folder")
    ap.add_argument("--id", help="output song id (one song only)")
    ap.add_argument("--players", default="1p", help="sht: 1p, 2p or 1p,2p")
    a = ap.parse_args(argv)
    fm = {x.strip().lower() for x in a.to.split(",") if x.strip()}
    bad = fm - {"sht", "tja", "bin", "vag"}
    if bad or not fm:
        ap.error("unknown format(s): %s" % (", ".join(sorted(bad)) or a.to))
    players = tuple(dict.fromkeys(
        x.strip().lower() for x in a.players.split(",") if x.strip()))
    if not players or set(players) - {"1p", "2p"}:
        ap.error("--players takes 1p, 2p or 1p,2p")
    paths = []
    for x in a.inputs:
        p = pathlib.Path(x)
        if p.is_dir():                  # a folder: every chart/audio in it
            paths += [str(f) for f in sorted(p.rglob("*"))
                      if f.is_file() and detect(f)]
        else:
            paths.append(x)
    _s, w, errors = convert(paths, a.out, fm, players=players, out_id=a.id)
    print("%d files written, %d problems" % (len(w), len(errors)))
    return 1 if errors else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

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


def load(path):
    """Read one input. Returns a song piece:
    {"id", "kind", "title", "wave", "charts": {course: sht}, "levels": {},
     "audio": path or None, "warnings": []}."""
    p = pathlib.Path(path)
    kind = detect(p)
    if kind is None:
        raise ValueError("%s: not a TJA, sht, Gen3 fumen or nus3bank" % p.name)
    piece = {"id": None, "kind": kind, "title": "", "wave": "", "charts": {},
             "levels": {}, "audio": None, "warnings": []}
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
                text, course, warn=lambda w, c=course: piece["warnings"].append(
                    "%s: %s" % (c, w)))
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


def convert(paths, out_dir, formats, players=("1p",), out_id=None, log=print,
            cancel=None):
    """Load, group and write. Returns (songs, written paths, errors).
    `out_id` makes every input one song with that id.

    `cancel()` is polled between input files and between songs (never inside
    a song's write), so a cancel leaves only whole files behind: the songs
    already written stay, the rest are not started."""
    pieces, errors = [], []
    for p in paths:
        if cancel is not None and cancel():
            log("cancelled")
            return [], [], errors
        try:
            pc = load(p)
        except Exception as e:
            errors.append("%s: %s" % (pathlib.Path(p).name, e))
            log("skip %s" % errors[-1])
            continue
        for w in pc["warnings"]:
            log("note %s: %s" % (pathlib.Path(p).name, w))
        pieces.append(pc)
    songs = group(pieces, out_id=out_id, warn=lambda w: log("note " + w))
    written = []
    for s in songs:
        if cancel is not None and cancel():
            log("cancelled before %s" % s["id"])
            break
        n0, e0 = len(written), len(errors)
        write(s, out_dir, formats, players=players, log=log,
              written=written, errors=errors, protect=paths)
        if len(written) == n0 and len(errors) == e0:
            errors.append("%s: nothing to write as %s (%s)" % (
                s["id"], "/".join(sorted(formats)),
                "no charts" if not s["charts"] else "no nus3bank audio"))
            log("FAILED %s" % errors[-1])
    return songs, written, errors


# --- GUI -------------------------------------------------------------------

try:
    from PySide6.QtCore import QThread, Signal
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
        QPlainTextEdit, QFileDialog, QMessageBox, QListWidget, QCheckBox,
        QGroupBox, QAbstractItemView)
    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtGui import QDesktopServices
except ImportError:                                            # headless / CLI
    QDialog = None


if QDialog is not None:
    from taiko2x6.app.i18n import tr
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

            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            v.addWidget(self.log, 1)

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

            self._drop_style = "QListWidget { border: 2px dashed #3b82f6; }"
            self._load_settings()
            self.lst.model().rowsInserted.connect(self._sync)
            self.lst.model().rowsRemoved.connect(self._sync)
            self.lst.model().modelReset.connect(self._sync)
            self.ed_out.textChanged.connect(self._sync)
            for c in self.ck.values():
                c.toggled.connect(self._sync)
            self._sync()

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
            self._worker = _ConvertWorker(dict(
                paths=paths, out_dir=out, formats=formats, players=players,
                out_id=self.ed_id.text().strip() or None), self)
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
                QMessageBox.critical(self, self.windowTitle(), str(res))
                return
            songs, written, errors = res
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

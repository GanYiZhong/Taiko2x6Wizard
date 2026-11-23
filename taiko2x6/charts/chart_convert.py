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

from taiko2x6.charts import gen3_convert, sht_export, tja2sht

COURSES = ("easy", "normal", "hard", "oni", "ura")
LETTER = {"easy": "e", "normal": "n", "hard": "h", "oni": "m", "ura": "x"}
_BY_LETTER = {v: k for k, v in LETTER.items()}
_TJA_IDX = {0: "easy", 1: "normal", 2: "hard", 3: "oni", 4: "ura"}
_STAR_KEY = {"easy": "starEasy", "normal": "starNormal", "hard": "starHard",
             "oni": "starMania", "ura": "starUra"}


def read_text(path):
    raw = pathlib.Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "shift_jis", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


def detect(path):
    """'tja', 'sht', 'bin' (Gen3 fumen), 'audio' (nus3bank) or None."""
    p = pathlib.Path(path)
    suf = p.suffix.lower()
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
        except Exception:
            pass
        try:
            tja2sht.parse_sht(p.read_bytes())
            return "sht"
        except Exception:
            return None
    return None


def _course_of(name):
    stem = pathlib.Path(name).stem.lower()
    for s in (pathlib.Path(name).parent.name.lower(), stem):
        if len(s) > 2 and s[-2] == "_" and s[-1] in _BY_LETTER:
            return _BY_LETTER[s[-1]]
    return None


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
            piece["charts"][course] = tja2sht.convert_tja(text, course)
            lv = cdata["meta"].get("LEVEL")
            try:
                if lv:
                    piece["levels"][course] = int(float(lv))
            except ValueError:
                pass
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


def group(pieces):
    """Merge pieces with the same id into songs (first title/wave wins)."""
    songs = {}
    for pc in pieces:
        s = songs.setdefault(pc["id"], {"id": pc["id"], "title": "", "wave": "",
                                        "charts": {}, "levels": {},
                                        "audio": None, "sources": set()})
        s["title"] = s["title"] or pc["title"]
        s["wave"] = s["wave"] or pc["wave"]
        s["charts"].update(pc["charts"])
        s["levels"].update(pc["levels"])
        s["audio"] = s["audio"] or pc["audio"]
        s["sources"].add(pc["kind"])
    return list(songs.values())


def write(song, out_dir, formats, players=("1p",), out_id=None, log=print,
          written=None):
    """Write a song in each of `formats` ('sht', 'tja', 'bin', 'vag').
    Returns the written paths (also appended to `written` as they land, so
    a later failure does not hide what was already written)."""
    out = pathlib.Path(out_dir)
    sid = out_id or song["id"]
    written = [] if written is None else written
    charts = song["charts"]
    if "sht" in formats:
        for course, sht in charts.items():
            n = len(tja2sht.parse_sht(sht)["notes"])
            limit = tja2sht.DIFFICULTY_NOTE_LIMIT.get(course)
            for pl in players:
                d = out / ("%s%s_%s" % (sid, pl, LETTER[course]))
                d.mkdir(parents=True, exist_ok=True)
                (d / "sht").write_bytes(sht)
                written.append(d / "sht")
            note = ""
            if limit is not None and n > limit:
                note = " — over the retail %s max (%d); check in game" % (
                    course, limit)
            log("sht  %s %s: %d notes%s" % (sid, course, n, note))
    if "tja" in formats and charts:
        text = sht_export.sht_to_tja(
            charts, title=song["title"] or sid, wave=song["wave"] or (sid + ".ogg"),
            levels=song["levels"])
        dst = out / (sid + ".tja")
        out.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
        written.append(dst)
        log("tja  %s: %s" % (sid, dst))
    if "bin" in formats:
        for course, sht in charts.items():
            d = out / sid
            d.mkdir(parents=True, exist_ok=True)
            dst = d / ("%s_%s.bin" % (sid, LETTER[course]))
            dst.write_bytes(sht_export.sht_to_gen3_bin(
                sht, course=course, stars=song["levels"].get(course)))
            written.append(dst)
            log("bin  %s %s: %s" % (sid, course, dst))
    if "vag" in formats and song.get("audio"):
        dst = out / (sid + ".vag")
        out.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(gen3_convert.convert_audio(song["audio"]))
        written.append(dst)
        log("vag  %s: %s" % (sid, dst))
    return written


def convert(paths, out_dir, formats, players=("1p",), out_id=None, log=print):
    """Load, group and write. Returns (songs, written paths, errors)."""
    pieces, errors = [], []
    for p in paths:
        try:
            pieces.append(load(p))
        except Exception as e:
            errors.append("%s: %s" % (pathlib.Path(p).name, e))
            log("skip %s" % errors[-1])
    songs = group(pieces)
    written = []
    for s in songs:
        try:
            write(s, out_dir, formats, players=players,
                  out_id=out_id if len(songs) == 1 else None, log=log,
                  written=written)
        except Exception as e:
            errors.append("%s: %s" % (s["id"], e))
            log("FAILED %s" % errors[-1])
    return songs, written, errors


# --- GUI -------------------------------------------------------------------

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
        QPlainTextEdit, QFileDialog, QMessageBox, QListWidget, QCheckBox,
        QGroupBox, QAbstractItemView)
except ImportError:                                            # headless / CLI
    QDialog = None


if QDialog is not None:
    from taiko2x6.app.i18n import tr

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
            v.addWidget(self.lst, 1)
            row = QHBoxLayout()
            b = QPushButton(tr("Add files…"))
            b.clicked.connect(self._add)
            row.addWidget(b)
            b = QPushButton(tr("Remove"))
            b.clicked.connect(self._remove)
            row.addWidget(b)
            b = QPushButton(tr("Clear"))
            b.clicked.connect(self.lst.clear)
            row.addWidget(b)
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
            b.clicked.connect(self._browse_out)
            row.addWidget(b)
            v.addLayout(row)

            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            v.addWidget(self.log, 1)

            row = QHBoxLayout()
            row.addStretch(1)
            self.b_go = QPushButton(tr("Convert"))
            self.b_go.clicked.connect(self._run)
            row.addWidget(self.b_go)
            close = QPushButton(tr("Close"))
            close.clicked.connect(self.reject)
            row.addWidget(close)
            v.addLayout(row)

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
            ps, _ = QFileDialog.getOpenFileNames(
                self, tr("Add charts"), "",
                tr("Charts and audio (*.tja *.sht *.bin sht *.nus3bank);;"
                   "All files (*)"))
            self.add_paths(ps)

        def _remove(self):
            for it in self.lst.selectedItems():
                self.lst.takeItem(self.lst.row(it))

        def dragEnterEvent(self, e):
            if e.mimeData().hasUrls():
                e.acceptProposedAction()

        def dropEvent(self, e):
            paths = []
            for u in e.mimeData().urls():
                p = pathlib.Path(u.toLocalFile())
                if p.is_dir():
                    paths += [str(x) for x in sorted(p.rglob("*"))
                              if x.is_file() and detect(x)]
                elif p.is_file():
                    paths.append(str(p))
            self.add_paths(paths)

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
            if not formats - {"vag"} and not any(
                    detect(p) == "audio" for p in paths):
                QMessageBox.warning(self, self.windowTitle(),
                                    tr("Tick at least one format to write."))
                return
            if not out:
                QMessageBox.warning(self, self.windowTitle(),
                                    tr("Pick an output folder."))
                return
            players = tuple(p for p, c in (("1p", self.ck_1p), ("2p", self.ck_2p))
                            if c.isChecked()) or ("1p",)
            self.b_go.setEnabled(False)
            self.log.clear()
            try:
                songs, written, errors = convert(
                    paths, out, formats, players=players,
                    out_id=self.ed_id.text().strip() or None, log=self._say)
                self._say(tr("done: {s} songs, {f} files written, {e} problems")
                          .format(s=len(songs), f=len(written), e=len(errors)))
            finally:
                self.b_go.setEnabled(True)

        def _say(self, s):
            self.log.appendPlainText(str(s))
            self.log.repaint()


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
    fm = {x.strip() for x in a.to.split(",") if x.strip()}
    bad = fm - {"sht", "tja", "bin", "vag"}
    if bad:
        ap.error("unknown format(s): %s" % ", ".join(sorted(bad)))
    _s, _w, errors = convert(a.inputs, a.out, fm,
                             players=tuple(a.players.split(",")), out_id=a.id)
    return 1 if errors else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

#!/usr/bin/env python3
"""
Batch add songs -- many custom songs into the open archive in one go.

Two layers:

  * scan / build logic (no widgets): `scan_sources` finds songs (TJA + audio, or
    Gen3 song folders), `BatchBuilder` builds the ticked ones one after another
    by calling the SAME functions the single-song Song Builder calls
    (`song_builder.prepare_new_song_db` + `song_builder.stage_new_song`). Nothing
    here re-implements the build.
  * `BatchAddDialog`: a table of what was found (title, editable new id,
    courses + stars, audio, problems), untick what you do not want, Build.

Adding a song creates new groups, which needs a full rebuild of DATA.000 (see
add_new_song). The batch stages every song (DB bins staged on the archive so
song N+1 sees song N; new groups collected) and rebuilds ONCE at the end. The
caller's pending edits are restored afterwards; the final (list_bytes,
data_bytes) is handed back the same way the single-song dialog does and
written through the same prompt.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from taiko2x6.charts import tja2sht
from taiko2x6.songs import song_builder as SB

logger = logging.getLogger("taiko.songbuilder")

_ID_RE = re.compile(r"^[a-z0-9_]{1,16}$")
_AUDIO_EXTS = (".ogg", ".wav")
_MAX_DEPTH = 6
_COURSE_ORDER = ("easy", "normal", "hard", "oni")


# --------------------------------------------------------------------------- #
#  candidates
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    kind: str                      # "tja" | "gen3"
    path: str                      # the .tja file, or the Gen3 song folder
    title: str = ""
    sid: str = ""                  # proposed new song id (editable)
    courses: list = field(default_factory=list)   # [(course, stars or None)]
    stars: list | None = None      # Gen3: song_builder star list, else None
    audio: str | None = None
    problems: list = field(default_factory=list)  # block the build
    warnings: list = field(default_factory=list)  # build still possible
    tja_text: str | None = None
    gen3: dict | None = None
    checked: bool = True

    @property
    def ok(self) -> bool:
        return not self.problems


def sanitize_id(text: str) -> str:
    """Lower-case ASCII id from a file/title stem ('' if nothing usable)."""
    s = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    return s[:12]


def unique_id(base: str, taken, fallback: str = "song") -> str:
    """`base` (or `fallback`) with a number appended until it is not in `taken`."""
    base = sanitize_id(base) or fallback
    if base not in taken:
        return base
    n = 2
    while True:
        cand = "%s%d" % (base[:12 - len(str(n))], n)
        if cand not in taken:
            return cand
        n += 1


def id_problem(sid: str, taken) -> str | None:
    """Why `sid` cannot be a new song id, or None."""
    if not _ID_RE.match(sid or ""):
        return "id must be 1-16 characters of a-z, 0-9 or _"
    if sid in taken:
        return "id already in use"
    return None


def _find_audio(tja_path: Path, meta: dict):
    """The audio next to a TJA: WAVE: header first, else same stem .ogg/.wav."""
    folder = tja_path.parent
    wave = str(meta.get("WAVE", "") or "").strip()
    if wave:
        p = folder / wave
        if p.is_file():
            return str(p)
        low = wave.lower()                      # tolerate a case mismatch
        try:
            for q in folder.iterdir():
                if q.name.lower() == low and q.is_file():
                    return str(q)
        except OSError:
            pass
    for ext in _AUDIO_EXTS:
        for q in (folder / (tja_path.stem + ext), folder / (tja_path.stem + ext.upper())):
            if q.is_file():
                return str(q)
    return None


def _tja_candidate(path: Path) -> Candidate:
    c = Candidate(kind="tja", path=str(path), title=path.stem)
    try:
        text, warn = SB._read_tja_text(str(path))
    except Exception as exc:
        c.problems.append("cannot read: %s" % exc)
        return c
    if warn:
        c.warnings.append(warn)
    try:
        parsed = tja2sht.parse_tja(text)
    except Exception as exc:
        c.problems.append("cannot parse: %s" % exc)
        return c
    meta = parsed.get("meta", {}) if isinstance(parsed, dict) else {}
    c.title = str(meta.get("TITLE", "") or "").strip() or path.stem
    levels = SB._course_levels(parsed)
    courses = parsed.get("courses", {}) or {}
    if not courses:
        c.problems.append("no playable courses")
    c.courses = [(n, levels.get(n)) for n in _COURSE_ORDER if n in levels]
    if courses and not c.courses:
        c.courses = [("?", None)]
    if not meta.get("BPM"):
        c.warnings.append("no BPM: header")
    c.audio = _find_audio(path, meta)
    if c.audio is None:
        c.warnings.append("audio not found")
        c.checked = False
    c.tja_text = text
    return c


def _is_gen3_folder(d: Path) -> bool:
    try:
        return any((d / ("%s_%s.bin" % (d.name, s))).is_file() for s in "emhnx")
    except OSError:
        return False


def _gen3_candidate(folder: Path) -> Candidate:
    from taiko2x6.charts import gen3_song
    c = Candidate(kind="gen3", path=str(folder), title=folder.name, sid=folder.name)
    try:
        song = gen3_song.load_song(folder)
    except Exception as exc:
        c.problems.append(str(exc))
        return c
    c.gen3 = song
    c.title = song.get("title") or song["sid"]
    c.sid = song["sid"]
    stars = gen3_song.stars_list(song)
    c.stars = stars
    c.courses = [(n, song.get("stars", {}).get(n)) for n in _COURSE_ORDER
                 if n in song.get("sht", {})]
    c.audio = song.get("audio_path")
    if stars is None:
        c.warnings.append("no star ratings in the game data; the template's are used")
    if c.audio is None:
        c.warnings.append("audio not found")
        c.checked = False
    for w in song.get("warnings", []):
        if "audio not found" not in w:
            c.warnings.append(w)
    return c


def scan_sources(paths, taken=(), progress=None) -> list:
    """Find songs under `paths` (TJA files, Gen3 song folders, or folders of them).

    Returns candidates in a stable (sorted) order with a proposed unique id each;
    ids avoid `taken` and each other. `progress(text)` is called per song found.
    """
    seen, found = set(), []

    def add(c: Candidate):
        key = os.path.normcase(os.path.abspath(c.path))
        if key in seen:
            return
        seen.add(key)
        found.append(c)
        if progress:
            progress(c.title)

    def walk(d: Path, depth: int):
        if _is_gen3_folder(d):
            add(_gen3_candidate(d))
            return
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for p in entries:
            if p.is_file() and p.suffix.lower() == ".tja":
                add(_tja_candidate(p))
        if depth < _MAX_DEPTH:
            for p in entries:
                if p.is_dir():
                    walk(p, depth + 1)

    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            walk(p, 0)
        elif p.is_file() and p.suffix.lower() == ".tja":
            add(_tja_candidate(p))
    used = set(taken)
    for c in found:
        if not (c.kind == "gen3" and c.sid and c.sid not in used):
            c.sid = unique_id(c.sid or Path(c.path).stem, used)
        used.add(c.sid)
    return found


# --------------------------------------------------------------------------- #
#  song limit
# --------------------------------------------------------------------------- #
def check_song_limit(current: int, adding: int, limit: int | None = None):
    """(level, text) about the song-count ceiling after adding, or None.

    The exe's ceiling lives in `taiko`, not in the archive, so it is not read
    from here: `limit` defaults to the stock 210. level is "warn" past that
    (raise it first, Tools > taiko executable patches) and "block" past the
    most the song tables can grow to.
    """
    from taiko2x6.exe import song_tables
    limit = song_tables.STOCK_SONGS if limit is None else limit
    total = current + adding
    if total > song_tables.MAX_SONGS:
        return ("block", total, song_tables.MAX_SONGS)
    if total > limit:
        return ("warn", total, limit)
    return None


# --------------------------------------------------------------------------- #
#  build
# --------------------------------------------------------------------------- #
@dataclass
class BuildItem:
    sid: str
    title: str
    kind: str                      # "tja" | "gen3"
    tja_text: str | None = None
    audio: str | None = None
    gen3: dict | None = None
    lyricist: str = ""
    composer: str = ""
    copyright_: str = ""
    lead_silence_ms: float = 0.0


@dataclass
class ItemResult:
    sid: str
    ok: bool
    message: str = ""


@dataclass
class BatchResult:
    items: list = field(default_factory=list)
    new_archive: tuple | None = None       # (list_bytes, data_bytes), or None

    @property
    def built(self) -> int:
        return sum(1 for r in self.items if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.items if not r.ok)


class BatchBuilder:
    """Builds items on top of `archive` with ONE final DATA.000 rebuild.

    Each song is staged (`song_builder.stage_new_song`): its three DB bins are
    staged on the archive, so the next song's DB step sees the previous songs
    (exactly what re-opening a rebuilt archive used to give), and its new
    groups / extra select files are collected. `build_final` then calls
    archive_builder.build_archive once with everything. Unchanged groups keep
    their sectors, new groups are appended. The caller's archive is restored to
    its starting pending edits by `close()`.

    Step API (the dialog interleaves a worker thread between prepare and
    finish): `prepare(item)` on the GUI thread returns a `task(log)` to run
    anywhere; `finish(item, res)` folds the outcome in; `build_final(log)` does
    the single rebuild (any thread); `close()` releases. `run_all` does the lot
    synchronously.
    """

    def __init__(self, archive, template_sid: str, log=lambda s: None):
        self.original = archive
        self.template_sid = template_sid
        self.log = log
        self.result = BatchResult()
        self._base = (dict(archive.replacements), set(archive.compact))
        self._snap = None
        self._groups: list = []
        self._extra: dict = {}
        self._closed = False

    # -- steps -----------------------------------------------------------------
    def prepare(self, item: BuildItem):
        """GUI thread: DB step for `item`; returns task(log) -> stage_new_song result."""
        cur = self.original
        # stage_new_song stages the DB bins on the archive; remember what was
        # there so a failed song can be rolled back.
        self._snap = (dict(cur.replacements), set(cur.compact))
        if item.kind == "gen3":
            from taiko2x6.charts import gen3_song
            stars = gen3_song.stars_list(item.gen3)
            db_pre, stars = SB.prepare_new_song_db(
                cur, item.sid, self.template_sid, None, stars=stars)

            def task(log):
                kw = gen3_song.to_builder_kwargs(item.gen3, log=log)
                return SB.stage_new_song(
                    cur, item.sid, title=item.title, template_sid=self.template_sid,
                    lyricist=item.lyricist, composer=item.composer,
                    copyright_=item.copyright_, charts=kw["charts"],
                    audio_vag=kw["audio_vag"], precomputed_db=db_pre,
                    stars=stars, log=log)
        else:
            db_pre, stars = SB.prepare_new_song_db(
                cur, item.sid, self.template_sid, item.tja_text)

            def task(log):
                return SB.stage_new_song(
                    cur, item.sid, title=item.title, template_sid=self.template_sid,
                    lyricist=item.lyricist, composer=item.composer,
                    copyright_=item.copyright_, tja_text=item.tja_text,
                    audio_path=item.audio, lead_silence_ms=item.lead_silence_ms,
                    precomputed_db=db_pre, stars=stars, log=log)
        return task

    def abandon(self):
        """Undo what a failed step staged on the archive."""
        if self._snap is not None:
            rep, comp = self._snap
            self.original.replacements = rep
            self.original.compact = comp
            self._snap = None

    def finish(self, item: BuildItem, res) -> ItemResult:
        """Fold one task outcome in. `res` is stage_new_song's tuple or an error."""
        if isinstance(res, tuple) and len(res) == 3 and res[0] is SB._BUILD_ERROR:
            self.abandon()
            return self._record(item, False, str(res[1]))
        new_groups, extra_files, summ = res
        if summ["errors"]:
            self.abandon()
            return self._record(item, False, "; ".join(summ["errors"]))
        self._snap = None                  # keep this song's staged DB bins
        self._groups.extend(new_groups)
        for gname, files in extra_files.items():
            self._extra.setdefault(gname, []).extend(files)
        msg = "; ".join(summ.get("warnings", []))
        return self._record(item, True, msg)

    def fail(self, item: BuildItem, exc) -> ItemResult:
        self.abandon()
        return self._record(item, False, str(exc))

    def _record(self, item, ok, msg) -> ItemResult:
        r = ItemResult(item.sid, ok, msg)
        self.result.items.append(r)
        return r

    # -- the one rebuild -------------------------------------------------------
    def build_final(self, log=None):
        """Rebuild DATA.000 once with every built song; any thread. Sets and
        returns result.new_archive ((list_bytes, data_bytes) or None)."""
        if not self.result.built:
            self.result.new_archive = None
            return None
        from taiko2x6.archive import archive_builder
        log = log or self.log
        log("rebuilding DATA.000 once (+%d groups, %d songs)..."
            % (len(self._groups), self.result.built))
        self.result.new_archive = archive_builder.build_archive(
            self.original, self._groups, self._extra)
        return self.result.new_archive

    # -- whole run -------------------------------------------------------------
    def run_all(self, items, progress=None) -> BatchResult:
        """Build every item (continue on error); returns the BatchResult.

        progress(i, n, sid) is called before each song. Must run on the GUI thread.
        """
        try:
            for i, item in enumerate(items):
                if progress:
                    progress(i, len(items), item.sid)
                try:
                    task = self.prepare(item)
                    res = task(self.log)
                except Exception as exc:
                    self.fail(item, exc)
                    continue
                self.finish(item, res)
            self.build_final()
        finally:
            self.close()
        return self.result

    def close(self):
        """Give the caller's archive back its starting pending edits."""
        if self._closed:
            return
        self._closed = True
        self.original.replacements = dict(self._base[0])
        self.original.compact = set(self._base[1])
        self._snap = None


def items_from_candidates(cands, lyricist="", composer="", copyright_="") -> list:
    """BuildItems for the candidates (title/id as currently set)."""
    out = []
    for c in cands:
        out.append(BuildItem(
            sid=c.sid, title=c.title or c.sid, kind=c.kind, tja_text=c.tja_text,
            audio=c.audio, gen3=c.gen3, lyricist=lyricist, composer=composer,
            copyright_=copyright_))
    return out


# --------------------------------------------------------------------------- #
#  dialog
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QAbstractItemView, QApplication, QDialog, QHBoxLayout, QHeaderView, QLabel,
        QMessageBox, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
        QVBoxLayout, QComboBox, QFormLayout)
    from taiko2x6.app import appconfig
    from taiko2x6.app.dialog_polish import polish
    from taiko2x6.app.i18n import tr
    from taiko2x6.songs import song_dialog_ui as UI

    def _t(text):
        """Translate a problem/warning text produced by the scan logic."""
        known = {
            "no playable courses": lambda: tr("no playable courses"),
            "audio not found": lambda: tr("audio not found"),
            "no BPM: header": lambda: tr("no BPM: header"),
            "id must be 1-16 characters of a-z, 0-9 or _":
                lambda: tr("id must be 1-16 characters of a-z, 0-9 or _"),
            "id already in use": lambda: tr("id already in use"),
            "no star ratings in the game data; the template's are used":
                lambda: tr("no star ratings in the game data; the template's are used"),
        }
        f = known.get(text)
        return f() if f else text

    _COL_TITLE, _COL_ID, _COL_COURSES, _COL_AUDIO, _COL_PROBLEMS = range(5)

    class BatchAddDialog(QDialog):
        def __init__(self, archive, parent=None, paths=None):
            super().__init__(parent)
            self.setWindowTitle(tr("Batch add songs"))
            self.resize(900, 620)
            self.setAcceptDrops(True)
            self.archive = archive
            self.new_archive = None          # (list_bytes, data_bytes) after Build
            self.result = None
            self._cands: list = []
            self._ids: list = []
            self._busy = False
            self._worker = None
            self._builder = None
            self._queue: list = []
            self._loading = False
            self._build_ui()
            polish(self, primary=self.b_build)
            if paths:
                self.add_paths(paths)

        # -- ui ---------------------------------------------------------------
        def _build_ui(self):
            lay = QVBoxLayout(self)
            lay.setSpacing(8)

            top = QFormLayout()
            self.cb_tmpl = QComboBox()
            try:
                self._ids = list(SB.song_ids(self.archive))
                self.cb_tmpl.addItems(self._ids)
            except Exception as exc:
                QMessageBox.warning(self, tr("Batch add songs"),
                                    tr("Could not list songs: {err}").format(err=exc))
            self.cb_tmpl.setToolTip(tr(
                "The existing song whose texture and chart layout every new song copies."))
            top.addRow(tr("template (id):"), self.cb_tmpl)
            lay.addLayout(top)

            row = QHBoxLayout()
            self.b_folder = QPushButton(tr("Add folder…"))
            self.b_folder.setToolTip(tr(
                "Find every TJA (with its audio) and Gen3 song folder inside a folder."))
            self.b_folder.clicked.connect(self._pick_folder)
            self.b_files = QPushButton(tr("Add TJA files…"))
            self.b_files.clicked.connect(self._pick_files)
            self.b_clear = QPushButton(tr("Clear list"))
            self.b_clear.clicked.connect(self.clear)
            for b in (self.b_folder, self.b_files, self.b_clear):
                row.addWidget(b)
            row.addStretch(1)
            lay.addLayout(row)

            self.lbl_hint = QLabel(tr(
                "Pick a folder, or drop TJA files and Gen3 song folders here. "
                "Untick what you do not want; the new id can be edited."))
            self.lbl_hint.setWordWrap(True)
            lay.addWidget(self.lbl_hint)

            self.table = QTableWidget(0, 5)
            self.table.setHorizontalHeaderLabels([
                tr("Title"), tr("New id"), tr("Courses (stars)"), tr("Audio"),
                tr("Problems")])
            self.table.verticalHeader().setVisible(False)
            self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.table.setEditTriggers(QAbstractItemView.DoubleClicked
                                       | QAbstractItemView.EditKeyPressed)
            hh = self.table.horizontalHeader()
            hh.setSectionResizeMode(_COL_TITLE, QHeaderView.Stretch)
            hh.setSectionResizeMode(_COL_PROBLEMS, QHeaderView.Stretch)
            for c in (_COL_ID, _COL_COURSES, _COL_AUDIO):
                hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
            self.table.itemChanged.connect(self._item_changed)
            lay.addWidget(self.table, 1)

            self.progress = QProgressBar()
            self.progress.setVisible(False)
            lay.addWidget(self.progress)
            self.log = UI.make_log()
            self.log.setMaximumHeight(130)
            lay.addWidget(self.log)
            self.lbl_status = UI.status_label()
            lay.addWidget(self.lbl_status)
            box, self.b_build = UI.make_buttons(
                tr("Build selected → stage"), tr("Close"), self._build, self.reject)
            lay.addWidget(box)
            self._refresh_status()

        def _log(self, text):
            UI.log_append(self.log, text)

        # -- sources ----------------------------------------------------------
        def dragEnterEvent(self, e):
            if e.mimeData().hasUrls() and not self._busy:
                e.acceptProposedAction()

        def dropEvent(self, e):
            paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
            if paths and not self._busy:
                self.add_paths(paths)

        def _pick_folder(self):
            d = appconfig.pick_dir(self, "batch_songs_dir",
                                   tr("Choose a folder of songs"))
            if d:
                self.add_paths([d])

        def _pick_files(self):
            ps = appconfig.pick_open_many(self, "tja", tr("Choose TJA files"),
                                          "TJA charts (*.tja)")
            if ps:
                self.add_paths(ps)

        def clear(self):
            if self._busy:
                return
            self._cands = []
            self._fill()

        def add_paths(self, paths):
            """Scan `paths` and append what was found (already-listed songs skipped)."""
            have = {os.path.normcase(os.path.abspath(c.path)) for c in self._cands}
            taken = set(self._ids) | {c.sid for c in self._cands}
            self.lbl_status.setText(tr("Scanning…"))
            QApplication.processEvents()
            found = scan_sources(paths, taken, progress=lambda t: QApplication.processEvents())
            new = [c for c in found
                   if os.path.normcase(os.path.abspath(c.path)) not in have]
            # ids were chosen against `taken`; scan_sources keeps them unique
            self._cands.extend(new)
            self._log(tr("Found {n} song(s).").format(n=len(new)))
            self._fill()

        # -- table ------------------------------------------------------------
        def _taken_by_others(self, row):
            return set(self._ids) | {c.sid for i, c in enumerate(self._cands) if i != row}

        def _row_problems(self, row):
            c = self._cands[row]
            probs = [_t(p) for p in c.problems]
            msg = self._id_msg(row)
            if msg:
                probs.append(msg)
            return probs

        def _id_msg(self, row):
            msg = id_problem(self._cands[row].sid, self._taken_by_others(row))
            return _t(msg) if msg else None

        def _fill(self):
            self._loading = True
            try:
                self.table.setRowCount(len(self._cands))
                for r, c in enumerate(self._cands):
                    t = QTableWidgetItem(c.title)
                    t.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                               | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
                    t.setCheckState(Qt.Checked if c.checked and c.ok else Qt.Unchecked)
                    t.setToolTip(c.path)
                    self.table.setItem(r, _COL_TITLE, t)
                    i = QTableWidgetItem(c.sid)
                    self.table.setItem(r, _COL_ID, i)
                    co = QTableWidgetItem(" ".join(
                        "%s %s" % (n, s if s is not None else "?") for n, s in c.courses))
                    co.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    self.table.setItem(r, _COL_COURSES, co)
                    au = QTableWidgetItem(tr("yes") if c.audio else tr("no"))
                    au.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    if c.audio:
                        au.setToolTip(c.audio)
                    self.table.setItem(r, _COL_AUDIO, au)
                    p = QTableWidgetItem("")
                    p.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    self.table.setItem(r, _COL_PROBLEMS, p)
                self._refresh_rows()
            finally:
                self._loading = False
            self._refresh_status()

        def _refresh_rows(self):
            for r, c in enumerate(self._cands):
                probs = self._row_problems(r)
                warns = [_t(w) for w in c.warnings]
                self.table.item(r, _COL_PROBLEMS).setText("; ".join(probs + warns))
                t = self.table.item(r, _COL_TITLE)
                if probs:
                    t.setCheckState(Qt.Unchecked)
                    t.setFlags(t.flags() & ~Qt.ItemIsUserCheckable)
                else:
                    t.setFlags(t.flags() | Qt.ItemIsUserCheckable)
                    t.setCheckState(Qt.Checked if c.checked else Qt.Unchecked)
                self.table.item(r, _COL_ID).setToolTip(
                    self._id_msg(r) or tr("Double-click to edit the new song id."))

        def _item_changed(self, item):
            if self._loading:
                return
            r = item.row()
            if not (0 <= r < len(self._cands)):
                return
            if item.column() == _COL_ID:
                self._cands[r].sid = item.text().strip()
            elif item.column() == _COL_TITLE:
                self._cands[r].checked = item.checkState() == Qt.Checked
                self._cands[r].title = item.text()
            self._loading = True
            try:
                self._refresh_rows()
            finally:
                self._loading = False
            self._refresh_status()

        def selected(self) -> list:
            """Candidates that are ticked and buildable, in table order."""
            out = []
            for r, c in enumerate(self._cands):
                t = self.table.item(r, _COL_TITLE)
                if t is not None and t.checkState() == Qt.Checked and not self._row_problems(r):
                    out.append(c)
            return out

        def _refresh_status(self):
            if not hasattr(self, "b_build"):
                return
            problems = []
            sel = self.selected()
            if not self.cb_tmpl.currentText():
                problems.append(tr("No template song in this archive."))
            if not sel:
                problems.append(tr("Nothing selected."))
            note = None
            lim = check_song_limit(len(self._ids), len(sel)) if sel else None
            if lim:
                level, total, limit = lim
                if level == "block":
                    problems.append(tr(
                        "{total} songs is more than the game's tables can hold ({limit}).")
                        .format(total=total, limit=limit))
                else:
                    note = tr(
                        "{total} songs is past the stock ceiling of {limit}: raise it first "
                        "(Tools > taiko executable patches), or the game will not show them all."
                    ).format(total=total, limit=limit)
            UI.set_status(self.lbl_status, problems)
            if note and not problems:
                self.lbl_status.setText("⚠ " + note)
            elif not problems:
                self.lbl_status.setText("✓ " + tr("{n} song(s) ready to build.").format(n=len(sel)))
            self.b_build.setEnabled(not problems and not self._busy)

        # -- build ------------------------------------------------------------
        def _set_busy(self, busy):
            self._busy = busy
            for w in (self.b_folder, self.b_files, self.b_clear, self.cb_tmpl, self.table):
                w.setEnabled(not busy)
            self.progress.setVisible(busy)
            self._refresh_status()

        def _build(self):
            if self._busy:
                return
            sel = self.selected()
            if not sel:
                return
            try:
                con = SB.check_db_consistency(self.archive)
            except Exception as exc:
                con = {"consistent": False, "error": str(exc)}
            if not con.get("consistent", True):
                QMessageBox.critical(self, tr("Batch add songs"), tr(
                    "This archive's song-DB bins disagree on the song count; adding "
                    "songs would produce a non-bootable file. Reload a clean DATA.000 / "
                    "LIST.BIN first."))
                return
            self.log.clear()
            self._queue = items_from_candidates(sel)
            self._builder = BatchBuilder(self.archive, self.cb_tmpl.currentText(),
                                         log=self._log)
            self.progress.setRange(0, len(self._queue))
            self.progress.setValue(0)
            self._total = len(self._queue)
            self._set_busy(True)
            self._next()

        def _next(self):
            if not self._queue:
                self._done()
                return
            item = self._queue.pop(0)
            n = self._total - len(self._queue)
            self.lbl_status.setText(
                tr("Building {i}/{n}: {sid}…")
                .format(i=n, n=self._total, sid=item.sid))
            self._log(tr("[{i}/{n}] {sid} — {title}").format(
                i=n, n=self._total, sid=item.sid, title=item.title))
            try:
                task = self._builder.prepare(item)
            except Exception as exc:
                self._finish_item(item, self._builder.fail(item, exc))
                return
            from taiko2x6.songs.song_builder import _BuildWorker
            self._worker = _BuildWorker(task)
            self._worker.log_sig.connect(self._log)
            self._worker.done_sig.connect(lambda res, it=item: self._on_task(it, res))
            self._worker.start()

        def _on_task(self, item, res):
            w = self._worker
            if w is not None:
                w.wait()
                w.deleteLater()
                self._worker = None
            self._finish_item(item, self._builder.finish(item, res))

        def _finish_item(self, item, r):
            if r.ok:
                self._log("  " + tr("OK") + (" — " + r.message if r.message else ""))
            else:
                self._log("  " + tr("FAILED: {err}").format(err=r.message))
            self.progress.setValue(self.progress.value() + 1)
            QApplication.processEvents()
            self._next()

        def _done(self):
            b = self._builder
            if not b.result.built:
                self._finalize(None)
                return
            self.lbl_status.setText(
                tr("Rebuilding DATA.000 once for all songs, please wait…"))
            from taiko2x6.songs.song_builder import _BuildWorker
            self._worker = _BuildWorker(lambda log: b.build_final(log))
            self._worker.log_sig.connect(self._log)
            self._worker.done_sig.connect(self._finalize)
            self._worker.start()

        def _finalize(self, res):
            w = self._worker
            if w is not None:
                w.wait()
                w.deleteLater()
                self._worker = None
            b = self._builder
            if isinstance(res, tuple) and len(res) == 3 and res[0] is SB._BUILD_ERROR:
                b.result.new_archive = None
                self._log(tr("FAILED: {err}").format(err=res[1]))
            b.close()
            self.result = b.result
            self._builder = None
            self.new_archive = b.result.new_archive
            self.progress.setValue(self.progress.maximum())
            self._set_busy(False)
            self._log("\n" + tr("Done: {ok} built, {bad} failed.").format(
                ok=b.result.built, bad=b.result.failed))
            for r in b.result.items:
                if not r.ok:
                    self._log("  " + tr("failed: {sid}: {err}").format(sid=r.sid, err=r.message))
            if self.new_archive:
                self._log(tr("Close this dialog to write DATA.000 + LIST.BIN."))
            self.lbl_status.setText(tr("Done: {ok} built, {bad} failed.").format(
                ok=b.result.built, bad=b.result.failed))
            self.b_build.setEnabled(False)

        def reject(self):
            if self._busy:
                return
            if self.new_archive:
                self.accept()
                return
            super().reject()

        def done(self, code):
            if self._busy:
                return
            super().done(code)

except ImportError:
    BatchAddDialog = None  # type: ignore

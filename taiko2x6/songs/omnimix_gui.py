#!/usr/bin/env python3
"""
Omnimix Maker — GUI front-end for omnimix_maker.

Pick a TARGET (a Taiko 14+ HDD .img, or a loose DATA.000+list.bin folder) and a
list of SOURCES (Taiko 7…14 images/folders). It harvests every song the target
lacks — charts, name textures, audio, stars — appends them in one rebuild, and
(for an .img target) lifts the executable's song ceiling so they are reachable.

Pipeline, respecting the "SongManager is a QWidget → GUI thread only" rule:
  1. [worker]  extract each .img source + the .img target to temp DATA.000/list pairs
  2. [GUI]     plan the merge + precompute the merged DB bins (fast, small bins)
  3. [worker]  harvest every added song's groups + one build_archive
  4. [worker]  write the merged archive back / export it, then patch the song limit
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from taiko2x6.safeio import write_pair

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QElapsedTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLineEdit,
    QPushButton, QFileDialog, QCheckBox, QPlainTextEdit, QLabel, QMessageBox,
    QWidget, QListWidget, QListWidgetItem, QInputDialog, QGroupBox,
    QProgressBar, QSplitter, QTableWidget, QTableWidgetItem, QHeaderView,
    QTabWidget,
)

from taiko2x6.app import appconfig
from taiko2x6.app.i18n import tr
from taiko2x6.app.dialog_polish import polish
from taiko2x6.songs import omnimix_maker as OM

# musicinfo genre -> folder name, for the build summary
GENRE_NAMES = {0: "J-POP", 1: "アニメ", 2: "バラエティ", 3: "クラシック",
               4: "ナムコオリジナル", 5: "どうよう", 6: "ゲームミュージック",
               8: "C-POP"}
_COUNT_RE = re.compile(r"\b(\d+)\s*/\s*(\d+)\b")


def genre_summary(items) -> list:
    """[(folder name, count)] of PlanItems, in folder order, empty folders left out."""
    counts = {}
    for it in items:
        counts[it.genre] = counts.get(it.genre, 0) + 1
    order = [0, 1, 2, 3, 4, 5, 6, 8]
    out = [(GENRE_NAMES[g], counts.pop(g)) for g in order if g in counts]
    out += [(tr("genre {g}").format(g=g), n) for g, n in sorted(counts.items())]
    return out

logger = logging.getLogger("taiko.omnimix.gui")

_CFG = "omnimix"


def source_summary(plan, specs) -> list:
    """[(label, songs added, duplicates skipped)] per source, in source order.
    A song counts as skipped for a source when an earlier source (or the target)
    already provided it, or another id with the same title did."""
    added, skipped = {}, {}
    for it in plan.items:
        added[it.source_label] = added.get(it.source_label, 0) + 1
    for labels in plan.skipped_dupes.values():
        for lb in labels:
            skipped[lb] = skipped.get(lb, 0) + 1
    for v in plan.skipped_titles.values():
        skipped[v[0]] = skipped.get(v[0], 0) + 1
    labels = [sp.label for sp in specs]
    for lb in list(added) + list(skipped):
        if lb not in labels:
            labels.append(lb)
    return [(lb, added.get(lb, 0), skipped.get(lb, 0)) for lb in labels]


def source_path_errors(sources) -> list:
    """Messages for sources whose files are gone (checked before a long job)."""
    errs = []
    for s in sources:
        if s["kind"] == "img":
            paths = [s["img"]]
        elif s["kind"] == "iso":
            paths = [s["iso"]]
        else:
            paths = [s["list"], s["data"]]
        for p in paths:
            if not os.path.exists(p):
                errs.append(tr("Source “{label}” is missing: {path}").format(
                    label=s["label"], path=p))
    return errs


def input_error(target: str, is_img: bool, sources) -> str:
    """First problem with the chosen target/sources ('' when fine)."""
    target = (target or "").strip()
    if not target:
        return tr("Choose a target.")
    if is_img and not os.path.isfile(target):
        return tr("The target image does not exist:") + "\n" + target
    if not is_img:
        for n in ("DATA.000", "list.bin"):
            if not os.path.isfile(os.path.join(target, n)):
                return tr("The target folder has no {name}:").format(name=n) + "\n" + target
    if not sources:
        return tr("Add at least one source.")
    errs = source_path_errors(sources)
    if errs:
        return "\n".join(errs)
    return ""


def genre_name(g) -> str:
    return GENRE_NAMES.get(g) or tr("genre {g}").format(g=g)


def _stars_text(st) -> str:
    return "/".join("-" if (v is None or v < 0) else str(v) for v in (st or []))


def _title(plan, sid) -> str:
    return (getattr(plan, "titles", None) or {}).get(sid, "")


def plan_song_rows(plan) -> list:
    """One row per song the build adds: (id, title, source, folder, stars,
    gauge, preview). Gauge says whether the values come from a source game,
    were estimated from the chart, or stay the template's; preview likewise."""
    est = getattr(plan, "play_estimated", None) or set()
    rows = []
    for it in plan.items:
        if it.play:
            gauge = tr("estimated from chart") if it.sid in est else tr("from source")
        else:
            gauge = tr("template default")
        prev = tr("from source") if getattr(it, "preview", None) else tr("template default")
        rows.append((it.sid, _title(plan, it.sid), it.source_label,
                     genre_name(it.genre), _stars_text(it.stars), gauge, prev))
    return rows


def plan_skipped_rows(plan) -> list:
    """(id, title, source, reason, duplicates) for every song left out."""
    rows = []
    for sid, labels in plan.skipped_dupes.items():
        for lb in labels:
            rows.append((sid, _title(plan, sid), lb,
                         tr("same id already added"), sid))
    for sid, (lb, title, other) in plan.skipped_titles.items():
        rows.append((sid, title or _title(plan, sid), lb,
                     tr("same title as another id"), other))
    return rows


def plan_fix_rows(plan) -> list:
    """(id, title, what is corrected, new value) for songs already in the target."""
    est = getattr(plan, "play_estimated", None) or set()
    rows = []
    for sid, g in plan.genre_fixes.items():
        rows.append((sid, _title(plan, sid), tr("genre"), genre_name(g)))
    for sid, st in plan.star_fixes.items():
        rows.append((sid, _title(plan, sid), tr("stars"), _stars_text(st)))
    for sid in plan.play_fixes:
        rows.append((sid, _title(plan, sid), tr("gauge"),
                     tr("estimated from chart") if sid in est else tr("from source")))
    for sid, ms in plan.preview_fixes.items():
        rows.append((sid, _title(plan, sid), tr("preview start"), f"{ms} ms"))
    return rows


def plan_csv_rows(plan) -> list:
    """Whole plan as flat rows for a CSV (header first)."""
    out = [[tr("Section"), tr("ID"), tr("Title"), tr("Source"), tr("Folder"),
            tr("Stars"), tr("Gauge"), tr("Preview"), tr("Detail")]]
    sec = tr("Added")
    for sid, title, src, gen, st, ga, pv in plan_song_rows(plan):
        out.append([sec, sid, title, src, gen, st, ga, pv, ""])
    sec = tr("Skipped")
    for sid, title, src, why, other in plan_skipped_rows(plan):
        out.append([sec, sid, title, src, "", "", "", "", f"{why}: {other}"])
    sec = tr("Corrected")
    for sid, title, what, val in plan_fix_rows(plan):
        out.append([sec, sid, title, "", "", "", "", "", f"{what}: {val}"])
    return out


def write_plan_csv(plan, path) -> int:
    import csv
    rows = plan_csv_rows(plan)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)
    return len(rows) - 1


class PlanDetailsDialog(QDialog):
    """Read-only view of what the build will do: added songs, skipped ones,
    and corrections to songs already in the target."""

    def __init__(self, plan, specs=(), parent=None, source=""):
        super().__init__(parent)
        self.setWindowTitle(tr("Omnimix plan details"))
        self.resize(900, 560)
        self._plan = plan
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.lbl_filter = QLabel(tr("filter:"))
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText(tr("id, title, folder…"))
        self.lbl_filter.setBuddy(self.ed_filter)
        self.lbl_source = QLabel(tr("source:"))
        self.cb_source = QComboBox()
        self.cb_source.addItem(tr("(all sources)"), "")
        labels = [getattr(sp, "label", sp) for sp in specs]
        for it in plan.items:
            if it.source_label not in labels:
                labels.append(it.source_label)
        for lb in labels:
            self.cb_source.addItem(lb, lb)
        self.lbl_source.setBuddy(self.cb_source)
        top.addWidget(self.lbl_filter); top.addWidget(self.ed_filter, 1)
        top.addWidget(self.lbl_source); top.addWidget(self.cb_source)
        lay.addLayout(top)

        self.tabs = QTabWidget()
        self.tabs.setUsesScrollButtons(False)
        self._specs = [
            (plan_song_rows(plan), 2, tr("Songs added"),
             [tr("ID"), tr("Title"), tr("Source"), tr("Folder"), tr("Stars"),
              tr("Gauge"), tr("Preview")]),
            (plan_skipped_rows(plan), 2, tr("Skipped"),
             [tr("ID"), tr("Title"), tr("Source"), tr("Reason"), tr("Duplicates")]),
            (plan_fix_rows(plan), None, tr("Corrections to target songs"),
             [tr("ID"), tr("Title"), tr("Corrected"), tr("New value")]),
        ]
        self.tables = []
        for rows, _src, name, heads in self._specs:
            t = QTableWidget(len(rows), len(heads))
            t.setHorizontalHeaderLabels(heads)
            t.setEditTriggers(QTableWidget.NoEditTriggers)
            t.setSelectionBehavior(QTableWidget.SelectRows)
            t.horizontalHeader().setStretchLastSection(True)
            t.verticalHeader().setVisible(False)
            for r, row in enumerate(rows):
                for c, v in enumerate(row):
                    t.setItem(r, c, QTableWidgetItem(str(v)))
            t.setSortingEnabled(True)
            t.resizeColumnsToContents()
            self.tables.append(t)
            self.tabs.addTab(t, name)
        lay.addWidget(self.tabs, 1)
        self.lbl_count = QLabel("")
        lay.addWidget(self.lbl_count)

        btns = QHBoxLayout(); btns.addStretch(1)
        b_csv = self.b_csv = QPushButton(tr("Export CSV…"))
        b_csv.clicked.connect(self._export)
        b_close = self.b_close = QPushButton(tr("Close"))
        b_close.clicked.connect(self.accept)
        btns.addWidget(b_csv); btns.addWidget(b_close)
        lay.addLayout(btns)

        self.ed_filter.textChanged.connect(self._apply)
        self.cb_source.currentIndexChanged.connect(self._apply)
        i = self.cb_source.findData(source) if source else 0
        self.cb_source.setCurrentIndex(max(i, 0))
        self._apply()
        polish(self, primary=b_close)

    def _apply(self, *_):
        needle = self.ed_filter.text().strip().casefold()
        src = self.cb_source.currentData() or ""
        parts = []
        for t, (rows, scol, name, _h) in zip(self.tables, self._specs):
            shown = 0
            for r in range(t.rowCount()):
                cells = [t.item(r, c).text() for c in range(t.columnCount())]
                ok = (not src or scol is None or cells[scol] == src) and \
                    (not needle or any(needle in v.casefold() for v in cells))
                t.setRowHidden(r, not ok)
                shown += ok
            parts.append(f"{name}: {shown}/{len(rows)}")
        self.lbl_count.setText("   ".join(parts))

    def _export(self):
        path = appconfig.pick_save(self, "omnimixplan", tr("Export plan as CSV"),
                                   "omnimix_plan.csv", "CSV (*.csv);;All (*)")
        if not path:
            return
        try:
            n = write_plan_csv(self._plan, path)
        except OSError as exc:
            QMessageBox.critical(self, tr("Export plan as CSV"), str(exc))
            return
        QMessageBox.information(self, tr("Export plan as CSV"),
                                tr("Wrote {n} rows to {path}.").format(n=n, path=path))


def fmt_size(n: int) -> str:
    return f"{n / (1 << 20):,.0f} MiB" if n < (1 << 30) else f"{n / (1 << 30):.2f} GiB"


class _Worker(QThread):
    log_sig = Signal(str)
    done_sig = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done_sig.emit(self._fn(lambda s: self.log_sig.emit(s)))
        except Exception as exc:
            import traceback
            self.done_sig.emit(("ERROR", exc, traceback.format_exc()))


def _pick_partition(parent, img):
    try:
        parts = OM.taiko_partitions(img)
    except Exception as exc:
        QMessageBox.critical(parent, tr("Open .img failed"), str(exc))
        return None
    if not parts:
        QMessageBox.warning(parent, tr("No partitions"), tr("No PFS partitions found."))
        return None
    # T14 song data lives in t14jp*.0001; default to it when present.
    default = next((p for p in parts if p.endswith(".0001")), parts[0])
    part, ok = QInputDialog.getItem(parent, tr("Choose partition"),
                                    f"{Path(img).name}:", parts,
                                    parts.index(default), False)
    return part if ok else None


class OmnimixDialog(QDialog):
    """Assemble an omnimix into a target image."""

    def __init__(self, parent=None, default_img=""):
        super().__init__(parent)
        self.setWindowTitle(tr("Omnimix Maker — fuse songs from many images into one"))
        self.resize(840, 900)
        self._sources = []           # list of location dicts
        self._tmproot = None
        self._plan = None
        self._db = None
        self._target_pair = None     # (list_path, data_path) after extraction
        self._worker = None
        self._build_ui()
        self._load_settings()
        if default_img:
            self.ed_timg.setText(default_img)
        self.ed_timg.textChanged.connect(self._target_edited)
        polish(self, primary=self.b_prev)

    # -- ui -------------------------------------------------------------------
    def _build_ui(self):
        lay = QVBoxLayout(self)
        note = QLabel(tr(
            "Fuses the songs of several Taiko images into one. Only songs the "
            "TARGET does not already have are added (dedup by id; the target is "
            "the base). Charts, textures, audio and stars are copied as-is. "
            "⚠ Close PCSX2 first, and work on a COPY — the .img is written in place."))
        note.setWordWrap(True); note.setStyleSheet("color:palette(mid);")
        lay.addWidget(note)

        # --- target ---
        gt = self.gb_target = QGroupBox(tr("Target (merge INTO this — the base)"))
        tf = QFormLayout(gt)
        self.cb_tkind = QComboBox(); self.cb_tkind.addItems(
            [tr("HDD .img (Taiko 14+)"), tr("Folder (DATA.000 + list.bin)")])
        self.cb_tkind.currentIndexChanged.connect(self._sync_target)
        tf.addRow(tr("target type:"), self.cb_tkind)
        self.ed_timg = QLineEdit(); bt = QPushButton("…"); bt.clicked.connect(self._pick_target)
        tf.addRow(tr("target path:"), self._row(self.ed_timg, bt))
        self.ed_tpart = QLineEdit(); self.ed_tpart.setPlaceholderText(tr("(auto — chosen on Preview)"))
        self.ed_tpart.setReadOnly(True)
        self.lbl_tpart = QLabel(tr("partition:"))
        tf.addRow(self.lbl_tpart, self.ed_tpart)
        lay.addWidget(gt)

        # --- sources ---
        gs = self.gb_sources = QGroupBox(tr("Sources (harvest FROM these — first one wins on ties)"))
        sv = QVBoxLayout(gs)
        self.lst = QListWidget(); self.lst.setMinimumHeight(70)
        self.lst.setToolTip(tr("Order matters: the first source wins when two have the same song."))
        sv.addWidget(self.lst)
        sb = QHBoxLayout()
        for txt, fn in ((tr("Add .img…"), self._add_img), (tr("Add ISO…"), self._add_iso),
                        (tr("Add folder…"), self._add_folder),
                        (tr("Up"), lambda: self._move(-1)), (tr("Down"), lambda: self._move(1)),
                        (tr("Remove"), self._remove)):
            b = QPushButton(txt); b.clicked.connect(fn); sb.addWidget(b)
        sb.addStretch(1)
        from taiko2x6.app import guide
        help_btn = guide.help_button(
            self, tr("Omnimix: sources and options"),
            tr("The target is the base: only songs it does not already have are "
               "added, matched by song id.\n\n"
               "Source order: sources are read top to bottom, and when two have "
               "the same song id the one higher in the list wins. Use Up / Down "
               "to reorder.\n\n"
               "More than 214 songs: “Fit everything” grows the game's song "
               "tables and patches the exe ceiling (up to 644); records of songs "
               "past 210 are not kept across a reboot. “Cap at 214” stops at 214.\n\n"
               "Song audio: DATA.000 must stay under 2 GiB or the game hangs on "
               "LOADING (about 430 songs at 44.1 kHz). 30 or 24 kHz keeps speed "
               "and pitch, with less treble.\n\n"
               "Backup: ticked, the target .img is copied to <img>.omnimix.bak "
               "before writing; unticked, an undo journal is kept instead.\n\n"
               "Game patches: ticked ones are applied to `taiko` after the build; "
               "unticked ones are left as they are. Preview plan writes nothing."),
            self.lst)
        sb.addWidget(help_btn)
        sv.addLayout(sb)
        lay.addWidget(gs)

        # --- options ---
        go = self.gb_options = QGroupBox(tr("Options"))
        of = QFormLayout(go)
        self.cb_over = QComboBox(); self.cb_over.addItems([
            tr("Fit everything, patch the ceiling (up to 644)"),
            tr("Cap at 214 songs")])
        of.addRow(tr("when songs exceed 214:"), self.cb_over)
        self.cb_audio = QComboBox(); self.cb_audio.addItems([
            tr("keep original (44.1 kHz)"),
            tr("re-encode all songs at 30 kHz (fits ~600 songs)"),
            tr("re-encode all songs at 24 kHz")])
        self.cb_audio.setToolTip(tr(
            "DATA.000 must stay under 2 GiB or the game hangs on LOADING. "
            "At 44.1 kHz that is about 430 songs. Lower rates keep speed and "
            "pitch (the game reads the rate from each song), only less treble."))
        of.addRow(tr("song audio:"), self.cb_audio)
        self.ck_backup = QCheckBox(tr("back up the target .img before writing")); self.ck_backup.setChecked(True)
        of.addRow("", self.ck_backup)
        self.ck_limit = QCheckBox(tr("patch the song-count ceiling in the exe (.img target)"))
        self.ck_limit.setChecked(True)
        of.addRow("", self.ck_limit)
        lay.addWidget(go)
        for w in (self.cb_over, self.cb_audio, self.ck_backup):
            w.setWhatsThis(help_btn.whatsThis())

        # --- boot / select patches, applied to `taiko` after the build ---
        from taiko2x6.exe import taiko_exe
        gp = self.gb_patches = QGroupBox(tr("Game patches applied after the build (.img target)"))
        gl = QVBoxLayout(gp)
        self.ck_boot = {}
        for key, (label, *_fns) in taiko_exe.BOOT_PATCHES.items():
            cb = QCheckBox(tr(label))
            cb.setToolTip(tr(taiko_exe.TaikoBootDialog.TIPS.get(key, "")))
            cb.setChecked(key == "freeze_select_timer")
            gl.addWidget(cb)
            self.ck_boot[key] = cb
        hint = QLabel(tr("Unticked ones are left as they are. Change them later "
                         "under Tools ▸ taiko executable patches."))
        hint.setWordWrap(True); hint.setStyleSheet("color:palette(mid);")
        gl.addWidget(hint)
        lay.addWidget(gp)

        # --- plan summary (filled after Preview) ---
        self.tbl = QTableWidget(0, 3)
        self.tbl.setHorizontalHeaderLabels(
            [tr("Source"), tr("Songs added"), tr("Duplicates skipped")])
        self.tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setMaximumHeight(150)
        self.tbl.setToolTip(tr("Double-click a source to see its songs in the plan details."))
        self.tbl.itemDoubleClicked.connect(self._open_details)
        self.tbl.setVisible(False)
        self.lbl_size = QLabel(""); self.lbl_size.setWordWrap(True)
        self.lbl_size.setVisible(False)
        lay.addWidget(self.tbl); lay.addWidget(self.lbl_size)

        # --- progress + log ---
        prow = QHBoxLayout()
        self.progress = QProgressBar(); self.progress.setRange(0, 1); self.progress.setValue(0)
        self.progress.setTextVisible(True); self.progress.setFormat("")
        self.lbl_stage = QLabel(tr("idle")); self.lbl_stage.setMinimumWidth(260)
        self.lbl_time = QLabel("")
        prow.addWidget(self.progress, 1); prow.addWidget(self.lbl_stage); prow.addWidget(self.lbl_time)
        lay.addLayout(prow)
        self._clock = QElapsedTimer()
        self._tick = QTimer(self); self._tick.setInterval(1000)
        self._tick.timeout.connect(self._update_time)

        self.log = QPlainTextEdit(readOnly=True)
        self.log.setFont(QFont("Consolas", 9))
        self.log.setMinimumHeight(110)
        self.log.setMaximumBlockCount(20000)
        self.log.setLineWrapMode(QPlainTextEdit.NoWrap)
        lay.addWidget(QLabel(tr("log:"))); lay.addWidget(self.log, 1)

        btns = QHBoxLayout(); btns.addStretch(1)
        self.b_prev = QPushButton(tr("Preview plan")); self.b_prev.clicked.connect(self._preview)
        self.b_build = QPushButton(tr("Build Omnimix")); self.b_build.clicked.connect(self._build)
        self.b_build.setEnabled(False)
        self.b_details = QPushButton(tr("Plan details…"))
        self.b_details.setToolTip(tr("Every song the build adds, skips or corrects, with its source."))
        self.b_details.clicked.connect(self._open_details)
        self.b_details.setEnabled(False)
        self.b_repair = QPushButton(tr("Repair existing Omnimix"))
        self.b_repair.setToolTip(tr(
            "Brings an Omnimix built with an older version up to date without "
            "rebuilding it: trims the select title textures (the 2nd song of a "
            "credit no longer hangs) and, with the same sources as the build "
            "(stock TK14 first), fixes gauge values and preview starts."))
        self.b_repair.clicked.connect(self._repair)
        b_close = QPushButton(tr("Close")); b_close.clicked.connect(self.reject)
        btns.addWidget(self.b_repair)
        btns.addWidget(self.b_details)
        btns.addWidget(self.b_prev); btns.addWidget(self.b_build); btns.addWidget(b_close)
        lay.addLayout(btns)
        self._sync_target()

    def _row(self, w, b):
        c = QWidget(); h = QHBoxLayout(c); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(w, 1); h.addWidget(b); return c

    # -- target helpers -------------------------------------------------------
    def _target_is_img(self):
        return self.cb_tkind.currentIndex() == 0

    def _sync_target(self):
        img = self._target_is_img()
        self.ed_tpart.setVisible(img); self.lbl_tpart.setVisible(img)
        self.ck_limit.setEnabled(img)
        self.ck_backup.setEnabled(img)
        for cb in getattr(self, "ck_boot", {}).values():
            cb.setEnabled(img)
        if hasattr(self, "tbl"):
            self._invalidate_plan()

    def _pick_target(self):
        if self._target_is_img():
            p = appconfig.pick_open(self, "hddimg", tr("Target PS2 HDD image"),
                                    "HDD image (*.img *.raw *.bin);;All (*)")
            if p:
                self.ed_timg.setText(p); self.ed_tpart.clear()
        else:
            d = QFileDialog.getExistingDirectory(self, tr("Target folder (DATA.000 + list.bin)"))
            if d:
                self.ed_timg.setText(d)

    # -- sources --------------------------------------------------------------
    def _add_img(self):
        p = appconfig.pick_open(self, "hddimg", tr("Source PS2 HDD image"),
                                "HDD image (*.img *.raw *.bin);;All (*)")
        if not p:
            return
        part = _pick_partition(self, p)
        if not part:
            return
        label, ok = QInputDialog.getText(self, tr("Label"),
                                         tr("Name this source (e.g. {ex}):").format(ex="Taiko 12"),
                                         text=Path(p).stem)
        if not ok:
            return
        self._sources.append({"kind": "img", "img": p, "partition": part,
                              "label": label or Path(p).stem})
        self._refresh_sources()

    def _add_iso(self):
        p = appconfig.pick_open(self, "gameiso", tr("Source game ISO (DATA.000 + LIST.BIN)"),
                                "PS2 game ISO (*.iso *.bin);;All (*)")
        if not p:
            return
        # validate it holds an arcade pair before accepting it
        try:
            with open(p, "rb") as f:
                recs = OM._iso_root_records(f)
            if "DATA.000" not in recs or "LIST.BIN" not in recs:
                QMessageBox.warning(self, tr("Not a Taiko game ISO"),
                                    tr("That ISO has no DATA.000 + LIST.BIN in its root."))
                return
        except Exception as exc:
            QMessageBox.critical(self, tr("Open ISO failed"), str(exc))
            return
        label, ok = QInputDialog.getText(self, tr("Label"),
                                         tr("Name this source (e.g. {ex}):").format(ex="Taiko 9"),
                                         text=Path(p).stem)
        if not ok:
            return
        self._sources.append({"kind": "iso", "iso": p,
                              "label": label or Path(p).stem})
        self._refresh_sources()

    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, tr("Source folder (DATA.000 + list.bin)"))
        if not d:
            return
        dp = Path(d) / "DATA.000"; lp = Path(d) / "list.bin"
        if not dp.exists() or not lp.exists():
            QMessageBox.warning(self, tr("Not a game folder"),
                                tr("That folder has no DATA.000 + list.bin."))
            return
        label, ok = QInputDialog.getText(self, tr("Label"),
                                         tr("Name this source (e.g. {ex}):").format(ex="Taiko 8"),
                                         text=Path(d).name)
        if not ok:
            return
        self._sources.append({"kind": "pair", "list": str(lp), "data": str(dp),
                              "label": label or Path(d).name})
        self._refresh_sources()

    def _move(self, delta):
        r = self.lst.currentRow()
        if r < 0:
            return
        j = r + delta
        if 0 <= j < len(self._sources):
            self._sources[r], self._sources[j] = self._sources[j], self._sources[r]
            self._refresh_sources(); self.lst.setCurrentRow(j)

    def _remove(self):
        r = self.lst.currentRow()
        if r >= 0:
            del self._sources[r]; self._refresh_sources()

    def _refresh_sources(self):
        self.lst.clear()
        for i, s in enumerate(self._sources):
            if s["kind"] == "img":
                where = f"{Path(s['img']).name}:{s['partition']}"
            elif s["kind"] == "iso":
                where = Path(s["iso"]).name
            else:
                where = Path(s["data"]).parent.name
            self.lst.addItem(QListWidgetItem(f"{i+1}. {s['label']}  —  {where}"))
        self._invalidate_plan()            # plan is stale after edits
        self._save_settings()

    def _invalidate_plan(self):
        self._plan = None; self._db = None
        self.b_build.setEnabled(False); self.b_details.setEnabled(False)
        self.tbl.setVisible(False); self.lbl_size.setVisible(False)

    def _target_edited(self, _text=""):
        self.ed_tpart.clear()
        self._invalidate_plan()

    # -- remembered settings --------------------------------------------------
    def _load_settings(self):
        g = lambda k, d="": appconfig.get(_CFG, k, d)
        self.cb_tkind.setCurrentIndex(1 if g("target_kind", "0") == "1" else 0)
        self.ed_timg.setText(g("target"))
        if self._target_is_img():
            self.ed_tpart.setText(g("target_part"))
        try:
            srcs = json.loads(g("sources", "[]"))
        except ValueError:
            srcs = []
        need = {"img": ("img", "partition"), "iso": ("iso",), "pair": ("list", "data")}
        if isinstance(srcs, list):
            self._sources = [s for s in srcs if isinstance(s, dict)
                             and s.get("kind") in need and s.get("label")
                             and all(k in s for k in need[s["kind"]])]
        self.cb_over.setCurrentIndex(int(g("over", "0") == "1"))
        a = g("audio", "0")
        self.cb_audio.setCurrentIndex(int(a) if a in ("0", "1", "2") else 0)
        self.ck_backup.setChecked(g("backup", "1") != "0")
        self.ck_limit.setChecked(g("limit", "1") != "0")
        if appconfig.get(_CFG, "target_kind") != "":     # saved at least once
            keys = g("boot").split(",")
            for k, cb in self.ck_boot.items():
                cb.setChecked(k in keys)
        self._refresh_sources()

    def _save_settings(self):
        s = appconfig.set_value
        s(_CFG, "target_kind", self.cb_tkind.currentIndex())
        s(_CFG, "target", self.ed_timg.text().strip())
        s(_CFG, "target_part", self.ed_tpart.text().strip())
        s(_CFG, "sources", json.dumps(self._sources, ensure_ascii=False))
        s(_CFG, "over", self.cb_over.currentIndex())
        s(_CFG, "audio", self.cb_audio.currentIndex())
        s(_CFG, "backup", int(self.ck_backup.isChecked()))
        s(_CFG, "limit", int(self.ck_limit.isChecked()))
        s(_CFG, "boot", ",".join(k for k, cb in self.ck_boot.items() if cb.isChecked()))

    # -- pipeline: extraction -------------------------------------------------
    def _validate(self):
        err = input_error(self.ed_timg.text(), self._target_is_img(), self._sources)
        if err:
            QMessageBox.warning(self, tr("Omnimix Maker"), err); return False
        self._save_settings()
        return True

    def _extract_all(self, log):
        """Worker: extract every .img (sources + target) to temp pairs.
        Returns (resolved_source_specs, target_list, target_data, target_part)."""
        self._tmproot = Path(tempfile.mkdtemp(prefix="omnimix_"))
        specs = []
        for i, s in enumerate(self._sources):
            if s["kind"] == "img":
                lp, dp = OM.extract_partition(s["img"], s["partition"],
                                              self._tmproot / f"src{i}", log)
            elif s["kind"] == "iso":
                lp, dp = OM.iso_extract_pair(s["iso"], self._tmproot / f"src{i}", log)
            else:
                lp, dp = s["list"], s["data"]
            specs.append(OM.SourceSpec(s["label"], str(lp), str(dp)))
        tpart = None
        if self._target_is_img():
            tpart = self.ed_tpart.text().strip()
            tl, td = OM.extract_partition(self.ed_timg.text().strip(), tpart,
                                          self._tmproot / "target", log)
        else:
            base = Path(self.ed_timg.text().strip())
            tl, td = base / "list.bin", base / "DATA.000"
        return specs, str(tl), str(td), tpart

    def _preview(self):
        if not self._validate():
            return
        if self._target_is_img() and not self.ed_tpart.text().strip():
            part = _pick_partition(self, self.ed_timg.text().strip())
            if not part:
                return
            self.ed_tpart.setText(part)
        self.log.clear(); self.log.appendPlainText(tr("Extracting images (large, ~1 min each)…"))
        self._start(self._extract_all, self._after_extract, tr("Extracting images…"))

    def _after_extract(self, res):
        if self._is_err(res):
            return
        specs, tl, td, tpart = res
        self._specs = specs
        self._target_pair = (tl, td)
        self._tpart = tpart
        # GUI-thread: plan + precompute DB (SongManager is a QWidget)
        try:
            target = OM.open_pair(tl, td)
            try:
                self.log.appendPlainText("planning merge…")
                self._plan = OM.plan_merge(target, specs,
                                           log=self.log.appendPlainText)
                self._db = OM.precompute_db(target, self._plan)
            finally:
                target.close()
        except Exception as exc:
            import traceback
            self.log.appendPlainText("PLAN FAILED:\n" + traceback.format_exc())
            QMessageBox.critical(self, tr("Plan failed"), str(exc))
            self._end(tr("failed")); return
        p = self._plan
        dupes = sum(len(v) for v in p.skipped_dupes.values())
        msg = (f"Target base: {len(p.target_ids)} songs\n"
               f"To add:      {len(p.items)} songs\n"
               f"TOTAL after: {p.total_after} songs"
               + (f"\nCross-source duplicate ids skipped: {dupes}" if dupes else ""))
        if p.total_after > OM.SAFE_LIMIT:
            msg += (f"\n\nNote: {p.total_after} is above 214, so the per-song "
                    "record tables are grown too (max 644). Records of songs past "
                    "210 are kept until power-off; the first 210 are saved to SRAM.")
        if p.items:
            msg += "\n\n" + tr("Songs to add by folder:") + "\n" + "\n".join(
                f"  {name:<12} {n:>4}" for name, n in genre_summary(p.items))
        self.log.appendPlainText("\n" + msg)
        self._show_summary(p, specs, td)
        self.b_build.setEnabled(len(p.items) > 0)
        self.b_details.setEnabled(True)
        self._end(tr("plan ready"))
        QMessageBox.information(self, tr("Plan ready"), msg
                                + "\n\n" + tr("Click “Build Omnimix” to assemble."))

    def _open_details(self, *_):
        if not self._plan:
            return
        row = self.tbl.currentRow()
        it = self.tbl.item(row, 0) if row >= 0 and self.tbl.isVisible() else None
        d = PlanDetailsDialog(self._plan, getattr(self, "_specs", ()), self,
                              source=it.text() if it else "")
        d.exec()
        d.deleteLater()

    def _show_summary(self, plan, specs, data_path):
        rows = source_summary(plan, specs)
        self.tbl.setRowCount(len(rows))
        for r, (lb, a, sk) in enumerate(rows):
            for c, v in enumerate((lb, str(a), str(sk))):
                it = QTableWidgetItem(v)
                if c:
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.tbl.setItem(r, c, it)
        self.tbl.setVisible(True)
        try:
            cur = os.path.getsize(data_path)
            self.lbl_size.setText(tr(
                "Target DATA.000 now: {cur} of the {limit} limit ({pct}). The added songs "
                "come on top; re-encode the audio if the total would pass the limit.").format(
                    cur=fmt_size(cur), limit=fmt_size(OM.MAX_DATA_SIZE),
                    pct=f"{cur / OM.MAX_DATA_SIZE:.0%}"))
            self.lbl_size.setVisible(True)
        except OSError:
            self.lbl_size.setVisible(False)

    # -- pipeline: build ------------------------------------------------------
    def _build(self):
        if not self._plan or not self._db:
            QMessageBox.warning(self, tr("Omnimix Maker"), tr("Run Preview plan first.")); return
        cap = self.cb_over.currentIndex() == 1
        if self._plan.total_after > OM.SAFE_LIMIT and not cap:
            if self._plan.total_after > OM.taiko_exe_max():
                QMessageBox.warning(
                    self, tr("Too many songs"),
                    f"{self._plan.total_after} songs is above the maximum "
                    f"{OM.taiko_exe_max()} the game executable can be patched for.")
                return
            if QMessageBox.question(
                    self, tr("Above 214 songs"),
                    f"This builds {self._plan.total_after} songs. The game's "
                    "song tables will be grown to match; records of songs past "
                    "210 are not kept across a reboot. Continue?") != \
                    QMessageBox.Yes:
                return
        tl, td = self._target_pair
        specs, db, plan = self._specs, self._db, self._plan
        is_img = self._target_is_img()
        img = self.ed_timg.text().strip(); tpart = self._tpart
        do_backup = self.ck_backup.isChecked() and is_img
        do_limit = self.ck_limit.isChecked() and is_img
        audio_rate = (None, 30000, 24000)[self.cb_audio.currentIndex()]
        boot = {k: True for k, cb in self.ck_boot.items() if cb.isChecked()} if is_img else {}
        added_by_genre = genre_summary(plan.items)

        def task(log):
            target = OM.open_pair(tl, td)
            try:
                t0 = time.perf_counter()
                res = OM.assemble(target, specs, plan, db, log=log,
                                  audio_rate=audio_rate)
            finally:
                target.close()
            log("assembled %d songs (total %d) in %.1fs"
                % (res["added"], res["total"], time.perf_counter() - t0))
            if res["errors"]:
                log("errors: " + "; ".join(res["errors"]))
            if is_img:
                if do_backup:
                    bak = img + ".omnimix.bak"
                    log(f"backing up target → {Path(bak).name} (copying, large)…")
                    shutil.copy2(img, bak)
                # a fresh full copy already exists -> skip the undo journal
                OM.write_partition_growing(img, tpart, res["list"], res["data"],
                                           log, undo=not do_backup)
                if do_limit:
                    cap_safe = (self.cb_over.currentIndex() == 1)
                    ceil = OM.patch_song_limit(img, res["total"],
                                               cap_safe=cap_safe, log=log)
                    res["ceiling"] = ceil
                if boot:
                    from taiko2x6.exe import taiko_exe
                    log("applying game patches: " + ", ".join(boot))
                    taiko_exe.set_hdd_boot(img, boot, log=log)
                    res["boot"] = list(boot)
            else:
                write_pair([(td, res["data"]), (tl, res["list"])], backup=True)
                log(f"wrote merged pair → {Path(td).parent}")
            res["by_genre"] = added_by_genre
            return res

        if not self._confirm_write(is_img, img, tpart):
            return
        self.log.appendPlainText("\n" + tr("Building — this rewrites the whole archive "
                                            "and (for .img) writes it back. Please wait…"))
        self._start(task, self._after_build, tr("Building…"))

    def _repair(self):
        if not self._target_is_img() or not self.ed_timg.text().strip():
            QMessageBox.warning(self, tr("Omnimix Maker"),
                                tr("Repair works on a HDD .img target. Choose one."))
            return
        img = self.ed_timg.text().strip()
        if not os.path.isfile(img):
            QMessageBox.warning(self, tr("Omnimix Maker"),
                                tr("The target image does not exist:") + "\n" + img)
            return
        errs = source_path_errors(self._sources)
        if errs:
            QMessageBox.warning(self, tr("Omnimix Maker"), "\n".join(errs)); return
        if not self.ed_tpart.text().strip():
            part = _pick_partition(self, img)
            if not part:
                return
            self.ed_tpart.setText(part)
        tpart = self.ed_tpart.text().strip()
        sources = list(self._sources)
        note = (tr("Sources: {n} (gauge values and preview starts are fixed too).").format(
                    n=len(sources)) if sources else
                tr("No sources added: only the title textures are trimmed."))
        if QMessageBox.question(
                self, tr("Repair existing Omnimix"),
                tr("Repair partition {part} of\n{img}\nin place?").format(part=tpart, img=img)
                + "\n\n" + note) != QMessageBox.Yes:
            return

        def task(log):
            self._tmproot = Path(tempfile.mkdtemp(prefix="omnimix_"))
            specs = []
            for i, s in enumerate(sources):
                if s["kind"] == "img":
                    lp, dp = OM.extract_partition(s["img"], s["partition"],
                                                  self._tmproot / f"src{i}", log)
                elif s["kind"] == "iso":
                    lp, dp = OM.iso_extract_pair(s["iso"], self._tmproot / f"src{i}", log)
                else:
                    lp, dp = s["list"], s["data"]
                specs.append(OM.SourceSpec(s["label"], str(lp), str(dp)))
            return {"written": OM.repair_image(img, tpart, specs,
                                               self._tmproot / "target", log)}

        self.log.clear()
        self._start(task, self._after_repair, tr("Repairing…"))

    def _after_repair(self, res):
        if self._is_err(res):
            return
        msg = (tr("Repaired and written back.") if res["written"]
               else tr("Nothing to repair: this Omnimix is up to date."))
        self.log.appendPlainText("\n" + msg)
        self._cleanup_tmp()
        self._end(tr("done"))
        QMessageBox.information(self, tr("Repair existing Omnimix"), msg)

    def _confirm_write(self, is_img, img, tpart):
        where = (f"partition {tpart} of\n{img}" if is_img
                 else f"{self.ed_timg.text().strip()}")
        return QMessageBox.question(
            self, tr("Write?"),
            tr("Add {n} songs → {total} total, then write into:\n{where}\n\nProceed?").format(
                n=len(self._plan.items), total=self._plan.total_after, where=where)
        ) == QMessageBox.Yes

    def _after_build(self, res):
        if self._is_err(res):
            return
        self.changed = True
        ceil = res.get("ceiling")
        msg = tr("Done — added {added} songs, total {total}.").format(
            added=res["added"], total=res["total"])
        if ceil:
            msg += "\n" + tr("Exe song ceiling patched to {n}.").format(n=ceil)
        if res.get("boot"):
            from taiko2x6.exe import taiko_exe
            msg += "\n" + tr("Game patches on:") + " " + ", ".join(
                tr(taiko_exe.BOOT_PATCHES[k][0]) for k in res["boot"])
        if res.get("by_genre"):
            msg += "\n\n" + tr("Added by folder:") + "\n" + "\n".join(
                f"  {name:<12} {n:>4}" for name, n in res["by_genre"])
        if res["warnings"]:
            msg += "\n\n" + tr("{n} warnings (see log).").format(n=len(res["warnings"]))
        for w in res["warnings"]:
            self.log.appendPlainText("WARNING: " + w)
        self.log.appendPlainText("\n" + msg)
        self._cleanup_tmp()
        self._end(tr("done"))
        QMessageBox.information(self, tr("Omnimix built"), msg)

    # -- worker plumbing ------------------------------------------------------
    def _start(self, fn, on_done, stage):
        self._begin(stage)
        self._worker = _Worker(fn)
        self._worker.log_sig.connect(self._on_log)
        self._worker.done_sig.connect(on_done)
        self._worker.start()

    def _begin(self, stage=""):
        for b in (self.b_prev, self.b_build, self.b_repair):
            b.setEnabled(False)
        for g in (self.gb_target, self.gb_sources, self.gb_options, self.gb_patches):
            g.setEnabled(False)
        self.progress.setRange(0, 0)          # busy until a "n/total" line shows up
        self.progress.setFormat("")
        self.lbl_stage.setText(stage)
        self._clock.start(); self._tick.start(); self._update_time()

    def _end(self, stage=""):
        self.b_prev.setEnabled(True)
        self.b_repair.setEnabled(True)
        for g in (self.gb_target, self.gb_sources, self.gb_options, self.gb_patches):
            g.setEnabled(True)
        self._tick.stop(); self._update_time()
        self.progress.setRange(0, 1); self.progress.setValue(1 if stage == tr("done") else 0)
        self.progress.setFormat("")
        self.lbl_stage.setText(stage)

    def _on_log(self, line: str):
        self.log.appendPlainText(line)
        text = line.strip()
        m = _COUNT_RE.search(text)
        if m and int(m.group(2)) > 0:
            done, total = int(m.group(1)), int(m.group(2))
            self.progress.setRange(0, total); self.progress.setValue(min(done, total))
            self.progress.setFormat("%v / %m")
        elif text and not text.startswith(" "):
            self.progress.setRange(0, 0)       # a new phase: busy again
        if text:
            self.lbl_stage.setText(text[:60] + ("…" if len(text) > 60 else ""))

    def _update_time(self):
        if not self._clock.isValid():
            return
        sec = self._clock.elapsed() // 1000
        self.lbl_time.setText(f"{sec // 60}:{sec % 60:02d}")

    def _is_err(self, res):
        if isinstance(res, tuple) and res and res[0] == "ERROR":
            self.log.appendPlainText("FAILED:\n" + res[2])
            QMessageBox.critical(self, tr("Omnimix failed"), str(res[1]))
            self._cleanup_tmp(); self._end(tr("failed"))
            return True
        return False

    def _cleanup_tmp(self):
        if self._tmproot and self._tmproot.exists():
            shutil.rmtree(self._tmproot, ignore_errors=True)
        self._tmproot = None

    def _busy(self):
        return self._worker is not None and self._worker.isRunning()

    def done(self, r):
        if self._busy():
            QMessageBox.information(self, tr("Omnimix Maker"),
                                    tr("A job is still running. Wait for it to finish."))
            return
        self._save_settings()
        super().done(r)

    def reject(self):
        if self._busy():
            return self.done(0)
        self._cleanup_tmp()
        super().reject()

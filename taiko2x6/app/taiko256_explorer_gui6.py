#!/usr/bin/env python3
"""
Taiko SYSTEM256 archive explorer - PySide6 GUI.

A file-explorer style front-end for LIST.BIN + DATA.000 Taiko archives.
It reuses the proven core from taiko256_archive_tool_v2.py:

  * browse groups/files as a tree (folders built from the dotted group names)
  * preview / read any file directly out of DATA.000 (decoded on demand)
  * multi-threaded "Extract All" / "Extract Selected"
  * replace files in place and save back to DATA.000 + LIST.BIN
    (conservative slot-preserving patch, hardware safe; optional relayout)

Run:
    python main.py
    python main.py "D:/path/to/extract"
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

# Console logger. Level via env TAIKO_LOG=DEBUG (or --debug); configured in main().
log = logging.getLogger("taiko")

# --- import the proven core logic from the archive CLI tool --------------------
from taiko2x6.archive import taiko256_archive_tool_v2 as core  # noqa: E402
from taiko2x6.graphics import tim2  # noqa: E402  TIM2 (.nut) image decoding
from taiko2x6.app import appconfig  # noqa: E402  last-used-path memory (config.ini)
from taiko2x6.app import i18n  # noqa: E402  zh-TW / English UI strings
from taiko2x6.app import theme  # noqa: E402  light/dark look
from taiko2x6.app import jobs  # noqa: E402  log file, crash dialog, elapsed time
from taiko2x6.app import icons  # noqa: E402  monochrome SVG icon set
from taiko2x6.app.i18n import tr  # noqa: E402

from PySide6.QtCore import (  # noqa: E402
    Qt, QThread, Signal, QObject, QSortFilterProxyModel, QModelIndex, QSize,
    QSettings, QEvent,
)
from PySide6.QtGui import (  # noqa: E402
    QStandardItem, QStandardItemModel, QAction, QActionGroup, QIcon, QImage,
    QPixmap, QFont, QColor, QKeySequence, QShortcut,
)
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QMainWindow, QWidget, QTreeView, QVBoxLayout, QHBoxLayout,
    QSplitter, QLineEdit, QLabel, QPushButton, QFileDialog, QMessageBox,
    QProgressDialog, QPlainTextEdit, QTabWidget, QStyle, QToolBar, QStatusBar,
    QHeaderView, QMenu, QCheckBox, QGroupBox, QFormLayout, QSpinBox,
    QStackedWidget, QToolButton, QFrame, QListWidget, QListWidgetItem,
)

SECTOR_SIZE = core.SECTOR_SIZE


# Qt item-data roles
ROLE_KIND = Qt.UserRole + 1      # "folder" | "group" | "file"
ROLE_GROUP = Qt.UserRole + 2     # group index (int)
ROLE_FILE = Qt.UserRole + 3      # file index (int)


# Archive model lives in taiko2x6.archive.archive_model (no Qt dependency);
# re-exported here so older callers using ``gui.Archive`` keep working.
from taiko2x6.archive.archive_model import Archive  # noqa: E402,F401
from taiko2x6.safeio import write_pair  # noqa: E402


# =============================================================================
# Extraction worker (multi-threaded)
# =============================================================================
class ExtractWorker(QThread):
    progress = Signal(int, int, str)        # done, total, current name
    done = Signal(int, int, int, float, str, int)  # groups, files, pngs, elapsed_s, error, png_skipped

    GROUP_RECORD_KEYS = ["index", "name", "file_count", "first_file",
                         "compression", "sector", "packed_size",
                         "unpacked_size", "unknown2"]

    def __init__(self, archive: Archive, out_dir: Path, group_indices: list[int],
                 workers: int, write_manifest: bool = False, convert_png: bool = False):
        super().__init__()
        self.archive = archive
        self.out_dir = Path(out_dir)
        self.group_indices = group_indices
        self.workers = max(1, workers)
        self.write_manifest = write_manifest
        self.convert_png = convert_png
        self._cancel = Event()
        self._png_skipped = 0
        self._png_lock = Lock()

    def cancel(self):
        self._cancel.set()

    def _extract_group(self, gi: int) -> tuple[dict, int] | None:
        if self._cancel.is_set():
            return None
        layout = self.archive.layout
        group = layout.groups[gi]
        # decode without touching the shared LRU cache (thread-local work)
        payload = core.decode_group_payload(self.archive._mm, group)
        group_dir = self.out_dir / core.group_to_path(group["name"])
        group_dir.mkdir(parents=True, exist_ok=True)
        record = {k: group[k] for k in self.GROUP_RECORD_KEYS}
        record["files"] = []
        pngs = 0
        for entry in layout.files_for_group(group):
            if self._cancel.is_set():
                break
            start, size = entry["offset"], entry["size"]
            out_path = group_dir / entry["name"]
            blob = payload[start:start + size]
            out_path.write_bytes(blob)
            record["files"].append({
                "index": entry["index"], "name": entry["name"], "size": entry["size"],
                "offset": entry["offset"], "unknown": entry["unknown"],
                "path": str(out_path.relative_to(self.out_dir)),
            })
            if self.convert_png and entry["name"].lower().endswith(".nut") \
                    and tim2.is_tim2(blob):
                try:
                    pngs += len(tim2.convert_nut_bytes_to_png(blob, out_path))
                except Exception as exc:
                    # leave the raw .nut; count + log the broken texture
                    with self._png_lock:
                        self._png_skipped += 1
                    log.warning("extract: PNG convert failed for %s/%s: %s",
                                group['name'], entry['name'], exc)
        return record, pngs

    def run(self):
        total = len(self.group_indices)
        groups_done = files_done = pngs_done = 0
        error = ""
        records: dict[int, dict] = {}
        start_t = time.perf_counter()
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                futures = {ex.submit(self._extract_group, gi): gi
                           for gi in self.group_indices}
                from concurrent.futures import as_completed
                for fut in as_completed(futures):
                    gi = futures[fut]
                    name = self.archive.layout.groups[gi]["name"]
                    res = fut.result()
                    if res is not None:
                        rec, pngs = res
                        records[gi] = rec
                        files_done += len(rec["files"])
                        pngs_done += pngs
                    groups_done += 1
                    self.progress.emit(groups_done, total, name)
                    if self._cancel.is_set():
                        break
            if self.write_manifest and not self._cancel.is_set() and not error:
                manifest = {
                    "format": self.archive.layout.format,
                    "source_list": str(self.archive.list_path),
                    "source_data": str(self.archive.data_path),
                    "groups": [records[gi] for gi in self.group_indices if gi in records],
                }
                (self.out_dir / "manifest.json").write_text(
                    json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            error = traceback.format_exc()
        elapsed = time.perf_counter() - start_t
        self.done.emit(groups_done, files_done, pngs_done, elapsed, error,
                       self._png_skipped)


# =============================================================================
# Preview widget
# =============================================================================
class _VagDecodeWorker(QThread):
    """Decode a VAG stream off the GUI thread.

    decode_vag walks a whole song's ADPCM (millions of samples in Python), so
    running it inline froze the UI on every music selection. A generation token
    lets the panel discard results from selections the user has already moved
    past.
    """
    done = Signal(object)                   # {gen, rate, ch, pcm, ms} | {gen, error}

    def __init__(self, gen: int, data: bytes, label: str = ""):
        super().__init__()
        self._gen = gen
        self._data = data
        self._label = label

    def run(self):
        t0 = time.perf_counter()
        try:
            from taiko2x6.audio import vagtool
            rate, ch, pcm = vagtool.decode_vag(self._data)
            ms = (time.perf_counter() - t0) * 1000
            log.info("VAG decoded %s: %d Hz %dch %.1fs in %.0f ms",
                     self._label, rate, ch, pcm.shape[0] / rate, ms)
            self.done.emit({"gen": self._gen, "rate": rate, "ch": ch,
                            "pcm": pcm, "ms": ms})
        except Exception as exc:
            log.warning("VAG decode failed %s: %s", self._label, exc)
            self.done.emit({"gen": self._gen, "error": str(exc)})


class PreviewPane(QTabWidget):
    def __init__(self):
        super().__init__()
        self._vag_gen = 0                   # bumped per selection; stale decodes dropped
        self._vag_workers = []              # keep refs alive until QThread finishes
        self.info = QPlainTextEdit(readOnly=True)
        self.info.setFont(theme.mono_font())
        self.hex = QPlainTextEdit(readOnly=True)
        self.hex.setFont(theme.mono_font())
        self.text = QPlainTextEdit(readOnly=True)
        self.text.setFont(theme.mono_font())
        from taiko2x6.graphics import imageview
        self.image_view = imageview.ImageViewer()
        self.image_view.set_message(tr("(no image)"))

        # The player (numpy + QtMultimedia) is built on first use, not at startup.
        self._audio = None
        self._audio_holder = QWidget()
        self._audio_layout = QVBoxLayout(self._audio_holder)
        self._audio_msg = QLabel(tr("(no audio)"), alignment=Qt.AlignCenter)
        self._audio_layout.addWidget(self._audio_msg)
        self._audio_layout.addStretch(1)

        from taiko2x6.charts import chart_timeline
        self.chart_view = chart_timeline.ChartTimelineView()
        self.addTab(self.info, tr("Info"))
        self.addTab(self.chart_view, tr("Chart"))
        self.addTab(self.hex, tr("Hex"))
        self.addTab(self.text, tr("Text"))
        self.addTab(self.image_view, tr("Image"))
        self.addTab(self._audio_holder, tr("Audio"))
        # Space = play/pause while the Audio tab is showing (e.g. tab bar focused)
        self._sc_space = QShortcut(QKeySequence(Qt.Key_Space), self)
        self._sc_space.setContext(Qt.WidgetWithChildrenShortcut)
        self._sc_space.activated.connect(self._space_pressed)

    @property
    def audio(self):
        if self._audio is None:
            from taiko2x6.audio import audioplayer
            self._audio = audioplayer.AudioPlayer()
            self._audio_layout.insertWidget(1, self._audio)
        return self._audio

    def _audio_stop(self):
        if self._audio is not None:
            self._audio.stop()

    def _space_pressed(self):
        if self.currentWidget() is self._audio_holder and self._audio is not None:
            self._audio._toggle()

    def show_file(self, name: str, group_name: str, entry: dict, data: bytes,
                  modified: bool):
        info = (
            f"name        : {name}\n"
            f"group       : {group_name}\n"
            f"size        : {len(data):,} bytes\n"
            f"group offset: 0x{entry['offset']:X}\n"
            f"file index  : {entry['index']}\n"
            f"modified    : {'YES (unsaved)' if modified else 'no'}\n"
        )
        self.hex.setPlainText(hexdump(data, limit=64 * 1024))

        # text tab (best-effort decode)
        txt = None
        for enc in ("utf-8", "shift_jis"):
            try:
                txt = data.decode(enc)
                break
            except Exception:
                continue
        self.text.setPlainText(txt if txt is not None else "(not decodable as UTF-8 / Shift-JIS)")

        # chart summary (read-only parse of .sht files)
        from taiko2x6.charts import chart_preview
        self.chart_view.set_model(None)
        if chart_preview.is_chart_name(name):
            try:
                info += "\n" + chart_preview.summarize_sht(name, data) + "\n"
                self.chart_view.set_model(chart_preview.parse_chart(data))
            except Exception as exc:
                info += "\n" + tr("(chart not readable: {e})").format(e=exc) + "\n"

        # image tab: decode TIM2 (.nut), else fall back to Qt's loaders.
        # The viewer zooms/pans, so the full-size pixmap is kept.
        pix = None
        fmt = ""
        if tim2.is_tim2(data):
            try:
                info += "\n" + tim2.tim2_summary(data) + "\n"
                pix = _tim2_to_pixmap(data)
                fmt = "TIM2"
            except Exception as exc:
                self.image_view.set_message(tr("(TIM2 decode failed: {e})").format(e=exc))
        else:
            qimg = QImage.fromData(data)
            if not qimg.isNull():
                pix = QPixmap.fromImage(qimg)
                from PySide6.QtCore import QBuffer, QByteArray
                from PySide6.QtGui import QImageReader
                ba = QByteArray(data)          # keep ba/buf/reader alive together
                buf = QBuffer(ba)
                buf.open(QBuffer.ReadOnly)
                reader = QImageReader(buf)
                fmt = bytes(reader.format()).decode("ascii", "ignore").upper()
                del reader
                buf.close()
            else:
                self.image_view.set_message(tr("(not a recognized image format)"))
        if pix is not None:
            self.image_view.set_pixmap(
                pix, tr("{w} x {h} px  {fmt}").format(w=pix.width(), h=pix.height(), fmt=fmt))

        # audio tab — decode VAG streams (file 'vag' / interleaved-VAG header).
        # Decoding a whole song is slow, so it runs on a worker thread; the UI
        # stays responsive and the player loads when the decode finishes.
        from taiko2x6.audio import vagsniff
        self._vag_gen += 1                  # invalidate any in-flight decode
        if name.lower() == "vag" or vagsniff.is_vag(data):
            self._audio_stop()
            gen = self._vag_gen
            nm = group_name.split(".")[-1] or name
            self._audio_msg.setText(tr("VAG audio — {n}  (decoding…)").format(n=name))
            info += "\n" + tr("VAG stream — decoding for the Audio tab…") + "\n"
            self.setCurrentWidget(self._audio_holder)
            log.info("preview VAG %s/%s (%d bytes) — decoding on worker",
                     group_name, name, len(data))
            w = _VagDecodeWorker(gen, data, label=f"{group_name}/{name}")
            w.done.connect(lambda r, nm=nm, name=name, src=data:
                           self._on_vag_decoded(r, nm, name, src))
            w.finished.connect(lambda w=w: self._vag_workers.remove(w)
                               if w in self._vag_workers else None)
            self._vag_workers.append(w)
            w.start()
        else:
            self._audio_stop()
            self._audio_msg.setText(tr("(no audio for this file)"))

        self.info.setPlainText(info)

    def _on_vag_decoded(self, r: dict, nm: str, name: str, src: bytes):
        # Drop results the user has already navigated past.
        if r.get("gen") != self._vag_gen:
            log.debug("dropping stale VAG decode (gen %s, now %s)",
                      r.get("gen"), self._vag_gen)
            return
        if "error" in r:
            self._audio_stop()
            self._audio_msg.setText(tr("(VAG decode failed: {e})").format(e=r["error"]))
            return
        pcm, rate, ch = r["pcm"], r["rate"], r["ch"]
        self.audio._src_vag = src           # enable Export VAG for this stream
        self.audio.load_pcm(
            pcm, rate, ch, autoplay=False, name=nm,
            info=f"VAG · {rate} Hz · {ch}ch · {pcm.shape[0] / rate:.1f}s")
        self._audio_msg.setText(tr("VAG audio — {n}  (press ▶ to play, Space toggles)").format(n=name))


def _tim2_to_pixmap(data: bytes) -> QPixmap:
    """Decode the first TIM2 picture to a QPixmap."""
    import numpy as np
    w, h, rgba = tim2.decode_tim2(data)[0]
    buf = np.ascontiguousarray(rgba, dtype=np.uint8).tobytes()
    qimg = QImage(buf, w, h, w * 4, QImage.Format_RGBA8888).copy()
    return QPixmap.fromImage(qimg)


def hexdump(data: bytes, limit: int = 4096) -> str:
    out = []
    view = data[:limit]
    for off in range(0, len(view), 16):
        chunk = view[off:off + 16]
        hexs = " ".join(f"{b:02X}" for b in chunk)
        asci = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{off:08X}  {hexs:<47}  {asci}")
    if len(data) > limit:
        out.append(f"... ({len(data) - limit:,} more bytes)")
    return "\n".join(out)


# =============================================================================
# Main window
# =============================================================================
class _DropTree(QTreeView):
    """Tree that accepts one dropped file and reports the row it landed on."""
    fileDropped = Signal(QModelIndex, str)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)

    @staticmethod
    def _local_file(event) -> str:
        md = event.mimeData()
        if md.hasUrls():
            for u in md.urls():
                if u.isLocalFile() and Path(u.toLocalFile()).is_file():
                    return u.toLocalFile()
        return ""

    def dragEnterEvent(self, event):
        if self._local_file(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._local_file(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        path = self._local_file(event)
        if not path:
            event.ignore()
            return
        event.acceptProposedAction()
        self.fileDropped.emit(self.indexAt(event.position().toPoint()), path)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Taiko2x6Wizard")
        self.resize(1180, 720)
        self.archive: Archive | None = None
        self.worker: ExtractWorker | None = None

        self._build_ui()
        self._build_actions()
        self._restore_layout()

    # -- ui scaffolding -------------------------------------------------------
    def _build_ui(self):
        style = self.style()

        # tree + model
        self.model = QStandardItemModel()
        self.model.setHorizontalHeaderLabels(
            [tr("Name"), tr("Size"), tr("Type"), tr("Group offset")])
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setRecursiveFilteringEnabled(True)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setFilterKeyColumn(0)

        self.tree = _DropTree()
        self.tree.fileDropped.connect(self._on_file_dropped)
        self.tree.setModel(self.proxy)
        self.tree.setSortingEnabled(False)
        # items are never renamed in place (cheaper than clearing the editable
        # flag on each of the 100k+ items)
        self.tree.setEditTriggers(QTreeView.NoEditTriggers)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QTreeView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.clicked.connect(self._on_tree_clicked)
        self.tree.doubleClicked.connect(self._on_tree_double)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)

        # filter box
        self.filter_box = QLineEdit(placeholderText=tr("Filter by name…"))
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.setToolTip(tr("Filter the tree by name (Ctrl+F to focus, Esc to clear)"))
        self.filter_box.textChanged.connect(self.proxy.setFilterFixedString)

        browser = QWidget()
        ll = QVBoxLayout(browser)
        ll.setContentsMargins(4, 4, 4, 4)
        ll.addWidget(self.filter_box)
        ll.addWidget(self.tree)

        # left: a start page until an archive is open, then the file tree
        self.left_stack = QStackedWidget()
        self.left_stack.addWidget(self._build_start_page())
        self.left_stack.addWidget(browser)

        # right: preview + (collapsible) extract / save options
        self.preview = PreviewPane()

        extract_box = QGroupBox(tr("Extract options"))
        eform = QFormLayout(extract_box)
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 64)
        self.spin_threads.setValue(min(32, os.cpu_count() or 4))
        self.spin_threads.setToolTip(
            tr("Number of worker threads used by Extract.") + "\n"
            + tr("Detected CPU cores: {n}").format(n=os.cpu_count() or "?"))
        eform.addRow(tr("threads:"), self.spin_threads)
        self.chk_manifest = QCheckBox(tr("write manifest.json (Extract All only)"))
        self.chk_manifest.setChecked(True)
        self.chk_manifest.setToolTip(tr(
            "Write the same manifest.json index the CLI tool produces, so the "
            "extracted tree can be fed back to the CLI patch/repack commands."))
        eform.addRow(self.chk_manifest)
        self.chk_png = QCheckBox(tr("convert .nut (TIM2) → PNG"))
        self.chk_png.setToolTip(tr(
            "For every .nut texture, also write a decoded .png next to it. "
            "The raw .nut is still extracted, so patching keeps working."))
        eform.addRow(self.chk_png)

        save_box = QGroupBox(tr("Save options"))
        form = QFormLayout(save_box)
        self.chk_full_rebuild = QCheckBox(
            tr("full rebuild (allow LARGER files; layout-preserving)"))
        self.chk_full_rebuild.setToolTip(tr(
            "Always on. Repacks DATA.000 so a replacement can be any size while "
            "KEEPING every unchanged group at its original sector (boot-safe on "
            "real SYSTEM256 hardware); only edited groups are recompressed and "
            "appended."))
        # Forced on: the layout-preserving full rebuild is both the most capable
        # (any-size edits) and boot-safe, so there is no reason to run the limited
        # in-place patch path. Disabled so it can't be turned off by accident.
        self.chk_full_rebuild.setChecked(True)
        self.chk_full_rebuild.setEnabled(False)
        self.chk_relayout = QCheckBox(tr("allow relayout (repack group internally)"))
        self.chk_relayout.setToolTip(tr(
            "In-place mode only: lets an edited file grow into spare room within "
            "its own group, as long as the compressed group still fits its "
            "original slot."))
        self.chk_relayout.setEnabled(False)          # irrelevant while full rebuild is forced on
        self.chk_overwrite = QCheckBox(tr("overwrite originals (.bak backup)"))
        self.chk_overwrite.setChecked(True)
        form.addRow(self.chk_full_rebuild)
        form.addRow(self.chk_relayout)
        form.addRow(self.chk_overwrite)
        from taiko2x6.app import guide
        form.addRow(guide.help_button(
            save_box, tr("Save options"),
            tr("Full rebuild (always on): repacks DATA.000 so a replaced file can "
               "be any size. Every group you did not edit stays at its original "
               "sector, which is what keeps the game booting; only edited groups "
               "are recompressed and appended.\n\n"
               "Relayout (greyed out while full rebuild is on) belongs to the "
               "older in-place patch: an edited file may grow inside its own "
               "group as long as the compressed group still fits its original "
               "slot.\n\n"
               "Overwrite originals: on, the old DATA.000 / list.bin are kept as "
               ".bak first. Off, nothing is overwritten and the results are "
               "written next to the originals as .new files."),
            self.chk_full_rebuild, self.chk_relayout, self.chk_overwrite))
        self.lbl_save_hint = QLabel()
        self.lbl_save_hint.setWordWrap(True)
        self.lbl_save_hint.setEnabled(False)       # greyed: a hint, not a control
        form.addRow(self.lbl_save_hint)
        self.chk_overwrite.toggled.connect(lambda _on: self._refresh_save_hint())
        self.chk_full_rebuild.toggled.connect(lambda _on: self._refresh_save_hint())
        self._refresh_save_hint()
        # in-group relayout is meaningless once full rebuild is on
        self.chk_full_rebuild.toggled.connect(
            lambda on: self.chk_relayout.setEnabled(not on))

        self.opts_panel = QWidget()
        ol = QVBoxLayout(self.opts_panel)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.addWidget(extract_box)
        ol.addWidget(save_box)
        self.btn_opts = QToolButton()
        self.btn_opts.setText(tr("Extract / save options"))
        self.btn_opts.setCheckable(True)
        self.btn_opts.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_opts.setAutoRaise(True)
        self.btn_opts.toggled.connect(self._toggle_options)
        show_opts = _settings().value("options_open", False, type=bool)
        self.btn_opts.setChecked(show_opts)
        self._toggle_options(show_opts)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(4, 4, 4, 4)
        rl.addWidget(self.preview, 1)
        rl.addWidget(self._build_pending_box())
        rl.addWidget(self.btn_opts)
        rl.addWidget(self.opts_panel)

        self.split = QSplitter()
        self.split.addWidget(self.left_stack)
        self.split.addWidget(right)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        self.setCentralWidget(self.split)

        self.setStatusBar(QStatusBar())
        self.dirty_label = QLabel()
        self.statusBar().addPermanentWidget(self.dirty_label)
        self.statusBar().showMessage(tr("Open an archive folder (File ▸ Open) to begin."))

    def _build_pending_box(self) -> QGroupBox:
        box = QGroupBox(tr("Pending edits"))
        v = QVBoxLayout(box)
        v.setContentsMargins(6, 6, 6, 6)
        self.pending_list = QListWidget()
        self.pending_list.setMaximumHeight(110)
        self.pending_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.pending_list.itemActivated.connect(self._pending_goto)
        self.pending_list.itemClicked.connect(self._pending_goto)
        v.addWidget(self.pending_list)
        self.btn_pending_discard = QPushButton(tr("Discard selected edit(s)"))
        self.btn_pending_discard.clicked.connect(self._discard_pending_selected)
        v.addWidget(self.btn_pending_discard)
        self.pending_box = box
        box.setVisible(False)
        return box

    def _refresh_save_hint(self):
        full = self.chk_full_rebuild.isChecked()
        how = (tr("Full rebuild: any file size, unchanged groups keep their sectors.")
               if full else tr("In-place patch: edited files must still fit."))
        where = (tr("Originals are overwritten; a .bak backup is made first.")
                 if self.chk_overwrite.isChecked()
                 else tr("Nothing is overwritten; new files are written next to the originals as .new."))
        self.lbl_save_hint.setText(f"{how}\n{where}")

    def _refresh_pending(self):
        a = self.archive
        self.pending_list.clear()
        if a is not None:
            for (gi, fi), data in sorted(a.replacements.items()):
                group = a.layout.groups[gi]
                entry = a.layout.files[fi]
                li = QListWidgetItem(tr("{grp} / {name}: {old:,} → {new:,} bytes").format(
                    grp=group["name"], name=entry["name"],
                    old=entry["size"], new=len(data)))
                li.setData(Qt.UserRole, (gi, fi))
                self.pending_list.addItem(li)
        self.pending_box.setVisible(self.pending_list.count() > 0)

    def _pending_goto(self, li: QListWidgetItem):
        gi, fi = li.data(Qt.UserRole)
        item = self._find_file_item(gi, fi)
        if item is None:
            return
        idx = self.proxy.mapFromSource(item.index())
        if idx.isValid():
            self.tree.setCurrentIndex(idx)
            self.tree.scrollTo(idx)

    def _discard_pending_selected(self):
        if not self.archive:
            return
        keys = [li.data(Qt.UserRole) for li in self.pending_list.selectedItems()]
        if not keys and self.pending_list.count() == 1:
            keys = [self.pending_list.item(0).data(Qt.UserRole)]
        for gi, fi in keys:
            self._unstage_file(gi, fi, update=False)
        if keys:
            self._update_actions()
            self.statusBar().showMessage(
                tr("Discarded {n} staged edit(s).").format(n=len(keys)))

    def _find_file_item(self, gi: int, fi: int) -> QStandardItem | None:
        it = getattr(self, "_file_items", {}).get((gi, fi))
        if it is not None and it.model() is self.model:
            return it
        stack = [self.model.item(r, 0) for r in range(self.model.rowCount())]
        while stack:
            it = stack.pop()
            if it is None:
                continue
            if it.data(ROLE_KIND) == "file":
                if it.data(ROLE_GROUP) == gi and it.data(ROLE_FILE) == fi:
                    return it
                continue
            stack.extend(it.child(r, 0) for r in range(it.rowCount()))
        return None

    def _style_file_item(self, item: QStandardItem, staged_size: int | None):
        """Bold red + new size when staged; plain + archive size otherwise."""
        font = item.font()
        font.setBold(staged_size is not None)
        item.setFont(font)
        entry = self.archive.layout.files[item.data(ROLE_FILE)]
        if staged_size is not None:
            item.setForeground(QColor("#c0392b"))
            text = f"{staged_size:,}*"
        else:
            item.setData(None, Qt.ForegroundRole)
            text = f"{entry['size']:,}"
        parent = item.parent() or self.model.invisibleRootItem()
        size_item = parent.child(item.index().row(), 1)
        if size_item:
            size_item.setText(text)

    def _stage_file(self, item: QStandardItem, data: bytes):
        gi, fi = item.data(ROLE_GROUP), item.data(ROLE_FILE)
        entry = self.archive.layout.files[fi]
        self.archive.stage_replace(gi, fi, data)
        self._style_file_item(item, len(data))
        self._preview_item(item)
        self.statusBar().showMessage(
            tr("Staged replacement: {name} ({n:,} bytes). Save to apply.").format(
                name=entry["name"], n=len(data)))
        self._update_actions()

    def _unstage_file(self, gi: int, fi: int, update: bool = True):
        if not self.archive.unstage(gi, fi):
            return
        item = self._find_file_item(gi, fi)
        if item is not None:
            self._style_file_item(item, None)
        if update:
            self._update_actions()
            self.statusBar().showMessage(tr("Edit discarded."))

    def _on_file_dropped(self, proxy_index: QModelIndex, path: str):
        if self.archive is None:
            return
        item = self._item_from_proxy(proxy_index) if proxy_index.isValid() else None
        if not item or item.data(ROLE_KIND) != "file":
            self.statusBar().showMessage(
                tr("Drop the file onto a file row in the tree to replace it."))
            return
        gi, fi = item.data(ROLE_GROUP), item.data(ROLE_FILE)
        group = self.archive.layout.groups[gi]
        entry = self.archive.layout.files[fi]
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            QMessageBox.critical(self, tr("Replace"), str(exc))
            return
        cur = len(self.archive.read_file(group, entry))
        if QMessageBox.question(
                self, tr("Replace"),
                tr("Replace {name} ({grp}) with {src}?\n\nSize: {old:,} → {new:,} bytes").format(
                    name=entry["name"], grp=group["name"], src=Path(path).name,
                    old=cur, new=len(data))) != QMessageBox.Yes:
            return
        self._stage_file(item, data)

    def _toggle_options(self, on: bool):
        self.opts_panel.setVisible(on)
        self.btn_opts.setArrowType(Qt.DownArrow if on else Qt.RightArrow)

    def _build_start_page(self) -> QWidget:
        from taiko2x6.app.start_page import StartPage
        groups = [
            (tr("Archive"), [
                (tr("Open archive folder…"),
                 tr("A folder with DATA.000 + list.bin: browse, preview, replace files."),
                 'archive', lambda: self.open_archive_dialog()),
                (tr("Pack to ISO…"),
                 tr("Inject the current DATA.000 + LIST.BIN into a new game ISO"),
                 'iso', lambda: self.open_pack_iso()),
            ]),
            (tr("Songs & charts"), [
                (tr("Song Manager…"),
                 tr("Edit, add or remove songs across musicinfo, tuning and streaminfo."),
                 'song_list', lambda: self.open_song_manager()),
                (tr("Custom Song Builder…"),
                 tr("Build a song from a TJA and audio: textures, charts, VAG, stars."),
                 'song_add', lambda: self.open_song_builder()),
                (tr("Song Replacer…"),
                 tr("Overwrite an existing song slot with a custom song."),
                 'song_replace', lambda: self.open_song_replacer()),
                (tr("Omnimix Maker…"),
                 tr("Merge the songs of several Taiko images into one."),
                 'omnimix', lambda: self.open_omnimix()),
            ]),
            (tr("Game patches"), [
                (tr("Boot & select patches…"),
                 tr("Skip the Notice / credits screens, freeze the select timers."),
                 'patch', lambda: self.open_taiko_boot()),
                (tr("Patch taiko song limit (T14+)…"),
                 tr("Raise the 210-song ceiling in the taiko executable."),
                 'patch', lambda: self.open_taiko_exe()),
                (tr("Song-select timer (T14+)…"),
                 tr("Change the song-select countdown to any value from 1 to 999 s."),
                 'timer', lambda: self.open_taiko_timer()),
                (tr("USB keyboard patch (T14)…"),
                 tr("Play Taiko 14 with a USB keyboard and no JVS I/O board."),
                 'keyboard', lambda: self.open_usb_keyboard()),
            ]),
            (tr("HDD & disc"), [
                (tr("Open PS2 HDD image…"),
                 tr("Browse a Taiko HDD .img and extract or replace DATA.000 / list.bin."),
                 'hdd', lambda: self.open_pfsshell()),
                (tr("Open PS2 Memory Card (Arcade/COH)…"),
                 tr("Browse a PS2 or MagicGate Arcade memory-card image."),
                 'memcard', lambda: self.open_ps2mc()),
                (tr("Slim PS2 HDD image…"),
                 tr("Make a PS2 HDD image smaller."),
                 'slim', lambda: self.open_img_slim()),
            ]),
        ]
        page = StartPage(groups, self._open_recent_entry, self._recent_entries,
                         self._remove_recent_entry)
        self.start_page = page
        self.setup_box = page.setup_box
        self.btn_setup = page.btn_setup
        self.btn_setup.clicked.connect(self.open_settings)
        self._start_buttons = page.cards
        self._refresh_setup_box()
        return page

    def _recent_entries(self) -> list[tuple[str, str]]:
        out = [("folder", p) for p in self._recent_folders()]
        raw = QSettings("TaikoTools", "SYSTEM256Explorer").value("recent_hdd_img", "")
        if raw:
            out.append(("image", str(raw)))
        return out

    def _open_recent_entry(self, kind: str, path: str):
        if kind == "folder":
            self.open_folder(Path(path))
        else:
            self.open_pfsshell()

    def _remove_recent_entry(self, kind: str, path: str):
        if kind == "folder":
            keep = [p for p in self._recent_folders() if p != path]
            appconfig.set_value("recent", "folders", "|".join(keep))
            self._refresh_recent_menu()
        else:
            QSettings("TaikoTools", "SYSTEM256Explorer").remove("recent_hdd_img")

    def _refresh_setup_box(self):
        from taiko2x6.app import settings_dialog
        missing = settings_dialog.missing_important()
        if missing:
            lines = "<br>".join(f"&#9744; {tr(d['label'])} — {tr(d['note'])}"
                                for d in missing)
            self.setup_box.setText(f"<b>{tr('Setup')}</b><br>{lines}")
        self.setup_box.setVisible(bool(missing))
        self.btn_setup.setVisible(bool(missing))

    def open_settings(self):
        from taiko2x6.app import settings_dialog
        settings_dialog.SettingsDialog(self).exec()
        self._refresh_setup_box()

    def _build_actions(self):
        tb = QToolBar(tr("Main"))
        tb.setObjectName("main_toolbar")
        tb.setIconSize(QSize(18, 18))
        # show what each button does next to its icon
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        self.setWindowIcon(icons.icon("app", color=QColor("#d94f3d")))
        self._icon_names: dict = {}

        def act(attr, text, slot, icon=None, tip=None):
            text = text.replace("&", "&&")         # a literal "&", not a mnemonic
            a = QAction(text, self)
            if icon:
                self._icon_names[a] = icon
            if tip:
                a.setToolTip(tip)
            a.triggered.connect(slot)
            setattr(self, attr, a)
            return a

        act("act_open", tr("Open…"), self.open_archive_dialog, 'open')
        self.act_open.setShortcut(QKeySequence.Open)
        act("act_extract_all", tr("Extract All…"),
            lambda: self.extract(selected_only=False), 'extract')
        act("act_extract_sel", tr("Extract Selected…"),
            lambda: self.extract(selected_only=True), 'extract')
        act("act_replace", tr("Replace File…"), self.replace_selected,
            'replace')
        act("act_save", tr("Save"), self.save_archive, 'save')
        self.act_save.setShortcut(QKeySequence.Save)
        act("act_discard", tr("Discard Edits"), self.discard_edits,
            'discard')
        self.act_extract_all.setShortcut(QKeySequence("Ctrl+E"))
        self.act_extract_sel.setShortcut(QKeySequence("Ctrl+Shift+E"))
        self.act_discard.setShortcut(QKeySequence("Ctrl+Shift+Z"))
        act("act_find", tr("Find…"), self._focus_filter, "find")
        self.act_find.setShortcut(QKeySequence.Find)
        self.addAction(self.act_find)
        act("act_quit", tr("Quit"), self.close, "quit")
        self.act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        # Esc inside the filter box clears it
        QShortcut(QKeySequence(Qt.Key_Escape), self.filter_box,
                  activated=self.filter_box.clear, context=Qt.WidgetShortcut)

        act("act_songmgr", tr("Song Manager…"), self.open_song_manager,
            'song_list',
            tr("Edit/add/remove songs across musicinfo + tuning + streaminfo"))
        act("act_songbuild", tr("Custom Song Builder…"), self.open_song_builder,
            'song_add',
            tr("Replace a song slot from a TJA + audio: textures, charts, VAG, stars"))
        act("act_songreplace", tr("Song Replacer…"), self.open_song_replacer,
            'song_replace',
            tr("Overwrite an existing song slot with a custom song (boot-safe: "
               "counts stay identical). Stages + writes DATA.000 in one step."))
        act("act_packiso", tr("Pack to ISO…"), self.open_pack_iso, 'iso',
            tr("Inject the current DATA.000 + LIST.BIN into a new game ISO"))

        # "Open PS2 HDD (.img)…" (act_hdd -> hdd_browser) and "HDD Song Wizard…"
        # (act_hddwiz -> hdd_song_wizard) were removed from the UI as unused; the
        # modules and the open_hdd_browser/open_hdd_wizard handlers are still here
        # if they need re-exposing. Use the pfsshell browser for HDD file work.
        close_first = " " + tr("Close any emulator holding the image first.")
        act("act_ps2mc", tr("Open PS2 Memory Card (Arcade/COH)…"), self.open_ps2mc,
            'memcard',
            tr("Browse a PS2 / MagicGate-Arcade memory-card image (what mymc "
               "does): extract / replace T14GAME, boot.bin, IRXARC.BIN… ECC is "
               "regenerated on write."))
        act("act_pfsshell", tr("Open PS2 HDD via pfsshell…"), self.open_pfsshell,
            'hdd',
            tr("Browse a PS2 HDD image with the native pfsshell.exe backend "
               "(extract / replace DATA.000, list.bin).") + close_first)
        act("act_taikoexe", tr("Patch taiko song limit (T14+)…"), self.open_taiko_exe,
            'patch',
            tr("Raise the 210-song ceiling in `taiko` (the exe T14+ actually "
               "runs, on the HDD — not the dongle's T14GAME).") + close_first)
        act("act_taikotimer", tr("Song-select timer (T14+)…"), self.open_taiko_timer,
            'timer',
            tr("Change the song-select countdown in `taiko` (stock 120 s) to any "
               "value from 1 to 999 s.") + close_first)
        act("act_taikoboot", tr("Boot & select patches (T14+)…"), self.open_taiko_boot,
            'patch',
            tr("Checkboxes for `taiko`: skip the boot Notice screen, skip the "
               "attract credits (協力) screen, freeze the song-select countdown, "
               "disable the difficulty-select timeout. Unticking restores the "
               "stock code.") + close_first)
        act("act_usbkbd", tr("USB keyboard patch (T14)…"), self.open_usb_keyboard,
            'keyboard',
            tr("Play Taiko 14 with a USB keyboard and no JVS I/O board "
               "(KBD2EE.irx). Installs into the HDD game partition (card "
               "untouched) or onto the dongle card image.") + close_first)
        act("act_imgslim", tr("Slim PS2 HDD image…"), self.open_img_slim,
            'slim',
            tr("Reclaim disk space from a .img. Punches holes over regions that "
               "already read as zero, so the emulator still reads identical "
               "bytes.") + close_first)
        act("act_apamerge", tr("Merge HDD images (multi-game disk)…"),
            self.open_apa_merge, 'hdd',
            tr("Combine several game HDD images into one APA disk. Each game's "
               "memory card looks up its own partition by name, so the card "
               "alone selects which game boots. Sources are never modified."))
        act("act_omnimix", tr("Omnimix Maker (fuse many games)…"), self.open_omnimix,
            'omnimix',
            tr("Harvest every song from several Taiko images (7…14) and merge "
               "the ones the target lacks into one image — charts, textures, "
               "audio and stars — then lift the exe's song ceiling.") + close_first)
        act("act_chartconv", tr("Chart converter (TJA / sht / Gen3 .bin)…"),
            self.open_chart_converter, "chart",
            tip=tr("Convert charts between TJA, PS2-arcade .sht and Nijiiro "
                   "(Gen3) fumen .bin in any direction, branches kept; a Gen3 "
                   "song_<id>.nus3bank becomes a game VAG."))
        act("act_vaggen", tr("VAG Generator (audio→vag)…"), self.open_vag_generator, "song_list",
            tip=tr("Convert wav/ogg to the game's VAG stream, exported to a folder"))
        act("act_shtval", tr("SHT Validator (check format)…"), self.open_sht_validator, "chart",
            tip=tr("Validate a .sht chart against the official on-disc format"))

        # Archive | Edit | Songs | Tools
        groups = ((self.act_open, self.act_extract_all, self.act_extract_sel),
                  (self.act_replace, self.act_save, self.act_discard),
                  (self.act_songmgr, self.act_songbuild, self.act_songreplace,
                   self.act_omnimix),
                  (self.act_packiso, self.act_chartconv, self.act_pfsshell))
        for i, grp in enumerate(groups):
            if i:
                tb.addSeparator()
            for a in grp:
                tb.addAction(a)

        menu = self.menuBar().addMenu(tr("&File"))
        menu.addAction(self.act_open)
        self.recent_menu = menu.addMenu(tr("Open Recent"))
        self._refresh_recent_menu()
        menu.addSeparator()
        menu.addAction(self.act_extract_all)
        menu.addAction(self.act_extract_sel)
        menu.addSeparator()
        menu.addAction(self.act_replace)
        menu.addAction(self.act_save)
        menu.addAction(self.act_discard)
        menu.addSeparator()
        menu.addAction(self.act_find)
        menu.addAction(self.act_quit)

        tools = self.menuBar().addMenu(tr("&Tools"))
        for title, acts in (
                (tr("Songs"), (self.act_songmgr, self.act_songreplace,
                               self.act_songbuild, None, self.act_omnimix)),
                (tr("Charts && audio"), (self.act_chartconv, self.act_vaggen,
                                         self.act_shtval)),
                (tr("PS2 HDD && memory card"), (self.act_pfsshell, self.act_ps2mc,
                                                None, self.act_imgslim,
                                                self.act_apamerge, self.act_packiso)),
                (tr("taiko executable patches"), (self.act_taikoexe,
                                                  self.act_taikotimer,
                                                  self.act_taikoboot,
                                                  self.act_usbkbd))):
            sub = tools.addMenu(title)
            for a in acts:
                if a is None:
                    sub.addSeparator()
                else:
                    sub.addAction(a)

        settings = self.menuBar().addMenu(tr("&Settings"))
        self.act_settings = QAction(tr("External files…"), self)
        self._icon_names[self.act_settings] = "settings"
        self.act_settings.triggered.connect(self.open_settings)
        settings.addAction(self.act_settings)
        lang_menu = settings.addMenu(tr("Language"))
        group = QActionGroup(self)
        group.setExclusive(True)
        self.lang_actions = {}
        for code, name in i18n.LANGUAGES.items():
            a = QAction(name, self, checkable=True)
            a.setChecked(code == i18n.lang())
            a.triggered.connect(lambda _=False, c=code: self._set_language(c))
            group.addAction(a)
            lang_menu.addAction(a)
            self.lang_actions[code] = a
        theme_menu = settings.addMenu(tr("Theme"))
        tgroup = QActionGroup(self)
        tgroup.setExclusive(True)
        self.theme_actions = {}
        for mode, name in (("system", tr("System")), ("light", tr("Light")),
                           ("dark", tr("Dark"))):
            a = QAction(name, self, checkable=True)
            a.setChecked(mode == theme.saved_mode())
            a.triggered.connect(lambda _=False, m=mode: self._set_theme(m))
            tgroup.addAction(a)
            theme_menu.addAction(a)
            self.theme_actions[mode] = a
        help_menu = self.menuBar().addMenu(tr("&Help"))
        self.act_guide = QAction(tr("Quick guide"), self)
        self._icon_names[self.act_guide] = "help"
        self.act_guide.setShortcut(QKeySequence.HelpContents)
        self.act_guide.triggered.connect(self.open_guide)
        help_menu.addAction(self.act_guide)
        self.act_open_log = QAction(tr("Open log folder"), self)
        self._icon_names[self.act_open_log] = "log"
        self.act_open_log.triggered.connect(jobs.open_log_folder)
        help_menu.addAction(self.act_open_log)
        self._refresh_icons()
        self._update_actions()

    def _refresh_icons(self):
        """Tint every action icon with the current palette; add shortcuts to tips."""
        for a, name in self._icon_names.items():
            a.setIcon(icons.icon(name))
            tip = a.toolTip()
            sc = a.shortcut().toString(QKeySequence.NativeText)
            if sc and not tip.endswith(f"({sc})"):
                a.setToolTip(f"{tip} ({sc})")

    def changeEvent(self, ev):
        if ev.type() == QEvent.ApplicationPaletteChange and hasattr(self, "_icon_names"):
            for a, name in self._icon_names.items():
                a.setIcon(icons.icon(name))
        super().changeEvent(ev)

    def open_guide(self):
        """Help > Quick guide: a non-modal window, one page per workflow."""
        from taiko2x6.app import guide
        g = getattr(self, "_guide", None)
        if g is None:
            g = self._guide = guide.GuideDialog(self)
        g.show()
        g.raise_()
        g.activateWindow()

    def _set_theme(self, mode: str):
        theme.save_mode(mode)
        theme.apply(QApplication.instance(), mode)

    def _focus_filter(self):
        if self.archive is not None:
            self.filter_box.setFocus()
            self.filter_box.selectAll()

    def _recent_folders(self) -> list[str]:
        raw = appconfig.get("recent", "folders", "")
        return [p for p in raw.split("|") if p]

    def _remember_folder(self, folder: Path):
        cur = [p for p in self._recent_folders() if p != str(folder)]
        appconfig.set_value("recent", "folders", "|".join(([str(folder)] + cur)[:8]))
        self._refresh_recent_menu()
        if getattr(self, "start_page", None) is not None:
            self.start_page.refresh_recent()

    def _refresh_recent_menu(self):
        self.recent_menu.clear()
        folders = [p for p in self._recent_folders() if Path(p).is_dir()]
        for p in folders:
            a = self.recent_menu.addAction(p.replace("&", "&&"))
            a.triggered.connect(lambda _=False, p=p: self.open_folder(Path(p)))
        self.recent_menu.setEnabled(bool(folders))

    def _refresh_dirty_ui(self):
        dirty = self.archive is not None and self.archive.dirty
        self.dirty_label.setText(tr("Unsaved edits") if dirty else "")
        if self.archive is not None:
            self.setWindowTitle(f"{'* ' if dirty else ''}Taiko2x6Wizard — "
                                f"{self.archive.data_path}")

    def _set_language(self, code: str):
        if code == i18n.lang():
            return
        i18n.set_lang(code)
        QMessageBox.information(
            self, i18n.tr_in(code, "Language"),
            i18n.tr_in(code, "Restart Taiko2x6Wizard to use the new language."))

    def _update_actions(self):
        has = self.archive is not None
        for a in (self.act_extract_all, self.act_extract_sel, self.act_replace,
                  self.act_songmgr, self.act_songbuild, self.act_packiso):
            a.setEnabled(has)
        self.act_save.setEnabled(has and self.archive.dirty)
        self.act_discard.setEnabled(has and self.archive.dirty)
        self.left_stack.setCurrentIndex(1 if has else 0)
        self._refresh_pending()
        self._refresh_dirty_ui()

    def _restore_layout(self):
        s = _settings()
        geo = s.value("window_geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        split = s.value("splitter_state")
        if split is not None:
            self.split.restoreState(split)

    def _save_layout(self):
        s = _settings()
        s.setValue("window_geometry", self.saveGeometry())
        s.setValue("splitter_state", self.split.saveState())
        s.setValue("options_open", self.btn_opts.isChecked())

    # -- open -----------------------------------------------------------------
    def open_archive_dialog(self):
        folder = appconfig.pick_dir(self, "archive_dir",
                                    tr("Choose folder containing LIST.BIN + DATA.000"))
        if folder:
            self.open_folder(Path(folder))

    def open_folder(self, folder: Path):
        list_path = _find_ci(folder, ["list.bin"])
        data_path = _find_ci(folder, ["data.000"])
        if not list_path or not data_path:
            QMessageBox.warning(self, tr("Not found"),
                                tr("Could not find both LIST.BIN and DATA.000 in:\n{folder}").format(folder=folder))
            return
        self._load(list_path, data_path)

    def _load(self, list_path: Path, data_path: Path):
        log.info("opening archive: %s", data_path)
        try:
            if self.archive:
                self.archive.close()
            self.archive = Archive(list_path, data_path, fmt=2)
        except Exception as exc:
            log.error("open failed for %s: %s", data_path, exc)
            QMessageBox.critical(self, tr("Open failed"), f"{exc}\n\n{traceback.format_exc()}")
            return
        self._remember_folder(data_path.parent)
        self.setWindowTitle(f"Taiko2x6Wizard — {data_path}")
        self._populate_tree()
        a = self.archive
        log.info("loaded %d groups · %d files · DATA %.1f MB",
                 len(a.layout.groups), len(a.layout.files), a.data_len / 1e6)
        self.statusBar().showMessage(
            tr("{groups} groups · {files} files · DATA {mb:.1f} MB").format(
                groups=len(a.layout.groups), files=len(a.layout.files),
                mb=a.data_len / 1e6))
        self._update_actions()

    def _populate_tree(self):
        # Detach the sort/filter proxy and the view while the model is bulk
        # edited: every insert would otherwise be re-filtered and re-laid-out.
        self.tree.setUpdatesEnabled(False)
        self.proxy.setSourceModel(None)
        self._file_items = {}
        try:
            self.model.removeRows(0, self.model.rowCount())
            if self.archive is not None:
                self._fill_tree()
        finally:
            self.proxy.setSourceModel(self.model)
            self.tree.setUpdatesEnabled(True)
        if self.archive is not None:
            self.tree.collapseAll()
            for (gi, fi), data in self.archive.replacements.items():
                it = self._find_file_item(gi, fi)
                if it is not None:
                    self._style_file_item(it, len(data))

    def _fill_tree(self):
        root = self.model.invisibleRootItem()
        folder_icon = self.style().standardIcon(QStyle.SP_DirIcon)
        file_icon = self.style().standardIcon(QStyle.SP_FileIcon)
        group_icon = self.style().standardIcon(QStyle.SP_DirLinkIcon)

        # folder node cache keyed by path tuple
        nodes: dict[tuple, QStandardItem] = {}

        def get_folder(parts: tuple) -> QStandardItem:
            if parts in nodes:
                return nodes[parts]
            parent = root if len(parts) == 1 else get_folder(parts[:-1])
            item = QStandardItem(folder_icon, parts[-1])
            item.setData("folder", ROLE_KIND)
            parent.appendRow([item, QStandardItem(), QStandardItem(), QStandardItem()])
            nodes[parts] = item
            return item

        for group in self.archive.layout.groups:
            parts = tuple(group["name"].split("."))
            gnode = get_folder(parts)
            gnode.setIcon(group_icon)
            gnode.setData("group", ROLE_KIND)
            gnode.setData(group["index"], ROLE_GROUP)
            comp = {2: "zlib", 6: "raw"}.get(group["compression"], str(group["compression"]))
            gi = group["index"]
            for entry in self.archive.layout.files_for_group(group):
                name_item = QStandardItem(file_icon, entry["name"])
                name_item.setData("file", ROLE_KIND)
                name_item.setData(group["index"], ROLE_GROUP)
                name_item.setData(entry["index"], ROLE_FILE)
                size_item = QStandardItem(f"{entry['size']:,}")
                type_item = QStandardItem(comp)
                off_item = QStandardItem(f"0x{entry['offset']:X}")
                gnode.appendRow([name_item, size_item, type_item, off_item])
                self._file_items[(gi, entry["index"])] = name_item

    # -- selection / preview --------------------------------------------------
    def _src_index(self, proxy_index: QModelIndex) -> QModelIndex:
        return self.proxy.mapToSource(proxy_index)

    def _item_from_proxy(self, proxy_index: QModelIndex) -> QStandardItem | None:
        src = self._src_index(proxy_index)
        if not src.isValid():
            return None
        # always resolve column 0 for role data
        col0 = src.sibling(src.row(), 0)
        return self.model.itemFromIndex(col0)

    def _on_tree_clicked(self, proxy_index: QModelIndex):
        item = self._item_from_proxy(proxy_index)
        if not item or item.data(ROLE_KIND) != "file":
            return
        self._preview_item(item)

    def _on_tree_double(self, proxy_index: QModelIndex):
        item = self._item_from_proxy(proxy_index)
        if not item or item.data(ROLE_KIND) != "file":
            return
        gi = item.data(ROLE_GROUP)
        entry = self.archive.layout.files[item.data(ROLE_FILE)]
        name = entry["name"].lower()
        if name.endswith(".swg"):
            self._open_swg_editor(item)
        elif name.endswith(".bin") and _bin_editor_module(entry["name"]):
            self._open_bin_editor(item)
        elif name in ("hd", "bd") and \
                self.archive.layout.groups[gi]["name"].startswith("sound.hdbd."):
            self._play_sound_bank(gi)
        else:
            self._preview_item(item)

    def _open_bin_editor(self, item: QStandardItem):
        gi, fi = item.data(ROLE_GROUP), item.data(ROLE_FILE)
        group = self.archive.layout.groups[gi]
        entry = self.archive.layout.files[fi]
        mod = _bin_editor_module(entry["name"])
        try:
            data = self.archive.read_file(group, entry)
        except Exception as exc:
            QMessageBox.critical(self, tr("Read failed"), str(exc))
            return
        try:
            dlg = mod.Editor(data, f"{group['name']}/{entry['name']}", self)
        except Exception as exc:
            QMessageBox.critical(self, tr("Editor failed"), f"{exc}\n\n{traceback.format_exc()}")
            return
        if dlg.exec() and dlg.result_bytes is not None and dlg.result_bytes != data:
            self.archive.stage_replace(gi, fi, dlg.result_bytes)
            font = item.font(); font.setBold(True); item.setFont(font)
            item.setForeground(QColor("#c0392b"))
            self.statusBar().showMessage(
                f"{entry['name']} edited ({len(dlg.result_bytes):,} bytes). Save to apply.")
            self._update_actions()

    def _open_swg_editor(self, item: QStandardItem):
        from taiko2x6.graphics import swg_editor
        gi, fi = item.data(ROLE_GROUP), item.data(ROLE_FILE)
        group = self.archive.layout.groups[gi]
        entry = self.archive.layout.files[fi]
        try:
            data = self.archive.read_file(group, entry)
        except Exception as exc:
            QMessageBox.critical(self, tr("Read failed"), str(exc))
            return
        # decode up to 64 group textures for visual context
        textures = []
        for e in self.archive.layout.files_for_group(group):
            if not e["name"].lower().endswith(".nut"):
                continue
            try:
                blob = self.archive.read_file(group, e)
                if tim2.is_tim2(blob):
                    textures.append((e["name"], _tim2_to_pixmap(blob)))
            except Exception as exc:
                log.debug("swg: texture decode skipped %s/%s: %s",
                          group['name'], e['name'], exc)
            if len(textures) >= 64:
                break
        dlg = swg_editor.SwgEditor(data, textures, f"{group['name']}/{entry['name']}", self)
        if dlg.exec() and dlg.result_bytes is not None and dlg.result_bytes != data:
            self.archive.stage_replace(gi, fi, dlg.result_bytes)
            font = item.font(); font.setBold(True); item.setFont(font)
            item.setForeground(QColor("#c0392b"))
            self.statusBar().showMessage(
                f"SWG edited: {entry['name']} ({len(dlg.result_bytes):,} bytes). Save to apply.")
            self._update_actions()

    def _preview_item(self, item: QStandardItem):
        gi = item.data(ROLE_GROUP)
        fi = item.data(ROLE_FILE)
        group = self.archive.layout.groups[gi]
        entry = self.archive.layout.files[fi]
        try:
            data = self.archive.read_file(group, entry)
        except Exception as exc:
            QMessageBox.critical(self, tr("Read failed"), str(exc))
            return
        modified = (gi, fi) in self.archive.replacements
        self.preview.show_file(entry["name"], group["name"], entry, data, modified)

    # -- context menu ---------------------------------------------------------
    def _tree_menu(self, pos):
        index = self.tree.indexAt(pos)
        item = self._item_from_proxy(index) if index.isValid() else None
        menu = QMenu(self)
        menu.addAction(self.act_extract_sel)
        if item and item.data(ROLE_KIND) in ("group", "folder"):
            act_play = menu.addAction("Play frame animation…")
            act_play.triggered.connect(lambda: self._play_frames(item))
            gi = item.data(ROLE_GROUP)
            if gi is not None and \
                    self.archive.layout.groups[gi]["name"].startswith("sound.hdbd."):
                act_bank = menu.addAction("Play sound bank…")
                act_bank.triggered.connect(lambda: self._play_sound_bank(gi))
        if item and item.data(ROLE_KIND) == "file":
            menu.addSeparator()
            menu.addAction(self.act_replace)
            gi0, fi0 = item.data(ROLE_GROUP), item.data(ROLE_FILE)
            if (gi0, fi0) in self.archive.replacements:
                act_disc = menu.addAction(tr("Discard edit"))
                act_disc.triggered.connect(lambda: self._unstage_file(gi0, fi0))
            act_copy = menu.addAction(tr("Copy name"))
            act_copy.triggered.connect(lambda: QApplication.clipboard().setText(
                self.archive.layout.files[fi0]["name"]))
            act_export = menu.addAction("Export This File…")
            act_export.triggered.connect(lambda: self._export_one(item))
            entry = self.archive.layout.files[item.data(ROLE_FILE)]
            group = self.archive.layout.groups[item.data(ROLE_GROUP)]
            if entry["name"].lower().endswith(".nut") and \
                    group["name"].lower().startswith("music_texture.kenri_song_"):
                act_gen = menu.addAction("Generate song-name texture…")
                act_gen.triggered.connect(lambda: self._gen_song_texture(item))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _gen_song_texture(self, item: QStandardItem):
        from taiko2x6.graphics import songtex
        gi, fi = item.data(ROLE_GROUP), item.data(ROLE_FILE)
        group = self.archive.layout.groups[gi]
        entry = self.archive.layout.files[fi]
        try:
            template = self.archive.read_file(group, entry)
            lay = tim2.first_picture_layout(template)
            if lay["image_type"] != 4 or (lay["width"], lay["height"]) != (640, 160):
                QMessageBox.warning(self, tr("Song texture"),
                                    tr("This nut is not a 640×160 4-bit song-name slot."))
                return
        except Exception as exc:
            QMessageBox.critical(self, tr("Read failed"), str(exc))
            return
        # seed title from the song id in the group name
        sid = group["name"].split("kenri_song_", 1)[-1]
        dlg = songtex.SongTexDialog(template, title=sid, parent=self)
        if dlg.exec() and dlg.result_bytes is not None and dlg.result_bytes != template:
            self.archive.stage_replace(gi, fi, dlg.result_bytes)
            self._mark_file_modified(gi, fi)
            self.statusBar().showMessage(
                f"Generated song-name texture for {group['name']}. Save to apply.")
            self._update_actions()

    def _play_frames(self, item: QStandardItem):
        from taiko2x6.graphics import flipbook_player
        # collect groups under this node, gather their .nut frames in order
        gids: set[int] = set()
        self._collect_descendant_groups(item, gids)
        if item.data(ROLE_KIND) == "group" and item.data(ROLE_GROUP) is not None:
            gids.add(item.data(ROLE_GROUP))
        frames = []
        for gi in sorted(gids):
            group = self.archive.layout.groups[gi]
            for e in self.archive.layout.files_for_group(group):
                if not e["name"].lower().endswith(".nut"):
                    continue
                try:
                    blob = self.archive.read_file(group, e)
                    if tim2.is_tim2(blob):
                        frames.append((e["name"], _tim2_to_pixmap(blob)))
                except Exception as exc:
                    log.debug("frames: decode skipped %s/%s: %s",
                              group['name'], e['name'], exc)
        if not frames:
            QMessageBox.information(self, tr("Frame Player"), tr("No .nut frames found here."))
            return
        title = item.text() if item.text() else "frames"
        dlg = flipbook_player.FlipbookPlayer(frames, title, self)
        dlg.exec()

    def _play_sound_bank(self, gi: int):
        from taiko2x6.audio import audioplayer
        group = self.archive.layout.groups[gi]
        files = {e["name"]: e for e in self.archive.layout.files_for_group(group)}
        if "hd" not in files or "bd" not in files:
            QMessageBox.information(self, tr("Sound bank"), tr("This group has no hd/bd pair."))
            return
        try:
            hd = self.archive.read_file(group, files["hd"])
            bd = self.archive.read_file(group, files["bd"])
            dlg = audioplayer.SoundBankDialog(hd, bd, group["name"], self)
        except Exception as exc:
            QMessageBox.critical(self, tr("Sound bank failed"),
                                 f"{exc}\n\n{traceback.format_exc()}")
            return
        dlg.exec()

    def _export_one(self, item: QStandardItem):
        gi, fi = item.data(ROLE_GROUP), item.data(ROLE_FILE)
        group = self.archive.layout.groups[gi]
        entry = self.archive.layout.files[fi]
        dest = appconfig.pick_save(self, "export", tr("Export file"), entry["name"])
        if not dest:
            return
        try:
            Path(dest).write_bytes(self.archive.read_file(group, entry))
            self.statusBar().showMessage(f"Exported {entry['name']} → {dest}")
        except Exception as exc:
            QMessageBox.critical(self, tr("Export failed"), str(exc))

    # -- replace --------------------------------------------------------------
    def replace_selected(self):
        item = self._current_file_item()
        if not item:
            QMessageBox.information(self, tr("Replace"), tr("Select a single file to replace."))
            return
        gi, fi = item.data(ROLE_GROUP), item.data(ROLE_FILE)
        entry = self.archive.layout.files[fi]
        src = appconfig.pick_open(self, "replace", f"Replace {entry['name']} with…")
        if not src:
            return
        self._stage_file(item, Path(src).read_bytes())

    def _current_file_item(self) -> QStandardItem | None:
        idxs = self.tree.selectionModel().selectedIndexes()
        for idx in idxs:
            item = self._item_from_proxy(idx)
            if item and item.data(ROLE_KIND) == "file":
                return item
        return None

    def discard_edits(self):
        if not self.archive or not self.archive.dirty:
            return
        if QMessageBox.question(self, tr("Discard"), tr("Discard all staged replacements?")) \
                != QMessageBox.Yes:
            return
        self.archive.discard_edits()
        self._populate_tree()
        self._update_actions()
        self.statusBar().showMessage(tr("Edits discarded."))

    # -- song manager ---------------------------------------------------------
    def _find_archive_file(self, filename: str):
        """Return (group, entry) for the first file named `filename`, else None."""
        for grp in self.archive.layout.groups:
            for e in self.archive.layout.files_for_group(grp):
                if e["name"].lower() == filename.lower():
                    return grp, e
        return None

    def open_song_manager(self):
        if not self.archive:
            return
        from taiko2x6.songs import song_manager
        needed = {"musicinfo.bin": None, "tuning.bin": None, "streaminfo.bin": None}
        located = {}
        for fn in needed:
            hit = self._find_archive_file(fn)
            if hit is None:
                QMessageBox.warning(self, tr("Song Manager"),
                                    tr("Could not find {fn} in this archive.").format(fn=fn))
                return
            located[fn] = hit
        try:
            data = {fn: self.archive.read_file(g, e) for fn, (g, e) in located.items()}
            dlg = song_manager.SongManager(
                data["musicinfo.bin"], data["tuning.bin"], data["streaminfo.bin"], self)
        except Exception as exc:
            QMessageBox.critical(self, tr("Song Manager failed"),
                                 f"{exc}\n\n{traceback.format_exc()}")
            return
        accepted = dlg.exec()
        result = getattr(dlg, "result", None)
        dlg.deleteLater()
        if not accepted or not result:
            return
        changed = 0
        for fn, new_bytes in result.items():
            if fn not in located or new_bytes == data[fn]:
                continue
            g, e = located[fn]
            self.archive.stage_replace(g["index"], e["index"], new_bytes)
            changed += 1
            self._mark_file_modified(g["index"], e["index"])
        if changed:
            self.statusBar().showMessage(
                tr("Song Manager edited {n} file(s). Save to apply to DATA.000.").format(n=changed))
            self._update_actions()

    def _recent_hdd_img(self) -> str:
        """Last successfully opened HDD .img path (persisted via QSettings), or ""."""
        guess = QSettings("TaikoTools", "SYSTEM256Explorer").value("recent_hdd_img", "")
        guess = str(guess) if guess else ""
        return guess if guess and Path(guess).exists() else ""

    def open_hdd_wizard(self):
        from taiko2x6.ps2 import hdd_song_wizard
        dlg = hdd_song_wizard.HddSongWizard(self, default_img=self._recent_hdd_img())
        dlg.exec()

    def open_hdd_browser(self):
        from taiko2x6.ps2 import hdd_browser
        dlg = hdd_browser.HddBrowserDialog(self, default_img=self._recent_hdd_img())
        dlg.exec()

    def open_ps2mc(self):
        from taiko2x6.ps2 import ps2mc_card
        dlg = ps2mc_card.Ps2mcDialog(self, default_img=self._recent_ps2mc())
        dlg.exec()

    def _recent_ps2mc(self) -> str:
        try:
            from taiko2x6.app import appconfig
            return appconfig.last_existing("ps2mc") or ""
        except Exception:
            return ""

    def open_pfsshell(self):
        from taiko2x6.ps2 import pfsshell_tool
        if not pfsshell_tool.find_pfsshell(self._recent_hdd_img() or None):
            QMessageBox.warning(
                self, "pfsshell",
                tr("pfsshell.exe not found. Put it next to this toolkit or next "
                   "to your .img, or set the PFSSHELL_EXE environment variable."))
            return
        dlg = pfsshell_tool.PfsShellDialog(self, default_img=self._recent_hdd_img())
        dlg.exec()

    def open_taiko_exe(self):
        from taiko2x6.exe import taiko_exe
        try:
            taiko_exe.find_t14load()
        except taiko_exe.TaikoExeError as exc:
            QMessageBox.warning(self, tr("Patch taiko"), str(exc))
            return
        dlg = taiko_exe.TaikoExeDialog(self, default_img=self._recent_hdd_img())
        dlg.exec()

    def open_taiko_timer(self):
        from taiko2x6.exe import taiko_exe
        try:
            taiko_exe.find_t14load()
        except taiko_exe.TaikoExeError as exc:
            QMessageBox.warning(self, tr("Song-select timer"), str(exc))
            return
        dlg = taiko_exe.TaikoTimerDialog(self, default_img=self._recent_hdd_img())
        dlg.exec()

    def open_taiko_boot(self):
        from taiko2x6.exe import taiko_exe
        try:
            taiko_exe.find_t14load()
        except taiko_exe.TaikoExeError as exc:
            QMessageBox.warning(self, tr("Boot & select patches"), str(exc))
            return
        dlg = taiko_exe.TaikoBootDialog(self, default_img=self._recent_hdd_img())
        dlg.exec()

    def open_usb_keyboard(self):
        from taiko2x6.exe import usb_keyboard
        dlg = usb_keyboard.UsbKeyboardDialog(self, default_img=self._recent_hdd_img())
        dlg.exec()

    def open_img_slim(self):
        from taiko2x6.ps2 import img_slim
        if img_slim.QDialog is None:
            QMessageBox.warning(self, tr("Slim image"), tr("PySide6 is required."))
            return
        if os.name != "nt":
            QMessageBox.warning(
                self, tr("Slim image"),
                tr("Slimming uses NTFS sparse files and needs Windows."))
            return
        dlg = img_slim.ImgSlimDialog(self, default_img=self._recent_hdd_img() or "")
        dlg.exec()

    def open_apa_merge(self):
        from taiko2x6.ps2 import apa_merge
        if apa_merge.QDialog is None:
            QMessageBox.warning(self, tr("Merge HDD images"), tr("PySide6 is required."))
            return
        dlg = apa_merge.ApaMergeDialog(self)
        dlg.exec()

    def open_omnimix(self):
        try:
            from taiko2x6.songs import omnimix_gui
        except Exception as exc:
            QMessageBox.warning(self, tr("Omnimix Maker"), f"Could not load: {exc}")
            return
        dlg = omnimix_gui.OmnimixDialog(self, default_img=self._recent_hdd_img() or "")
        dlg.exec()

    def open_pack_iso(self):
        if not self.archive:
            return
        from taiko2x6.ps2 import iso_packer
        # guess the original ISO next to the data dir
        data_dir = Path(self.archive.data_path).resolve().parent
        guess = ""
        for cand in (data_dir.parent / "Taiko No Tatsujin 8.iso",
                     data_dir / "Taiko No Tatsujin 8.iso"):
            if cand.exists():
                guess = str(cand)
                break
        dlg = iso_packer.PackIsoDialog(
            self.archive.data_path, self.archive.list_path, guess, self)
        dlg.exec()

    def open_song_replacer(self):
        if not self.archive:
            QMessageBox.information(self, tr("Song Replacer"), tr("Open an archive first."))
            return
        if self.archive.dirty and QMessageBox.question(
                self, tr("Song Replacer"),
                "There are unsaved staged edits. Replacing a song will add to them "
                "and then write everything out. Continue?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        from taiko2x6.songs import song_replacer
        dlg = song_replacer.SongReplacerDialog(self.archive, self)
        dlg.exec()
        if not getattr(dlg, "changed", False):
            return
        for (gi, fi) in list(self.archive.replacements.keys()):
            self._mark_file_modified(gi, fi)
        self._update_actions()
        # Write it out immediately with the boot-safe (layout-preserving) full
        # rebuild, so the result is a ready-to-run DATA.000 / list.bin.
        if QMessageBox.question(
                self, tr("Write DATA.000"),
                "Song staged into the slot. Write it into DATA.000 / list.bin now "
                "(boot-safe full rebuild, .bak backup)?",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.save_archive()

    def open_chart_converter(self):
        from taiko2x6.charts import chart_convert
        if chart_convert.QDialog is None:
            QMessageBox.warning(self, "Chart converter", tr("PySide6 is required."))
            return
        chart_convert.ChartConverterDialog(self).exec()

    def open_vag_generator(self):
        from taiko2x6.songs import generators
        generators.VagGeneratorDialog(self).exec()

    def open_sht_validator(self):
        from taiko2x6.charts import sht_validator
        sht_validator.ShtValidatorDialog(self).exec()

    def open_song_builder(self):
        if not self.archive:
            return
        from taiko2x6.songs import song_builder
        dlg = song_builder.SongBuilderDialog(self.archive, self)
        dlg.exec()
        if getattr(dlg, "new_archive", None):
            self._write_full_archive(*dlg.new_archive)
            return
        if getattr(dlg, "changed", False):
            # mark all staged files in the tree and refresh action state
            for (gi, fi) in list(self.archive.replacements.keys()):
                self._mark_file_modified(gi, fi)
            self.statusBar().showMessage(
                "Custom Song Builder staged edits (incl. audio). Save to write DATA.000.")
            self._update_actions()

    def _write_full_archive(self, list_bytes: bytes, data_bytes: bytes):
        """Write a fully-rebuilt LIST.BIN + DATA.000 (new-song add), backup + reload."""
        a = self.archive
        list_path, data_path = a.list_path, a.data_path
        if QMessageBox.question(
                self, tr("Write new archive"),
                f"A new song was built. Overwrite (with .bak backup)?\n\n"
                f"{list_path}\n{data_path}") != QMessageBox.Yes:
            return
        prog = QProgressDialog("Writing rebuilt archive…", None, 0, 0, self)
        prog.setWindowModality(Qt.WindowModal); prog.setMinimumDuration(0)
        QApplication.processEvents()
        try:
            a.close()
            # DATA + LIST commit together (both or neither), .bak first
            write_pair([(data_path, data_bytes), (list_path, list_bytes)],
                       backup=True)
        except Exception as exc:
            prog.close()
            QMessageBox.critical(self, tr("Write failed"), str(exc))
            self._reload_after_save(list_path, data_path)
            return
        prog.close()
        self._reload_after_save(list_path, data_path)
        QMessageBox.information(
            self, tr("New song added"),
            f"Wrote rebuilt archive.\nGroups now: {len(self.archive.layout.groups)}, "
            f"files: {len(self.archive.layout.files)}.\nAudio (if any) is included in DATA.000.")

    def _mark_file_modified(self, gi: int, fi: int):
        """Bold+red the tree item for a staged file, if currently visible."""
        for r in range(self.model.rowCount()):
            self._mark_recursive(self.model.item(r, 0), gi, fi)

    def _mark_recursive(self, item, gi, fi) -> bool:
        if item is None:
            return False
        if item.data(ROLE_KIND) == "file" and item.data(ROLE_GROUP) == gi \
                and item.data(ROLE_FILE) == fi:
            font = item.font(); font.setBold(True); item.setFont(font)
            item.setForeground(QColor("#c0392b"))
            return True
        for r in range(item.rowCount()):
            if self._mark_recursive(item.child(r, 0), gi, fi):
                return True
        return False

    # -- save -----------------------------------------------------------------
    def save_archive(self):
        if not self.archive or not self.archive.dirty:
            return
        a = self.archive
        full_rebuild = self.chk_full_rebuild.isChecked()
        if self.chk_overwrite.isChecked():
            out_list, out_data = a.list_path, a.data_path
        else:
            out_list = a.list_path.with_suffix(a.list_path.suffix + ".new")
            out_data = a.data_path.with_suffix(a.data_path.suffix + ".new")

        if QMessageBox.question(
                self, tr("Save"),
                tr("Save {files} edited file(s) in {groups} group(s)?").format(
                    files=len(a.replacements), groups=len(a.affected_groups()))
                + "\n\n" + self.lbl_save_hint.text()
                + "\n\n" + f"{out_list}\n{out_data}") != QMessageBox.Yes:
            return
        cur = self._current_file_item()
        keep_sel = (cur.data(ROLE_GROUP), cur.data(ROLE_FILE)) if cur else None

        prog_total = len(a.layout.groups) if full_rebuild else len(a.affected_groups())
        verb = "Rebuilding" if full_rebuild else "Patching"
        # No Cancel: a half-written save is not safe to stop (safeio commits both
        # files or neither, but the button was never checked anyway).
        prog = QProgressDialog(f"{verb}…", None, 0, prog_total, self)
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)

        def report(done, total, name):
            prog.setLabelText(f"{verb} {name}")
            prog.setValue(done)
            QApplication.processEvents()

        log.info("%s %d group(s) -> %s%s", verb.lower(), prog_total, out_data,
                 " (overwrite)" if self.chk_overwrite.isChecked() else "")
        t0 = time.perf_counter()
        try:
            overwrite = self.chk_overwrite.isChecked()
            if overwrite:
                # close mmap before overwriting the file we are mmap'ing
                a.close()
            if full_rebuild:
                summary = a.rebuild(out_list, out_data, report, backup=overwrite)
            else:
                summary = a.save(out_list, out_data, self.chk_relayout.isChecked(),
                                 report, backup=overwrite)
        except Exception as exc:
            log.error("save failed: %s", exc)
            prog.close()
            hint = ""
            if not full_rebuild and "fit" in str(exc).lower():
                hint = ("\n\nTip: tick 'full rebuild' in Save options to allow "
                        "larger replacements (it rewrites the DATA layout).")
            QMessageBox.critical(self, tr("Save failed"), str(exc) + hint)
            # reopen mmap so the app stays usable
            self._reload_after_save(a.list_path, a.data_path)
            return
        prog.close()
        took = _fmt_duration(time.perf_counter() - t0)
        log.info("%s complete: %d group(s), %d file(s) in %s",
                 "rebuild" if full_rebuild else "patch",
                 summary['changed_groups'], summary['files'], took)
        msg = tr("Wrote {groups} group(s), {files} file(s) in {took}.").format(
            groups=summary['changed_groups'], files=summary['files'], took=took)
        if full_rebuild:
            msg += " " + tr("DATA size: {mb:.1f} MB").format(mb=summary['data_size'] / 1e6)
        msg += f"  {out_data}"
        # reload from the written files so further edits start clean
        self._reload_after_save(out_list if not self.chk_overwrite.isChecked() else a.list_path,
                                out_data if not self.chk_overwrite.isChecked() else a.data_path)
        if self.archive is not None:
            if keep_sel is not None:
                it = self._find_file_item(*keep_sel)
                if it is not None:
                    idx = self.proxy.mapFromSource(it.index())
                    self.tree.setCurrentIndex(idx)
                    self.tree.scrollTo(idx)
            self.statusBar().showMessage(msg)

    def _reload_after_save(self, list_path: Path, data_path: Path):
        try:
            self.archive = Archive(list_path, data_path, fmt=2)
        except Exception as exc:
            # the old archive is already closed; do not leave a dangling handle
            self.archive = None
            QMessageBox.critical(self, tr("Reload failed"), str(exc))
            self._populate_tree()      # clears the now-stale tree
            self._update_actions()     # disables save/extract/etc.
            return
        self._populate_tree()
        self._update_actions()

    # -- extract --------------------------------------------------------------
    def _collect_group_indices(self, selected_only: bool) -> list[int]:
        if not selected_only:
            return [g["index"] for g in self.archive.layout.groups]
        result: set[int] = set()
        for idx in self.tree.selectionModel().selectedIndexes():
            if idx.column() != 0:
                continue
            item = self._item_from_proxy(idx)
            if not item:
                continue
            kind = item.data(ROLE_KIND)
            if kind in ("group", "file"):
                result.add(item.data(ROLE_GROUP))
            elif kind == "folder":
                self._collect_descendant_groups(item, result)
        return sorted(result)

    def _collect_descendant_groups(self, item: QStandardItem, out: set[int]):
        gi = item.data(ROLE_GROUP)
        if item.data(ROLE_KIND) == "group" and gi is not None:
            out.add(gi)
        for r in range(item.rowCount()):
            child = item.child(r, 0)
            if child:
                self._collect_descendant_groups(child, out)

    def extract(self, selected_only: bool):
        if not self.archive:
            return
        groups = self._collect_group_indices(selected_only)
        if not groups:
            QMessageBox.information(self, tr("Extract"), tr("Nothing selected to extract."))
            return
        out = appconfig.pick_dir(self, "extract_dir", tr("Extract to folder"))
        if not out:
            return
        workers = self.spin_threads.value()
        write_manifest = self.chk_manifest.isChecked() and not selected_only
        convert_png = self.chk_png.isChecked()
        self._run_extract(Path(out), groups, workers, write_manifest, convert_png)

    def _run_extract(self, out_dir: Path, groups: list[int], workers: int,
                     write_manifest: bool, convert_png: bool):
        prog = QProgressDialog(
            f"Extracting {len(groups)} groups using {workers} threads…",
            tr("Cancel"), 0, len(groups), self)
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        t_start = time.monotonic()
        # we close the dialog explicitly in on_done; don't let reaching the max
        # value auto-close/reset it and race the cancel signal.
        prog.setAutoClose(False)
        prog.setAutoReset(False)

        self.worker = ExtractWorker(self.archive, out_dir, groups, workers,
                                    write_manifest, convert_png)

        def on_progress(done, total, name):
            prog.setValue(done)
            prog.setLabelText(f"[{done}/{total}] {name}  ("
                              f"{jobs.fmt_elapsed(time.monotonic() - t_start)})")

        def on_done(g, f, pngs, elapsed, error, png_skipped):
            prog.close()
            took = _fmt_duration(elapsed)
            rate = f"{f/elapsed:,.0f} files/s" if elapsed > 0 else ""
            mtxt = " · manifest.json written" if write_manifest else ""
            ptxt = f" · {pngs:,} PNG" if convert_png else ""
            stxt = f" · {png_skipped:,} texture(s) skipped" if png_skipped else ""
            if error:
                QMessageBox.critical(self, tr("Extract failed"), error)
            else:
                QMessageBox.information(
                    self, tr("Extract complete"),
                    tr("Extracted {g} groups, {f} files").format(g=g, f=f)
                    + f"{ptxt} · {took} ({rate}){mtxt}{stxt}\n→ {out_dir}")
            self.statusBar().showMessage(
                f"Extracted {g} groups · {f} files{ptxt} · {took} · {rate}{mtxt}{stxt}")
            self.worker = None

        self.worker.progress.connect(on_progress)
        self.worker.done.connect(on_done)
        prog.canceled.connect(self.worker.cancel)
        self.worker.start()

    # -- close ----------------------------------------------------------------
    def closeEvent(self, event):
        if self.archive and self.archive.dirty:
            r = QMessageBox.question(
                self, tr("Unsaved edits"),
                tr("You have unsaved replacements. Quit anyway?"),
                QMessageBox.Yes | QMessageBox.No)
            if r != QMessageBox.Yes:
                event.ignore()
                return
        if self.worker:
            self.worker.cancel()
            self.worker.wait(2000)
        # Let any in-flight VAG decode threads finish so Qt doesn't destroy a
        # running QThread on exit.
        for w in list(getattr(self.preview, "_vag_workers", [])):
            w.wait(3000)
        if self.archive:
            self.archive.close()
        self._save_layout()
        super().closeEvent(event)


_BIN_EDITORS: dict | None = None


def _bin_editor_module(filename: str):
    """Return the bineditor_* module registered for this .bin filename, or None.

    Modules are discovered lazily by importing every bineditor_*.py in the
    taiko2x6.bineditors package and reading its FILENAME constant. A broken
    module is skipped, not fatal.
    """
    global _BIN_EDITORS
    if _BIN_EDITORS is None:
        import importlib
        from taiko2x6 import bineditors
        _BIN_EDITORS = {}
        stems = sorted(p.stem for p in
                       Path(bineditors.__file__).resolve().parent.glob("bineditor_*.py"))
        if not stems:
            # Frozen (PyInstaller) build: no .py files on disk to glob. The
            # modules are compiled into the exe, so import them by name.
            stems = [
                "bineditor_enso_parts", "bineditor_fname", "bineditor_hdbdinfo",
                "bineditor_lamp", "bineditor_musicinfo", "bineditor_rank",
                "bineditor_streaminfo", "bineditor_tuning",
            ]
        for stem in stems:
            try:
                mod = importlib.import_module(f"taiko2x6.bineditors.{stem}")
                fn = getattr(mod, "FILENAME", None)
                if fn and hasattr(mod, "Editor"):
                    _BIN_EDITORS[fn.lower()] = mod
            except Exception as exc:
                log.debug("bin-editor: skipped %s: %s", stem, exc)
    return _BIN_EDITORS.get(Path(filename).name.lower())


def _settings() -> QSettings:
    return QSettings("TaikoTools", "SYSTEM256Explorer")


def _fmt_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds*1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


def _find_ci(folder: Path, names: list[str]) -> Path | None:
    """Case-insensitive lookup of any of `names` in folder."""
    wanted = {n.lower() for n in names}
    for p in folder.iterdir():
        if p.is_file() and p.name.lower() in wanted:
            return p
    return None


def _setup_logging(argv):
    """Send taiko logs to the console. DEBUG if --debug or TAIKO_LOG=DEBUG."""
    level_name = os.environ.get("TAIKO_LOG", "").upper()
    debug = "--debug" in argv or level_name in ("DEBUG", "1", "TRUE")
    level = logging.DEBUG if debug else getattr(logging, level_name, logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
    log.handlers.clear()
    log.addHandler(h)
    log.setLevel(level)
    log.propagate = False
    # The file always records at least INFO (DEBUG when asked) so a problem
    # report has something to attach.
    jobs.setup_file_logging(log, level)


def main():
    argv = [a for a in sys.argv if a != "--debug"]
    _setup_logging(sys.argv)
    log.info("Taiko SYSTEM256 Explorer starting (log level %s)",
             logging.getLevelName(log.level))
    theme.configure_high_dpi()
    app = QApplication(argv)
    theme.apply(app)
    jobs.install_excepthook()
    app.setApplicationName("Taiko SYSTEM256 Explorer")
    win = MainWindow()
    win.show()
    if len(argv) > 1:
        arg = Path(argv[1])
        if arg.is_dir():
            win.open_folder(arg)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

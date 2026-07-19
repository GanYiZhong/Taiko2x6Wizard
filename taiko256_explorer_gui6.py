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
    python taiko256_explorer_gui6.py
    python taiko256_explorer_gui6.py "E:/Taiko No Tatsujin 8"
"""
from __future__ import annotations

import json
import logging
import gc
import mmap
import os
import sys
import shutil
import tempfile
import time
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

# Console logger. Level via env TAIKO_LOG=DEBUG (or --debug); configured in main().
log = logging.getLogger("taiko")

# --- import the proven core logic from the sibling CLI tool --------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
import taiko256_archive_tool_v2 as core  # noqa: E402
import tim2  # noqa: E402  TIM2 (.nut) image decoding
import appconfig  # noqa: E402  last-used-path memory (config.ini)

from PySide6.QtCore import (  # noqa: E402
    Qt, QThread, Signal, QObject, QSortFilterProxyModel, QModelIndex, QSize,
    QSettings,
)
from PySide6.QtGui import (  # noqa: E402
    QStandardItem, QStandardItemModel, QAction, QIcon, QImage, QPixmap,
    QFont, QColor, QKeySequence,
)
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QMainWindow, QWidget, QTreeView, QVBoxLayout, QHBoxLayout,
    QSplitter, QLineEdit, QLabel, QPushButton, QFileDialog, QMessageBox,
    QProgressDialog, QPlainTextEdit, QTabWidget, QStyle, QToolBar, QStatusBar,
    QHeaderView, QMenu, QCheckBox, QGroupBox, QFormLayout, QSpinBox,
)

SECTOR_SIZE = core.SECTOR_SIZE


# =============================================================================
# Atomic write + backup helpers (crash-safe, no truncated DATA.000)
# =============================================================================
def _atomic_write(path: Path, data: bytes):
    """Write `data` to `path` atomically: write a sibling temp file, fsync, then
    os.replace() (atomic on the same volume). Never leaves a half-written target."""
    path = Path(path)
    d = path.parent
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # os.replace can transiently fail on Windows if the target is still held
        # by a just-closed mmap section (see archive_builder) or a viewer/emulator
        # that momentarily has DATA.000 open. Retry a few times with a gc pass and
        # short backoff before giving up.
        for attempt in range(8):
            try:
                os.replace(tmp, str(path))   # atomic on same volume
                break
            except PermissionError:
                if attempt == 7:
                    raise
                gc.collect()
                time.sleep(0.25)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _make_backup(path: Path):
    """Refresh a backup of `path` before an overwrite, preserving prior backups.

    The first backup is `<file>.bak`; subsequent saves roll the live file into
    `<file>.bak.1`, `.bak.2`, … so the known-good original is never clobbered by
    a backup of an already-edited file. No-op if the source does not exist."""
    path = Path(path)
    if not path.exists():
        return
    bak = path.with_suffix(path.suffix + ".bak")
    if not bak.exists():
        shutil.copy2(str(path), str(bak))
        return
    n = 1
    while True:
        rolled = path.with_suffix(path.suffix + f".bak.{n}")
        if not rolled.exists():
            shutil.copy2(str(path), str(rolled))
            return
        n += 1


# Qt item-data roles
ROLE_KIND = Qt.UserRole + 1      # "folder" | "group" | "file"
ROLE_GROUP = Qt.UserRole + 2     # group index (int)
ROLE_FILE = Qt.UserRole + 3      # file index (int)


# =============================================================================
# Archive model (reads via mmap, writes via conservative patch)
# =============================================================================
class Archive:
    """In-memory view over a LIST.BIN + DATA.000 pair, backed by mmap for reads."""

    CACHE_BYTES_CAP = 256 * 1024 * 1024  # decoded-group LRU cache cap

    def __init__(self, list_path: Path, data_path: Path, fmt: int = 2):
        self.list_path = Path(list_path)
        self.data_path = Path(data_path)
        self.fmt = fmt
        self.layout = core.ArchiveLayout(core.crypt_list(self.list_path.read_bytes()), fmt)

        self._fh = open(self.data_path, "rb")
        self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)

        self._cache: "OrderedDict[int, bytes]" = OrderedDict()
        self._cache_bytes = 0
        self._cache_lock = Lock()

        # pending edits: (group_index, file_index) -> new bytes
        self.replacements: dict[tuple[int, int], bytes] = {}

    # -- lifecycle ------------------------------------------------------------
    def close(self):
        try:
            self._mm.close()
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass

    @property
    def data_len(self) -> int:
        return len(self._mm)

    # -- decode / read --------------------------------------------------------
    def decode_group(self, group: dict, use_cache: bool = True) -> bytes:
        gi = group["index"]
        if use_cache:
            with self._cache_lock:
                hit = self._cache.get(gi)
                if hit is not None:
                    self._cache.move_to_end(gi)
                    return hit
        payload = core.decode_group_payload(self._mm, group)
        if use_cache:
            with self._cache_lock:
                self._cache[gi] = payload
                self._cache.move_to_end(gi)
                self._cache_bytes += len(payload)
                while self._cache_bytes > self.CACHE_BYTES_CAP and len(self._cache) > 1:
                    _, dropped = self._cache.popitem(last=False)
                    self._cache_bytes -= len(dropped)
        return payload

    def read_file(self, group: dict, entry: dict) -> bytes:
        key = (group["index"], entry["index"])
        if key in self.replacements:
            return self.replacements[key]
        payload = self.decode_group(group)
        start = entry["offset"]
        return payload[start:start + entry["size"]]

    # -- edit -----------------------------------------------------------------
    def stage_replace(self, group_index: int, file_index: int, data: bytes):
        self.replacements[(group_index, file_index)] = data

    def discard_edits(self):
        self.replacements.clear()

    @property
    def dirty(self) -> bool:
        return bool(self.replacements)

    def affected_groups(self) -> set[int]:
        return {gi for (gi, _fi) in self.replacements}

    # -- save (conservative in-place patch) -----------------------------------
    def save(self, out_list: Path, out_data: Path, allow_relayout: bool,
             progress=None) -> dict:
        """Apply staged replacements and write out_list / out_data.

        Returns a summary dict. Raises ValueError with a readable message on
        a hard conflict (file no longer fits) when allow_relayout is False.

        Memory note: a full writable copy of DATA is loaded here. When the GUI
        overwrites in place it closes self._mm first (so peak ≈ DATA size); the
        non-overwrite (.new) path keeps the mmap open, so peak is ≈ 2× DATA.
        """
        layout = self.layout
        # Need a writable full copy of DATA for patching.
        full = bytearray(self.data_path.read_bytes())
        new_list = bytearray(layout.decoded_list)

        affected = sorted(self.affected_groups())
        changed = 0
        for n, gi in enumerate(affected):
            group = layout.groups[gi]
            if progress:
                progress(n, len(affected), group["name"])
            entries = layout.files_for_group(group)
            original_payload = core.decode_group_payload(full, group)
            new_payload, updated_entries = self._patch_group(
                group, entries, original_payload, allow_relayout
            )
            encoded = core.encode_group_payload(new_payload, group["compression"])
            max_bytes = core.group_capacity_bytes(layout, len(full), group)
            if len(encoded) > max_bytes:
                raise ValueError(
                    f"group '{group['name']}' no longer fits its DATA sector range "
                    f"(encoded={len(encoded)} > available={max_bytes}). "
                    f"Make the replacement smaller."
                )
            start = group["sector"] * SECTOR_SIZE
            clear_len = max(group["packed_size"], len(encoded))
            full[start:start + clear_len] = b"\0" * clear_len
            full[start:start + len(encoded)] = encoded

            gw = dict(group)
            gw["packed_size"] = len(encoded)
            gw["unpacked_size"] = len(new_payload)
            # Content changed -> recompute the game-validated content hash, else
            # the game freezes on load (see core.compute_unknown2).
            gw["unknown2"] = core.compute_unknown2(new_payload)
            layout.write_group(new_list, gw)
            for e in updated_entries:
                layout.write_file(new_list, e)
            changed += 1

        _atomic_write(out_data, bytes(full))
        _atomic_write(out_list, core.crypt_list(bytes(new_list)))
        if progress:
            progress(len(affected), len(affected), "done")
        return {"changed_groups": changed, "files": len(self.replacements)}

    def _patch_group(self, group, entries, original_payload, allow_relayout):
        """Mirror of core conservative patch but driven by self.replacements."""
        gi = group["index"]
        updated = [dict(e) for e in entries]
        payload = bytearray(original_payload)

        if not allow_relayout:
            for i, entry in enumerate(updated):
                key = (gi, entry["index"])
                if key not in self.replacements:
                    continue
                new_file = self.replacements[key]
                start = entry["offset"]
                cap = core.file_slot_capacity(updated, len(original_payload), i)
                if len(new_file) > cap:
                    raise ValueError(
                        f"'{group['name']}/{entry['name']}' is {len(new_file)} bytes "
                        f"but its in-place slot only holds {cap}. "
                        f"Enable 'allow relayout' to repack the group internally."
                    )
                payload[start:start + cap] = b"\0" * cap
                payload[start:start + len(new_file)] = new_file
                entry["size"] = len(new_file)
            return bytes(payload), updated

        # relayout: rebuild the group tightly, keeping the decompressed size.
        rebuilt = bytearray()
        for entry in updated:
            key = (gi, entry["index"])
            if key in self.replacements:
                file_data = self.replacements[key]
            else:
                file_data = bytes(original_payload[entry["offset"]:entry["offset"] + entry["size"]])
            new_off = core.align(len(rebuilt), core.DATA_ALIGN)
            if new_off > len(rebuilt):
                rebuilt.extend(b"\0" * (new_off - len(rebuilt)))
            entry["offset"] = new_off
            entry["size"] = len(file_data)
            rebuilt.extend(file_data)
        if len(rebuilt) > len(original_payload):
            raise ValueError(
                f"group '{group['name']}' grew past its decompressed size "
                f"({len(rebuilt)} > {len(original_payload)}); cannot patch in place."
            )
        rebuilt.extend(b"\0" * (len(original_payload) - len(rebuilt)))
        return bytes(rebuilt), updated

    # -- full rebuild (allows files of ANY size; layout-preserving) -----------
    def rebuild(self, out_list: Path, out_data: Path, progress=None) -> dict:
        """Repack the archive so replacements may be any size.

        LAYOUT-PRESERVING: SYSTEM256 loads some boot-critical data by ABSOLUTE
        sector, so unchanged groups MUST keep their original position — a dense
        re-pack that reorders them boots to a black screen even though LIST.BIN
        stays self-consistent. Unchanged groups therefore stay exactly where they
        were (the whole original DATA region is copied verbatim); only EDITED
        groups are recompressed and appended past the end of the original data,
        and their old sectors become harmless dead space.

        Memory note: reads the whole source DATA into memory and copies it into
        the output bytearray. In the overwrite path the GUI closes self._mm first
        (peak ≈ 2× DATA); the .new path keeps the mmap open (≈ 3×).
        """
        layout = self.layout
        src = self.data_path.read_bytes()          # read original fully into memory first
        new_list = bytearray(layout.decoded_list)
        # Preserve every unchanged group at its original sector by copying the
        # entire original data region verbatim; edited groups get appended.
        new_data = bytearray(src)
        total = len(layout.groups)
        changed = 0

        for n, group in enumerate(layout.groups):
            if progress and (n % 16 == 0):
                progress(n, total, group["name"])
            gi = group["index"]
            entries = layout.files_for_group(group)
            edited = any((gi, e["index"]) in self.replacements for e in entries)
            if not edited:
                continue                            # already in place, verbatim

            # rebuild + recompress the edited group, then append it
            original_payload = core.decode_group_payload(src, group)
            updated = [dict(e) for e in entries]
            payload = bytearray()
            for entry in updated:
                file_data = self.replacements.get((gi, entry["index"]))
                if file_data is None:
                    file_data = original_payload[entry["offset"]:entry["offset"] + entry["size"]]
                new_off = core.align(len(payload), core.DATA_ALIGN)
                if new_off > len(payload):
                    payload.extend(b"\0" * (new_off - len(payload)))
                entry["offset"] = new_off
                entry["size"] = len(file_data)
                payload.extend(file_data)
            # trailer pad, mirroring the CLI repacker
            min_len = core.align(len(payload) + core.PAYLOAD_TRAILER_SIZE, core.PAYLOAD_ALIGN)
            if min_len > len(payload):
                payload.extend(b"\0" * (min_len - len(payload)))
            payload = bytes(payload)
            packed = core.encode_group_payload(payload, group["compression"])

            # append at the next free sector past the current end
            sector = core.align(len(new_data), SECTOR_SIZE) // SECTOR_SIZE
            pad = sector * SECTOR_SIZE - len(new_data)
            if pad:
                new_data.extend(b"\0" * pad)
            gw = dict(group)
            gw["sector"] = sector
            gw["packed_size"] = len(packed)
            gw["unpacked_size"] = len(payload)
            gw["unknown2"] = core.compute_unknown2(payload)   # game-validated hash
            new_data.extend(packed)
            layout.write_group(new_list, gw)
            for entry in updated:
                layout.write_file(new_list, entry)
            changed += 1

        if progress:
            progress(total, total, "writing files")
        _atomic_write(out_data, bytes(new_data))
        _atomic_write(out_list, core.crypt_list(bytes(new_list)))
        return {"changed_groups": changed, "files": len(self.replacements),
                "data_size": len(new_data)}


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
            import vagtool
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
        self.info.setFont(QFont("Consolas", 9))
        self.hex = QPlainTextEdit(readOnly=True)
        self.hex.setFont(QFont("Consolas", 9))
        self.text = QPlainTextEdit(readOnly=True)
        self.text.setFont(QFont("Consolas", 9))
        self.image_label = QLabel("(no image)", alignment=Qt.AlignCenter)
        self.image_label.setMinimumSize(QSize(64, 64))

        import audioplayer
        self.audio = audioplayer.AudioPlayer()
        self._audio_holder = QWidget()
        _ah = QVBoxLayout(self._audio_holder)
        self._audio_msg = QLabel("(no audio)", alignment=Qt.AlignCenter)
        _ah.addWidget(self._audio_msg)
        _ah.addWidget(self.audio)
        _ah.addStretch(1)

        self.addTab(self.info, "Info")
        self.addTab(self.hex, "Hex")
        self.addTab(self.text, "Text")
        self.addTab(self.image_label, "Image")
        self.addTab(self._audio_holder, "Audio")

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

        # image tab — decode TIM2 (.nut), else fall back to Qt's loaders
        pix = None
        if tim2.is_tim2(data):
            try:
                info += "\n" + tim2.tim2_summary(data) + "\n"
                pix = _tim2_to_pixmap(data)
            except Exception as exc:
                self.image_label.setText(f"(TIM2 decode failed: {exc})")
        if pix is None and not tim2.is_tim2(data):
            qimg = QImage.fromData(data)
            if not qimg.isNull():
                pix = QPixmap.fromImage(qimg)
        if pix is not None:
            # scale down only oversized images, keep small ones crisp
            if pix.width() > 768 or pix.height() > 768:
                pix = pix.scaled(768, 768, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image_label.setPixmap(pix)
            self.image_label.setText("")
        elif not tim2.is_tim2(data):
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText("(not a recognized image format)")

        # audio tab — decode VAG streams (file 'vag' / interleaved-VAG header).
        # Decoding a whole song is slow, so it runs on a worker thread; the UI
        # stays responsive and the player loads when the decode finishes.
        import audioplayer
        self._vag_gen += 1                  # invalidate any in-flight decode
        if name.lower() == "vag" or audioplayer.is_vag(data):
            self.audio.stop()
            gen = self._vag_gen
            nm = group_name.split(".")[-1] or name
            self._audio_msg.setText(f"VAG audio — {name}  (decoding…)")
            info += "\nVAG stream — decoding for the Audio tab…\n"
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
            self.audio.stop()
            self._audio_msg.setText("(no audio for this file)")

        self.info.setPlainText(info)

    def _on_vag_decoded(self, r: dict, nm: str, name: str, src: bytes):
        # Drop results the user has already navigated past.
        if r.get("gen") != self._vag_gen:
            log.debug("dropping stale VAG decode (gen %s, now %s)",
                      r.get("gen"), self._vag_gen)
            return
        if "error" in r:
            self.audio.stop()
            self._audio_msg.setText(f"(VAG decode failed: {r['error']})")
            return
        pcm, rate, ch = r["pcm"], r["rate"], r["ch"]
        self.audio._src_vag = src           # enable Export VAG for this stream
        self.audio.load_pcm(
            pcm, rate, ch, autoplay=False, name=nm,
            info=f"VAG · {rate} Hz · {ch}ch · {pcm.shape[0] / rate:.1f}s")
        self._audio_msg.setText(f"VAG audio — {name}  (press ▶ to play)")


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
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Taiko SYSTEM256 Explorer (PySide6)")
        self.resize(1180, 720)
        self.archive: Archive | None = None
        self.worker: ExtractWorker | None = None

        self._build_ui()
        self._build_actions()

    # -- ui scaffolding -------------------------------------------------------
    def _build_ui(self):
        style = self.style()

        # tree + model
        self.model = QStandardItemModel()
        self.model.setHorizontalHeaderLabels(["Name", "Size", "Type", "Group offset"])
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setRecursiveFilteringEnabled(True)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setFilterKeyColumn(0)

        self.tree = QTreeView()
        self.tree.setModel(self.proxy)
        self.tree.setSortingEnabled(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QTreeView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.clicked.connect(self._on_tree_clicked)
        self.tree.doubleClicked.connect(self._on_tree_double)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)

        # filter box
        self.filter_box = QLineEdit(placeholderText="Filter by name…")
        self.filter_box.textChanged.connect(self.proxy.setFilterFixedString)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(4, 4, 4, 4)
        ll.addWidget(self.filter_box)
        ll.addWidget(self.tree)

        # right: preview + save options
        self.preview = PreviewPane()

        extract_box = QGroupBox("Extract options")
        eform = QFormLayout(extract_box)
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 64)
        self.spin_threads.setValue(min(32, os.cpu_count() or 4))
        self.spin_threads.setToolTip(
            "Number of worker threads used by Extract.\n"
            f"Detected CPU cores: {os.cpu_count() or '?'}")
        eform.addRow("threads:", self.spin_threads)
        self.chk_manifest = QCheckBox("write manifest.json (Extract All only)")
        self.chk_manifest.setChecked(True)
        self.chk_manifest.setToolTip(
            "Write the same manifest.json index the CLI tool produces,\n"
            "so the extracted tree can be fed back to the CLI patch/repack commands.")
        eform.addRow(self.chk_manifest)
        self.chk_png = QCheckBox("convert .nut (TIM2) → PNG")
        self.chk_png.setToolTip(
            "For every .nut texture, also write a decoded .png next to it.\n"
            "The raw .nut is still extracted, so patching keeps working.")
        eform.addRow(self.chk_png)

        save_box = QGroupBox("Save options")
        form = QFormLayout(save_box)
        self.chk_full_rebuild = QCheckBox("full rebuild (allow LARGER files; layout-preserving)")
        self.chk_full_rebuild.setToolTip(
            "Always on. Repacks DATA.000 so a replacement can be any size while\n"
            "KEEPING every unchanged group at its original sector (boot-safe on real\n"
            "SYSTEM256 hardware); only edited groups are recompressed and appended.")
        # Forced on: the layout-preserving full rebuild is both the most capable
        # (any-size edits) and boot-safe, so there is no reason to run the limited
        # in-place patch path. Disabled so it can't be turned off by accident.
        self.chk_full_rebuild.setChecked(True)
        self.chk_full_rebuild.setEnabled(False)
        self.chk_relayout = QCheckBox("allow relayout (repack group internally)")
        self.chk_relayout.setToolTip(
            "In-place mode only: lets an edited file grow into spare room within its\n"
            "own group, as long as the compressed group still fits its original slot.")
        self.chk_relayout.setEnabled(False)          # irrelevant while full rebuild is forced on
        self.chk_overwrite = QCheckBox("overwrite originals (.bak backup)")
        self.chk_overwrite.setChecked(True)
        form.addRow(self.chk_full_rebuild)
        form.addRow(self.chk_relayout)
        form.addRow(self.chk_overwrite)
        # in-group relayout is meaningless once full rebuild is on
        self.chk_full_rebuild.toggled.connect(
            lambda on: self.chk_relayout.setEnabled(not on))

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(4, 4, 4, 4)
        rl.addWidget(self.preview, 1)
        rl.addWidget(extract_box)
        rl.addWidget(save_box)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        self.setCentralWidget(split)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Open an archive folder (File ▸ Open) to begin.")

    def _build_actions(self):
        style = self.style()
        tb = QToolBar("Main")
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(tb)

        self.act_open = QAction(style.standardIcon(QStyle.SP_DirOpenIcon), "Open…", self)
        self.act_open.setShortcut(QKeySequence.Open)
        self.act_open.triggered.connect(self.open_archive_dialog)

        self.act_extract_all = QAction(style.standardIcon(QStyle.SP_ArrowDown), "Extract All…", self)
        self.act_extract_all.triggered.connect(lambda: self.extract(selected_only=False))

        self.act_extract_sel = QAction(style.standardIcon(QStyle.SP_FileDialogDetailedView), "Extract Selected…", self)
        self.act_extract_sel.triggered.connect(lambda: self.extract(selected_only=True))

        self.act_replace = QAction(style.standardIcon(QStyle.SP_FileDialogStart), "Replace File…", self)
        self.act_replace.triggered.connect(self.replace_selected)

        self.act_save = QAction(style.standardIcon(QStyle.SP_DialogSaveButton), "Save", self)
        self.act_save.setShortcut(QKeySequence.Save)
        self.act_save.triggered.connect(self.save_archive)

        self.act_discard = QAction(style.standardIcon(QStyle.SP_DialogResetButton), "Discard Edits", self)
        self.act_discard.triggered.connect(self.discard_edits)

        self.act_songmgr = QAction(style.standardIcon(QStyle.SP_FileDialogContentsView),
                                   "Song Manager…", self)
        self.act_songmgr.setToolTip("Edit/add/remove songs across musicinfo + tuning + streaminfo")
        self.act_songmgr.triggered.connect(self.open_song_manager)

        self.act_songbuild = QAction(style.standardIcon(QStyle.SP_FileDialogNewFolder),
                                     "Custom Song Builder…", self)
        self.act_songbuild.setToolTip("Replace a song slot from a TJA + audio: textures, charts, VAG, stars")
        self.act_songbuild.triggered.connect(self.open_song_builder)

        self.act_songreplace = QAction(style.standardIcon(QStyle.SP_BrowserReload),
                                       "Song Replacer…", self)
        self.act_songreplace.setToolTip(
            "Overwrite an existing song slot with a custom song (boot-safe: counts "
            "stay identical). Stages + writes DATA.000 in one step.")
        self.act_songreplace.triggered.connect(self.open_song_replacer)

        self.act_packiso = QAction(style.standardIcon(QStyle.SP_DriveCDIcon),
                                   "Pack to ISO…", self)
        self.act_packiso.setToolTip("Inject the current DATA.000 + LIST.BIN into a new game ISO")
        self.act_packiso.triggered.connect(self.open_pack_iso)

        # "Open PS2 HDD (.img)…" (act_hdd -> hdd_browser) and "HDD Song Wizard…"
        # (act_hddwiz -> hdd_song_wizard) were removed from the UI as unused; the
        # modules and the open_hdd_browser/open_hdd_wizard handlers are still here
        # if they need re-exposing. Use the pfsshell browser for HDD file work.

        self.act_ps2mc = QAction(style.standardIcon(QStyle.SP_DriveFDIcon),
                                 "Open PS2 Memory Card (Arcade/COH)…", self)
        self.act_ps2mc.setToolTip(
            "Browse a PS2 / MagicGate-Arcade memory-card image (what mymc does): "
            "extract / replace T14GAME, boot.bin, IRXARC.BIN… ECC is regenerated "
            "on write.")
        self.act_ps2mc.triggered.connect(self.open_ps2mc)

        self.act_pfsshell = QAction(style.standardIcon(QStyle.SP_DriveHDIcon),
                                    "Open PS2 HDD via pfsshell…", self)
        self.act_pfsshell.setToolTip(
            "Browse a PS2 HDD image with the native pfsshell.exe backend "
            "(extract / replace DATA.000, list.bin). Close any emulator holding "
            "the image first.")
        self.act_pfsshell.triggered.connect(self.open_pfsshell)

        self.act_taikoexe = QAction(style.standardIcon(QStyle.SP_DriveHDIcon),
                                    "Patch taiko song limit (T14+)…", self)
        self.act_taikoexe.setToolTip(
            "Raise the 210-song ceiling in `taiko` (the exe T14+ actually runs, on "
            "the HDD — not the dongle's T14GAME). Relocates ctx->arrayB so it stops "
            "aliasing arrayA. Close any emulator holding the image first.")
        self.act_taikoexe.triggered.connect(self.open_taiko_exe)

        self.act_taikotimer = QAction(style.standardIcon(QStyle.SP_DriveHDIcon),
                                      "Song-select timer (T14+)…", self)
        self.act_taikotimer.setToolTip(
            "Change the song-select countdown in `taiko` (stock 120 s) to any "
            "value from 1 to 999 s. Sets all three select-flow inactivity timers "
            "that otherwise drop you back to the attract loop. Close any emulator "
            "holding the image first.")
        self.act_taikotimer.triggered.connect(self.open_taiko_timer)

        self.act_imgslim = QAction(style.standardIcon(QStyle.SP_DriveHDIcon),
                                   "Slim PS2 HDD image…", self)
        self.act_imgslim.setToolTip(
            "Reclaim disk space from a .img. Punches holes over regions that "
            "already read as zero, so the emulator still reads identical bytes. "
            "Close any emulator holding the image first.")
        self.act_imgslim.triggered.connect(self.open_img_slim)

        self.act_apamerge = QAction(style.standardIcon(QStyle.SP_DriveHDIcon),
                                    "Merge HDD images (multi-game disk)…", self)
        self.act_apamerge.setToolTip(
            "Combine several game HDD images into one APA disk. Each game's "
            "memory card looks up its own partition by name, so the card alone "
            "selects which game boots. Sources are never modified.")
        self.act_apamerge.triggered.connect(self.open_apa_merge)

        self.act_omnimix = QAction(style.standardIcon(QStyle.SP_DriveHDIcon),
                                   "Omnimix Maker (fuse many games)…", self)
        self.act_omnimix.setToolTip(
            "Harvest every song from several Taiko images (8…14) and merge the "
            "ones the target lacks into one image — charts, textures, audio and "
            "stars — then lift the exe's song ceiling. Dedup by id; the target is "
            "the base. Close any emulator holding the images first.")
        self.act_omnimix.triggered.connect(self.open_omnimix)

        self.act_gen3conv = QAction("Gen3 → Gen2 converter…", self)
        self.act_gen3conv.setToolTip(
            "Convert a Nijiiro (Gen3) song to PS2-arcade assets: fumen .bin to "
            ".sht, and song_<id>.nus3bank to a game VAG. Audio decoding needs "
            "vgmstream.")
        self.act_gen3conv.triggered.connect(self.open_gen3_convert)

        self.act_shtgen = QAction("SHT Generator (TJA→sht)…", self)
        self.act_shtgen.setToolTip("Convert a TJA chart to .sht files, exported to a folder")
        self.act_shtgen.triggered.connect(self.open_sht_generator)
        self.act_vaggen = QAction("VAG Generator (audio→vag)…", self)
        self.act_vaggen.setToolTip("Convert wav/ogg to the game's VAG stream, exported to a folder")
        self.act_vaggen.triggered.connect(self.open_vag_generator)
        self.act_shtval = QAction("SHT Validator (check format)…", self)
        self.act_shtval.setToolTip("Validate a .sht chart against the official on-disc format")
        self.act_shtval.triggered.connect(self.open_sht_validator)

        for a in (self.act_open, self.act_extract_all, self.act_extract_sel,
                  self.act_replace, self.act_save, self.act_discard):
            tb.addAction(a)
        tb.addSeparator()
        tb.addAction(self.act_songmgr)

        menu = self.menuBar().addMenu("&File")
        menu.addAction(self.act_open)
        menu.addSeparator()
        menu.addAction(self.act_extract_all)
        menu.addAction(self.act_extract_sel)
        menu.addSeparator()
        menu.addAction(self.act_replace)
        menu.addAction(self.act_save)
        menu.addAction(self.act_discard)
        tb.addAction(self.act_songbuild)
        tb.addAction(self.act_packiso)
        tools = self.menuBar().addMenu("&Tools")
        tools.addAction(self.act_songmgr)
        tools.addAction(self.act_songreplace)
        tools.addAction(self.act_songbuild)
        tools.addAction(self.act_packiso)
        tools.addSeparator()
        tools.addAction(self.act_shtgen)
        tools.addAction(self.act_vaggen)
        tools.addAction(self.act_shtval)
        tools.addSeparator()
        tools.addAction(self.act_pfsshell)
        tools.addAction(self.act_ps2mc)
        tools.addAction(self.act_taikoexe)
        tools.addAction(self.act_taikotimer)
        tools.addAction(self.act_imgslim)
        tools.addAction(self.act_apamerge)
        tools.addAction(self.act_omnimix)
        tools.addAction(self.act_gen3conv)
        self._update_actions()

    def _update_actions(self):
        has = self.archive is not None
        for a in (self.act_extract_all, self.act_extract_sel, self.act_replace,
                  self.act_songmgr, self.act_songbuild, self.act_packiso):
            a.setEnabled(has)
        self.act_save.setEnabled(has and self.archive.dirty)
        self.act_discard.setEnabled(has and self.archive.dirty)

    # -- open -----------------------------------------------------------------
    def open_archive_dialog(self):
        folder = appconfig.pick_dir(self, "archive_dir",
                                    "Choose folder containing LIST.BIN + DATA.000")
        if folder:
            self.open_folder(Path(folder))

    def open_folder(self, folder: Path):
        list_path = _find_ci(folder, ["list.bin"])
        data_path = _find_ci(folder, ["data.000"])
        if not list_path or not data_path:
            QMessageBox.warning(self, "Not found",
                                f"Could not find both LIST.BIN and DATA.000 in:\n{folder}")
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
            QMessageBox.critical(self, "Open failed", f"{exc}\n\n{traceback.format_exc()}")
            return
        self.setWindowTitle(f"Taiko SYSTEM256 Explorer — {data_path}")
        self._populate_tree()
        a = self.archive
        log.info("loaded %d groups · %d files · DATA %.1f MB",
                 len(a.layout.groups), len(a.layout.files), a.data_len / 1e6)
        self.statusBar().showMessage(
            f"{len(a.layout.groups)} groups · {len(a.layout.files)} files · "
            f"DATA {a.data_len/1e6:.1f} MB")
        self._update_actions()

    def _populate_tree(self):
        self.model.removeRows(0, self.model.rowCount())
        if self.archive is None:
            return
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
            item.setEditable(False)
            item.setData("folder", ROLE_KIND)
            blanks = [QStandardItem() for _ in range(3)]
            for b in blanks:
                b.setEditable(False)
            parent.appendRow([item] + blanks)
            nodes[parts] = item
            return item

        for group in self.archive.layout.groups:
            parts = tuple(group["name"].split("."))
            gnode = get_folder(parts)
            gnode.setIcon(group_icon)
            gnode.setData("group", ROLE_KIND)
            gnode.setData(group["index"], ROLE_GROUP)
            for entry in self.archive.layout.files_for_group(group):
                name_item = QStandardItem(file_icon, entry["name"])
                name_item.setEditable(False)
                name_item.setData("file", ROLE_KIND)
                name_item.setData(group["index"], ROLE_GROUP)
                name_item.setData(entry["index"], ROLE_FILE)
                size_item = QStandardItem(f"{entry['size']:,}")
                comp = {2: "zlib", 6: "raw"}.get(group["compression"], str(group["compression"]))
                type_item = QStandardItem(comp)
                off_item = QStandardItem(f"0x{entry['offset']:X}")
                for it in (size_item, type_item, off_item):
                    it.setEditable(False)
                gnode.appendRow([name_item, size_item, type_item, off_item])

        self.tree.collapseAll()

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
            QMessageBox.critical(self, "Read failed", str(exc))
            return
        try:
            dlg = mod.Editor(data, f"{group['name']}/{entry['name']}", self)
        except Exception as exc:
            QMessageBox.critical(self, "Editor failed", f"{exc}\n\n{traceback.format_exc()}")
            return
        if dlg.exec() and dlg.result_bytes is not None and dlg.result_bytes != data:
            self.archive.stage_replace(gi, fi, dlg.result_bytes)
            font = item.font(); font.setBold(True); item.setFont(font)
            item.setForeground(QColor("#c0392b"))
            self.statusBar().showMessage(
                f"{entry['name']} edited ({len(dlg.result_bytes):,} bytes). Save to apply.")
            self._update_actions()

    def _open_swg_editor(self, item: QStandardItem):
        import swg_editor
        gi, fi = item.data(ROLE_GROUP), item.data(ROLE_FILE)
        group = self.archive.layout.groups[gi]
        entry = self.archive.layout.files[fi]
        try:
            data = self.archive.read_file(group, entry)
        except Exception as exc:
            QMessageBox.critical(self, "Read failed", str(exc))
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
            QMessageBox.critical(self, "Read failed", str(exc))
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
        import songtex
        gi, fi = item.data(ROLE_GROUP), item.data(ROLE_FILE)
        group = self.archive.layout.groups[gi]
        entry = self.archive.layout.files[fi]
        try:
            template = self.archive.read_file(group, entry)
            lay = tim2.first_picture_layout(template)
            if lay["image_type"] != 4 or (lay["width"], lay["height"]) != (640, 160):
                QMessageBox.warning(self, "Song texture",
                                    "This nut is not a 640×160 4-bit song-name slot.")
                return
        except Exception as exc:
            QMessageBox.critical(self, "Read failed", str(exc))
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
        import flipbook_player
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
            QMessageBox.information(self, "Frame Player", "No .nut frames found here.")
            return
        title = item.text() if item.text() else "frames"
        dlg = flipbook_player.FlipbookPlayer(frames, title, self)
        dlg.exec()

    def _play_sound_bank(self, gi: int):
        import audioplayer
        group = self.archive.layout.groups[gi]
        files = {e["name"]: e for e in self.archive.layout.files_for_group(group)}
        if "hd" not in files or "bd" not in files:
            QMessageBox.information(self, "Sound bank", "This group has no hd/bd pair.")
            return
        try:
            hd = self.archive.read_file(group, files["hd"])
            bd = self.archive.read_file(group, files["bd"])
            dlg = audioplayer.SoundBankDialog(hd, bd, group["name"], self)
        except Exception as exc:
            QMessageBox.critical(self, "Sound bank failed",
                                 f"{exc}\n\n{traceback.format_exc()}")
            return
        dlg.exec()

    def _export_one(self, item: QStandardItem):
        gi, fi = item.data(ROLE_GROUP), item.data(ROLE_FILE)
        group = self.archive.layout.groups[gi]
        entry = self.archive.layout.files[fi]
        dest = appconfig.pick_save(self, "export", "Export file", entry["name"])
        if not dest:
            return
        try:
            Path(dest).write_bytes(self.archive.read_file(group, entry))
            self.statusBar().showMessage(f"Exported {entry['name']} → {dest}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    # -- replace --------------------------------------------------------------
    def replace_selected(self):
        item = self._current_file_item()
        if not item:
            QMessageBox.information(self, "Replace", "Select a single file to replace.")
            return
        gi, fi = item.data(ROLE_GROUP), item.data(ROLE_FILE)
        entry = self.archive.layout.files[fi]
        src = appconfig.pick_open(self, "replace", f"Replace {entry['name']} with…")
        if not src:
            return
        data = Path(src).read_bytes()
        self.archive.stage_replace(gi, fi, data)
        # mark item visually
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setForeground(QColor("#c0392b"))
        row = item.index().row()
        parent = item.parent() or self.model.invisibleRootItem()
        size_item = parent.child(row, 1)
        if size_item:
            size_item.setText(f"{len(data):,}*")
        self._preview_item(item)
        self.statusBar().showMessage(
            f"Staged replacement: {entry['name']} ({len(data):,} bytes). Save to apply.")
        self._update_actions()

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
        if QMessageBox.question(self, "Discard", "Discard all staged replacements?") \
                != QMessageBox.Yes:
            return
        self.archive.discard_edits()
        self._populate_tree()
        self._update_actions()
        self.statusBar().showMessage("Edits discarded.")

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
        import song_manager
        needed = {"musicinfo.bin": None, "tuning.bin": None, "streaminfo.bin": None}
        located = {}
        for fn in needed:
            hit = self._find_archive_file(fn)
            if hit is None:
                QMessageBox.warning(self, "Song Manager",
                                    f"Could not find {fn} in this archive.")
                return
            located[fn] = hit
        try:
            data = {fn: self.archive.read_file(g, e) for fn, (g, e) in located.items()}
            dlg = song_manager.SongManager(
                data["musicinfo.bin"], data["tuning.bin"], data["streaminfo.bin"], self)
        except Exception as exc:
            QMessageBox.critical(self, "Song Manager failed",
                                 f"{exc}\n\n{traceback.format_exc()}")
            return
        if not dlg.exec() or not getattr(dlg, "result", None):
            return
        changed = 0
        for fn, new_bytes in dlg.result.items():
            if fn not in located or new_bytes == data[fn]:
                continue
            g, e = located[fn]
            self.archive.stage_replace(g["index"], e["index"], new_bytes)
            changed += 1
            self._mark_file_modified(g["index"], e["index"])
        if changed:
            self.statusBar().showMessage(
                f"Song Manager edited {changed} file(s). Save to apply to DATA.000.")
            self._update_actions()

    def _recent_hdd_img(self) -> str:
        """Last successfully opened HDD .img path (persisted via QSettings), or ""."""
        guess = QSettings("TaikoTools", "SYSTEM256Explorer").value("recent_hdd_img", "")
        guess = str(guess) if guess else ""
        return guess if guess and Path(guess).exists() else ""

    def open_hdd_wizard(self):
        import hdd_song_wizard
        dlg = hdd_song_wizard.HddSongWizard(self, default_img=self._recent_hdd_img())
        dlg.exec()

    def open_hdd_browser(self):
        import hdd_browser
        dlg = hdd_browser.HddBrowserDialog(self, default_img=self._recent_hdd_img())
        dlg.exec()

    def open_ps2mc(self):
        import ps2mc_card
        dlg = ps2mc_card.Ps2mcDialog(self, default_img=self._recent_ps2mc())
        dlg.exec()

    def _recent_ps2mc(self) -> str:
        try:
            import appconfig
            return appconfig.last_existing("ps2mc") or ""
        except Exception:
            return ""

    def open_pfsshell(self):
        import pfsshell_tool
        if not pfsshell_tool.find_pfsshell(self._recent_hdd_img() or None):
            QMessageBox.warning(
                self, "pfsshell",
                "pfsshell.exe not found. Put it next to this toolkit or next to "
                "your .img, or set the PFSSHELL_EXE environment variable.")
            return
        dlg = pfsshell_tool.PfsShellDialog(self, default_img=self._recent_hdd_img())
        dlg.exec()

    def open_taiko_exe(self):
        import taiko_exe
        try:
            taiko_exe.find_t14load()
        except taiko_exe.TaikoExeError as exc:
            QMessageBox.warning(self, "Patch taiko", str(exc))
            return
        dlg = taiko_exe.TaikoExeDialog(self, default_img=self._recent_hdd_img())
        dlg.exec()

    def open_taiko_timer(self):
        import taiko_exe
        try:
            taiko_exe.find_t14load()
        except taiko_exe.TaikoExeError as exc:
            QMessageBox.warning(self, "Song-select timer", str(exc))
            return
        dlg = taiko_exe.TaikoTimerDialog(self, default_img=self._recent_hdd_img())
        dlg.exec()

    def open_gen3_convert(self):
        import gen3_convert
        if gen3_convert.QDialog is None:
            QMessageBox.warning(self, "Gen3 → Gen2", "PySide6 is required.")
            return
        dlg = gen3_convert.Gen3ConvertDialog(self)
        dlg.exec()

    def open_img_slim(self):
        import img_slim
        if img_slim.QDialog is None:
            QMessageBox.warning(self, "Slim image", "PySide6 is required.")
            return
        if os.name != "nt":
            QMessageBox.warning(
                self, "Slim image",
                "Slimming uses NTFS sparse files and needs Windows.")
            return
        dlg = img_slim.ImgSlimDialog(self, default_img=self._recent_hdd_img() or "")
        dlg.exec()

    def open_apa_merge(self):
        import apa_merge
        if apa_merge.QDialog is None:
            QMessageBox.warning(self, "Merge HDD images", "PySide6 is required.")
            return
        dlg = apa_merge.ApaMergeDialog(self)
        dlg.exec()

    def open_omnimix(self):
        try:
            import omnimix_gui
        except Exception as exc:
            QMessageBox.warning(self, "Omnimix Maker", f"Could not load: {exc}")
            return
        dlg = omnimix_gui.OmnimixDialog(self, default_img=self._recent_hdd_img() or "")
        dlg.exec()

    def open_pack_iso(self):
        if not self.archive:
            return
        import iso_packer
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
            QMessageBox.information(self, "Song Replacer", "Open an archive first.")
            return
        if self.archive.dirty and QMessageBox.question(
                self, "Song Replacer",
                "There are unsaved staged edits. Replacing a song will add to them "
                "and then write everything out. Continue?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        import song_replacer
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
                self, "Write DATA.000",
                "Song staged into the slot. Write it into DATA.000 / list.bin now "
                "(boot-safe full rebuild, .bak backup)?",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.save_archive()

    def open_sht_generator(self):
        import generators
        generators.ShtGeneratorDialog(self).exec()

    def open_vag_generator(self):
        import generators
        generators.VagGeneratorDialog(self).exec()

    def open_sht_validator(self):
        import sht_validator
        sht_validator.ShtValidatorDialog(self).exec()

    def open_song_builder(self):
        if not self.archive:
            return
        import song_builder
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
                self, "Write new archive",
                f"A new song was built. Overwrite (with .bak backup)?\n\n"
                f"{list_path}\n{data_path}") != QMessageBox.Yes:
            return
        prog = QProgressDialog("Writing rebuilt archive…", None, 0, 0, self)
        prog.setWindowModality(Qt.WindowModal); prog.setMinimumDuration(0)
        QApplication.processEvents()
        try:
            a.close()
            for p in (list_path, data_path):
                _make_backup(p)
            _atomic_write(data_path, data_bytes)
            _atomic_write(list_path, list_bytes)
        except Exception as exc:
            prog.close()
            QMessageBox.critical(self, "Write failed", str(exc))
            self._reload_after_save(list_path, data_path)
            return
        prog.close()
        self._reload_after_save(list_path, data_path)
        QMessageBox.information(
            self, "New song added",
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

        prog_total = len(a.layout.groups) if full_rebuild else len(a.affected_groups())
        verb = "Rebuilding" if full_rebuild else "Patching"
        prog = QProgressDialog(f"{verb}…", "Cancel", 0, prog_total, self)
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
            if self.chk_overwrite.isChecked():
                # close mmap before overwriting the file we are mmap'ing
                a.close()
                for p in (a.list_path, a.data_path):
                    _make_backup(p)
            if full_rebuild:
                summary = a.rebuild(out_list, out_data, report)
            else:
                summary = a.save(out_list, out_data, self.chk_relayout.isChecked(), report)
        except Exception as exc:
            log.error("save failed: %s", exc)
            prog.close()
            hint = ""
            if not full_rebuild and "fit" in str(exc).lower():
                hint = ("\n\nTip: tick 'full rebuild' in Save options to allow "
                        "larger replacements (it rewrites the DATA layout).")
            QMessageBox.critical(self, "Save failed", str(exc) + hint)
            # reopen mmap so the app stays usable
            self._reload_after_save(a.list_path, a.data_path)
            return
        prog.close()
        took = _fmt_duration(time.perf_counter() - t0)
        log.info("%s complete: %d group(s), %d file(s) in %s",
                 "rebuild" if full_rebuild else "patch",
                 summary['changed_groups'], summary['files'], took)
        extra = (f"\nDATA size: {summary['data_size']/1e6:.1f} MB" if full_rebuild else "")
        QMessageBox.information(
            self, "Saved",
            f"{'Rebuilt' if full_rebuild else 'Patched'} {summary['changed_groups']} group(s), "
            f"{summary['files']} file(s) in {took}.{extra}\n\n{out_list}\n{out_data}")
        # reload from the written files so further edits start clean
        self._reload_after_save(out_list if not self.chk_overwrite.isChecked() else a.list_path,
                                out_data if not self.chk_overwrite.isChecked() else a.data_path)

    def _reload_after_save(self, list_path: Path, data_path: Path):
        try:
            self.archive = Archive(list_path, data_path, fmt=2)
        except Exception as exc:
            # the old archive is already closed; do not leave a dangling handle
            self.archive = None
            QMessageBox.critical(self, "Reload failed", str(exc))
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
            QMessageBox.information(self, "Extract", "Nothing selected to extract.")
            return
        out = appconfig.pick_dir(self, "extract_dir", "Extract to folder")
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
            "Cancel", 0, len(groups), self)
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        # we close the dialog explicitly in on_done; don't let reaching the max
        # value auto-close/reset it and race the cancel signal.
        prog.setAutoClose(False)
        prog.setAutoReset(False)

        self.worker = ExtractWorker(self.archive, out_dir, groups, workers,
                                    write_manifest, convert_png)

        def on_progress(done, total, name):
            prog.setValue(done)
            prog.setLabelText(f"[{done}/{total}] {name}")

        def on_done(g, f, pngs, elapsed, error, png_skipped):
            prog.close()
            took = _fmt_duration(elapsed)
            rate = f"{f/elapsed:,.0f} files/s" if elapsed > 0 else ""
            mtxt = " · manifest.json written" if write_manifest else ""
            ptxt = f" · {pngs:,} PNG" if convert_png else ""
            stxt = f" · {png_skipped:,} texture(s) skipped" if png_skipped else ""
            if error:
                QMessageBox.critical(self, "Extract failed", error)
            else:
                QMessageBox.information(
                    self, "Extract complete",
                    f"Extracted {g} groups, {f} files{ptxt} in {took} ({rate}){mtxt}{stxt}\n→ {out_dir}")
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
                self, "Unsaved edits",
                "You have unsaved replacements. Quit anyway?",
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
        super().closeEvent(event)


_BIN_EDITORS: dict | None = None


def _bin_editor_module(filename: str):
    """Return the bineditor_* module registered for this .bin filename, or None.

    Modules are discovered lazily by importing every bineditor_*.py sibling and
    reading its FILENAME constant. A broken module is skipped, not fatal.
    """
    global _BIN_EDITORS
    if _BIN_EDITORS is None:
        import importlib
        _BIN_EDITORS = {}
        stems = sorted(p.stem for p in
                       Path(__file__).resolve().parent.glob("bineditor_*.py"))
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
                mod = importlib.import_module(stem)
                fn = getattr(mod, "FILENAME", None)
                if fn and hasattr(mod, "Editor"):
                    _BIN_EDITORS[fn.lower()] = mod
            except Exception as exc:
                log.debug("bin-editor: skipped %s: %s", stem, exc)
    return _BIN_EDITORS.get(Path(filename).name.lower())


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


def main():
    argv = [a for a in sys.argv if a != "--debug"]
    _setup_logging(sys.argv)
    log.info("Taiko SYSTEM256 Explorer starting (log level %s)",
             logging.getLevelName(log.level))
    app = QApplication(argv)
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

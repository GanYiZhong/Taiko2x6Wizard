"""Drag and drop onto the main window: decide which tool a dropped file or
folder belongs to.

Detection is read-only and cheap: names, sizes, extensions and a few magic
bytes (no archive is opened and no big file is read). ``classify`` returns a
kind; ``candidates`` turns it into the tools that make sense, and
``OpenWithDialog`` lets the user pick when there is more than one.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox,
                               QLabel, QListWidget, QListWidgetItem,
                               QVBoxLayout)

from taiko2x6.app.dialog_polish import polish
from taiko2x6.app.i18n import tr

_CHART_SUFFIXES = (".tja", ".sht", ".nus3bank")
_MAX_BIN_PROBE = 8 * 1024 * 1024      # a Gen3 fumen / sht is tiny
_HDD_MIN = 1024 * 1024


def _find_ci(folder: Path, names: tuple[str, ...]) -> Path | None:
    try:
        for p in folder.iterdir():
            if p.name.lower() in names and p.is_file():
                return p
    except OSError:
        pass
    return None


def _head(path: Path, offset: int, n: int) -> bytes:
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(n)
    except OSError:
        return b""


def _folder_kind(folder: Path) -> str:
    if _find_ci(folder, ("list.bin",)) and _find_ci(folder, ("data.000",)):
        return "archive"
    try:
        for p in folder.iterdir():
            n = p.name.lower()
            if p.is_file() and n.endswith(_CHART_SUFFIXES):
                return "chart"
            if p.is_dir() and n == "sht":
                return "chart"
    except OSError:
        pass
    return "unknown"


def _is_dongle_card(path: Path, size: int) -> bool:
    if _head(path, 0, 28) == b"Sony PS2 Memory Card Format ":
        return True
    try:
        from taiko2x6.exe import usb_keyboard
        return usb_keyboard._is_card(path)
    except Exception:
        return size in (8650752, 8388608)


def classify(path) -> str:
    """'archive' | 'hdd' | 'iso' | 'card' | 't14load' | 'chart' | 'unknown'."""
    p = Path(path)
    try:
        if p.is_dir():
            return _folder_kind(p)
        if not p.is_file():
            return "unknown"
        size = p.stat().st_size
    except OSError:
        return "unknown"
    name, suf = p.name.lower(), p.suffix.lower()
    if name in ("data.000", "list.bin"):
        d = p.parent
        if _find_ci(d, ("list.bin",)) and _find_ci(d, ("data.000",)):
            return "archive"
    if name.startswith("t14load"):
        return "t14load"
    if suf in _CHART_SUFFIXES or name == "sht":
        return "chart"
    if _head(p, 0x8001, 5) == b"CD001":
        return "iso"
    if size >= _HDD_MIN and _head(p, 4, 4) == b"APA\x00":
        return "hdd"
    if _is_dongle_card(p, size):
        return "card"
    if suf == ".bin" and size <= _MAX_BIN_PROBE:
        try:
            from taiko2x6.charts import chart_convert
            if chart_convert.detect(p) in ("bin", "sht"):
                return "chart"
        except Exception:
            pass
    return "unknown"


def classify_all(paths) -> tuple[str, list[str]]:
    """One kind for the whole drop. Several chart files / folders together are
    one 'chart' drop; otherwise the first path decides and the rest is ignored."""
    paths = [str(p) for p in paths]
    kinds = [classify(p) for p in paths]
    if kinds and all(k == "chart" for k in kinds):
        return "chart", paths
    return (kinds[0], paths[:1]) if kinds else ("unknown", [])


def candidates(kind: str, has_archive: bool = False) -> list[tuple[str, str, str]]:
    """(key, title, one-line description) of the tools that fit, best first.
    Key ``k`` is handled by ``MainWindow._drop_<k>``."""
    if kind == "archive":
        return [("archive", tr("Open archive"),
                 tr("Browse, preview and replace files of DATA.000 + list.bin."))]
    if kind == "t14load":
        return [("t14load", tr("Use as T14LOAD key file"),
                 tr("Save it in Settings for the patch tools."))]
    if kind == "hdd":
        return [
            ("hdd_browser", tr("HDD browser"),
             tr("Browse the image and extract or replace DATA.000 / list.bin.")),
            ("hdd_wizard", tr("HDD Song Wizard"),
             tr("Add or replace a song directly in this image.")),
            ("omnimix_target", tr("Omnimix Maker (as target)"),
             tr("Merge songs from other images into this one.")),
            ("usb_keyboard", tr("USB keyboard patch"),
             tr("Play Taiko 14 with a USB keyboard; this image is the target.")),
            ("slim", tr("Slim image"),
             tr("Make this image smaller.")),
        ]
    if kind == "iso":
        out = [("omnimix_source", tr("Omnimix Maker (as source)"),
                tr("Take songs from this game ISO."))]
        if has_archive:
            out.append(("pack_iso", tr("Pack to ISO"),
                        tr("Inject the open DATA.000 + list.bin into this ISO.")))
        return out
    if kind == "card":
        return [
            ("ps2mc", tr("Memory card browser"),
             tr("Browse the memory-card image and its saves.")),
            ("usb_keyboard_card", tr("USB keyboard patch"),
             tr("Use this dongle card as the stock module source.")),
        ]
    if kind == "chart":
        out = [("chart_convert", tr("Chart converter"),
                tr("Convert TJA / sht / Gen3 .bin charts (files are prefilled)."))]
        if has_archive:
            out.append(("song_builder", tr("Custom Song Builder"),
                        tr("Build a song into the open archive from a TJA and audio.")))
        return out
    return []


class OpenWithDialog(QDialog):
    """Small chooser: which tool should open the dropped item?"""

    def __init__(self, label: str, options, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Open with…"))
        self.resize(480, 300)
        self.chosen = ""
        lay = QVBoxLayout(self)
        self.lbl = QLabel(tr("What should be done with {name}?").format(name=label))
        self.lbl.setWordWrap(True)
        lay.addWidget(self.lbl)
        self.listing = QListWidget()
        self.listing.setSelectionMode(QAbstractItemView.SingleSelection)
        for key, title, desc in options:
            it = QListWidgetItem(f"{title}\n{desc}")
            it.setData(Qt.UserRole, key)
            self.listing.addItem(it)
        if self.listing.count():
            self.listing.setCurrentRow(0)
        self.listing.itemDoubleClicked.connect(lambda _i: self.accept())
        lay.addWidget(self.listing, 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.Ok).setText(tr("Open"))
        self.buttons.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))
        lay.addWidget(self.buttons)
        self.lbl.setBuddy(self.listing)
        polish(self, primary=self.buttons.button(QDialogButtonBox.Ok))

    def accept(self):
        it = self.listing.currentItem()
        self.chosen = it.data(Qt.UserRole) if it is not None else ""
        if self.chosen:
            super().accept()

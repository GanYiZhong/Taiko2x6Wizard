#!/usr/bin/env python3
"""Settings dialog: the external things the tools need, and whether they are found.

Each row shows a status (found / missing), the resolved path, Browse and Detect
buttons and a one-line note of what needs it. Choices are stored through
appconfig (the same ``[paths]`` keys the tools' own pickers use), so a path set
here is what the tools see.
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QGridLayout, QLabel,
                               QMessageBox, QPushButton, QVBoxLayout)

from taiko2x6.app import appconfig
from taiko2x6.app.i18n import tr

# key, label, what needs it, kind ("file" | "dir"), file filter, important?
# (label / note are looked up with tr() at display time; zh-TW entries live in
# i18n.py and tests/test_settings.py checks them.)
DEPS = (
    ("t14load", "T14LOAD key file",
     "Boot & select patches, USB keyboard patch, taiko exe patches.",
     "file", "T14LOAD (T14LOAD.bin);;All (*)", True),
    ("vgmstream", "vgmstream",
     "Converting nus3bank audio (Taiko 3 song import).",
     "file", "vgmstream-cli (vgmstream-cli*);;All (*)", True),
    ("ps2mc", "Dongle card image",
     "USB keyboard patch on the card, memory-card browser.",
     "file", "Card image (*.ps2 *.mc2 *.bin *.img);;All (*)", False),
    ("library", "Game library folder",
     "Where your game images / archives are kept; dialogs open here.",
     "dir", "", False),
)


def _detect(key: str) -> str:
    """Best automatic guess for `key` ('' if none)."""
    if key == "t14load":
        from taiko2x6.exe import taiko_exe
        try:
            return taiko_exe.find_t14load()
        except taiko_exe.TaikoExeError:
            return ""
    if key == "vgmstream":
        from taiko2x6.charts import gen3_convert
        return gen3_convert.find_vgmstream() or ""
    if key == "library":
        env = os.environ.get("TAIKO_GAME_LIBRARY", "")
        return env if env and Path(env).is_dir() else ""
    return ""


def resolve(key: str) -> str:
    """The path the tools will actually use for `key` ('' = missing)."""
    if key in ("t14load", "vgmstream"):
        return _detect(key)             # the finders already honour the saved path
    return appconfig.last_existing(key) or _detect(key)


def status() -> list:
    """One dict per dependency: key, label, note, path, found, important."""
    out = []
    for key, label, note, _kind, _filt, important in DEPS:
        path = resolve(key)
        out.append(dict(key=key, label=label, note=note, path=path,
                        found=bool(path), important=important))
    return out


def missing_important() -> list:
    return [d for d in status() if d["important"] and not d["found"]]


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Settings"))
        self.setMinimumWidth(680)
        self._rows = {}
        v = QVBoxLayout(self)
        intro = QLabel(tr("External files the tools rely on. A missing item "
                          "only disables the tools that need it."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)
        for r, (key, label, note, _kind, _filt, _imp) in enumerate(DEPS):
            name = QLabel(f"<b>{tr(label)}</b><br><span style='color:#888'>"
                          f"{tr(note)}</span>")
            name.setWordWrap(True)
            st = QLabel()
            st.setWordWrap(True)
            st.setTextInteractionFlags(Qt.TextSelectableByMouse)
            b = QPushButton(tr("Browse…"))
            d = QPushButton(tr("Detect"))
            b.clicked.connect(lambda _=False, k=key: self._browse(k))
            d.clicked.connect(lambda _=False, k=key: self._detect_one(k))
            d.setEnabled(key != "ps2mc")          # nothing sensible to scan for
            grid.addWidget(name, r, 0)
            grid.addWidget(st, r, 1)
            grid.addWidget(b, r, 2)
            grid.addWidget(d, r, 3)
            self._rows[key] = st
        self.btn_reset = QPushButton(tr("Forget all remembered paths"))
        self.btn_reset.clicked.connect(self._reset)
        v.addWidget(self.btn_reset)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.refresh()

    def refresh(self):
        for d in status():
            lab = self._rows[d["key"]]
            if d["found"]:
                lab.setText(f"<span style='color:#2a9d3a'>{tr('Found')}</span>"
                            f"<br>{d['path']}")
            elif d["important"]:
                lab.setText(f"<span style='color:#c0392b'>{tr('Missing')}</span>")
            else:
                lab.setText(f"<span style='color:#888'>{tr('Not set')}</span>")

    def _browse(self, key):
        _k, label, _n, kind, filt, _i = next(x for x in DEPS if x[0] == key)
        if kind == "dir":
            appconfig.pick_dir(self, key, tr(label))
        else:
            appconfig.pick_open(self, key, tr(label), filt)
        self.refresh()

    def _detect_one(self, key):
        found = _detect(key)
        if found:
            appconfig.remember(key, found)
        else:
            QMessageBox.information(
                self, tr("Detect"),
                tr("Nothing found automatically. Use Browse… to pick it."))
        self.refresh()

    def _reset(self):
        if QMessageBox.question(
                self, tr("Settings"),
                tr("Forget every remembered path? File pickers will start "
                   "from scratch. Files on disk are not touched.")) \
                == QMessageBox.Yes:
            appconfig.clear_paths()
            self.refresh()

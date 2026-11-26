"""Small shared widgets for the disc / image utility dialogs (ISO packer,
HDD slimmer, APA merger, pfsshell): a path row with a Browse button that
remembers the last folder, an auto-scrolling log, a palette-based problem
label and the free-space helper. Colours come from the palette so both the
light and dark themes read well."""
from __future__ import annotations

import os
import shutil

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QWidget)

from taiko2x6.app import appconfig
from taiko2x6.app.i18n import tr


class PathRow(QWidget):
    """Line edit + Browse button. mode is "open" or "save"; `key` is the
    appconfig key remembering the last location."""
    changed = Signal()

    def __init__(self, key, title, filt="All (*)", mode="open", text="",
                 parent=None):
        super().__init__(parent)
        self.key, self.title, self.filt, self.mode = key, title, filt, mode
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(text)
        self.edit.textChanged.connect(lambda *_: self.changed.emit())
        self.btn = QPushButton(tr("Browse…"))
        self.btn.setAutoDefault(False)
        self.btn.clicked.connect(self._browse)
        h.addWidget(self.edit, 1)
        h.addWidget(self.btn)

    def text(self):
        return self.edit.text().strip()

    def setText(self, s):
        self.edit.setText(s)

    def _browse(self):
        if self.mode == "save":
            p = appconfig.pick_save(self, self.key, self.title, "", self.filt)
        else:
            p = appconfig.pick_open(self, self.key, self.title, self.filt)
        if p:
            self.edit.setText(p)


def make_log() -> QPlainTextEdit:
    log = QPlainTextEdit()
    log.setReadOnly(True)
    return log


def log_line(log: QPlainTextEdit, text: str) -> None:
    """Append a line and keep the newest one in view."""
    log.appendPlainText(text)
    sb = log.verticalScrollBar()
    sb.setValue(sb.maximum())


def make_problem_label() -> QLabel:
    lb = QLabel()
    lb.setWordWrap(True)
    f = lb.font()
    f.setBold(True)
    lb.setFont(f)
    return lb


def set_problem(lb: QLabel, text: str) -> None:
    lb.setText(("⚠ " + text) if text else "")


def free_space(path):
    """Free bytes on the drive that will hold `path` (None if unknown)."""
    try:
        d = os.path.dirname(os.path.abspath(path)) or "."
        return shutil.disk_usage(d).free
    except Exception:
        return None


def parent_dir_ok(path: str) -> bool:
    d = os.path.dirname(os.path.abspath(path))
    return os.path.isdir(d)

#!/usr/bin/env python3
"""
Shared UI pieces for the Song Builder and Song Replacer dialogs.

Display-only helpers: nothing here touches the build logic or the bytes it
produces. The TJA summary is a read-only preview built with the same parser the
builder uses, so what the table shows is what the converter will see.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QWidget,
)

from taiko2x6.app.i18n import tr

_COURSE_NAMES = {0: "easy", 1: "normal", 2: "hard", 3: "oni", 4: "ura"}
MAX_LINT_WARNINGS = 6
_BAD_STYLE ="border:1px solid palette(bright-text);"


def summarize_tja(text: str) -> dict:
    """Parse TJA text for display.

    Returns {title, bpm, offset, rows: [(course, level, notes)], warnings}.
    Never raises: a parse failure comes back as a warning.
    """
    out = {"title": "", "bpm": "", "offset": "", "rows": [], "warnings": []}
    try:
        from taiko2x6.charts import tja2sht
        parsed = tja2sht.parse_tja(text)
    except Exception as exc:
        out["warnings"].append(str(exc))
        return out
    meta = parsed.get("meta", {}) if isinstance(parsed, dict) else {}
    out["title"] = str(meta.get("TITLE", ""))
    out["bpm"] = str(meta.get("BPM", ""))
    out["offset"] = str(meta.get("OFFSET", ""))
    courses = parsed.get("courses", {}) if isinstance(parsed, dict) else {}
    for cidx in sorted(courses, key=lambda k: (not isinstance(k, int), k)):
        c = courses[cidx]
        notes = 0
        for m in c.get("measures", []):
            notes += sum(1 for ch in m.get("notes", "") if ch in "1234")
        name = _COURSE_NAMES.get(cidx, str(cidx)) if isinstance(cidx, int) else str(cidx)
        out["rows"].append((name, c.get("meta", {}).get("LEVEL"), notes))
    if not out["rows"]:
        out["warnings"].append("no playable courses found")
    if not out["bpm"]:
        out["warnings"].append("no BPM: header")
    try:
        from taiko2x6.charts import tja_lint
        found = [i for i in tja_lint.lint_tja(text)
                 if i.severity == tja_lint.WARNING]
        lines = [tja_lint.format_issue(i) for i in found[:MAX_LINT_WARNINGS]]
    except Exception:
        found, lines = [], []
    out["warnings"].extend(lines)
    if len(found) > MAX_LINT_WARNINGS:
        out["warnings"].append(tr("(+{n} more; see Check TJA… in the chart "
                                  "converter)").format(
                                      n=len(found) - MAX_LINT_WARNINGS))
    return out


class SummaryTable(QTableWidget):
    """Compact course | stars | notes table (3-5 rows, no scrolling)."""

    def __init__(self, parent=None):
        super().__init__(0, 3, parent)
        self.setHorizontalHeaderLabels([tr("Course"), tr("Stars"), tr("Notes")])
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.setMaximumHeight(140)

    def set_rows(self, rows):
        self.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                self.setItem(r, c, QTableWidgetItem("" if v is None else str(v)))
        self.setVisible(bool(rows))


def tja_summary_line(s: dict) -> str:
    """One-line header: title, BPM, offset."""
    parts = []
    if s["title"]:
        parts.append(tr("Title: {t}").format(t=s["title"]))
    if s["bpm"]:
        parts.append("BPM %s" % s["bpm"])
    if s["offset"]:
        parts.append("OFFSET %s" % s["offset"])
    return "   ".join(parts)


def path_row(edit, btn) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(edit, 1)
    h.addWidget(btn)
    return w


def mark_bad(widget, bad: bool, tip: str = "") -> None:
    """Red border + tooltip on a line edit that blocks the build."""
    widget.setStyleSheet(_BAD_STYLE if bad else "")
    if bad:
        widget.setToolTip(tip)


def make_log() -> QPlainTextEdit:
    log = QPlainTextEdit(readOnly=True)
    log.setMaximumBlockCount(5000)
    return log


def log_append(log: QPlainTextEdit, text: str) -> None:
    """Append and keep the newest line in view."""
    log.appendPlainText(text)
    log.moveCursor(QTextCursor.End)
    log.ensureCursorVisible()


def make_buttons(build_text: str, close_text: str, on_build, on_close):
    """Build (primary/default) + Close in a QDialogButtonBox.

    Returns (box, build_button). Handlers are wired to the buttons directly so
    the box's accepted/rejected signals never close the dialog by themselves.
    """
    box = QDialogButtonBox()
    b_build = box.addButton(build_text, QDialogButtonBox.AcceptRole)
    b_close = box.addButton(close_text, QDialogButtonBox.RejectRole)
    b_build.setDefault(True)
    b_close.setAutoDefault(False)
    b_build.clicked.connect(on_build)
    b_close.clicked.connect(on_close)
    return box, b_build


def status_label() -> QLabel:
    lbl = QLabel()
    lbl.setWordWrap(True)
    return lbl


def set_status(lbl: QLabel, problems: list) -> None:
    if problems:
        lbl.setText("⚠ " + problems[0] +
                    ("  (+%d)" % (len(problems) - 1) if len(problems) > 1 else ""))
        lbl.setStyleSheet("color:palette(bright-text);")
    else:
        lbl.setText("✓ " + tr("Ready to build."))
        lbl.setStyleSheet("")


def file_problem(path: str, what: str):
    """Message when a non-empty path does not exist, else None."""
    path = (path or "").strip()
    if path and not Path(path).exists():
        return tr("{what} not found: {path}").format(what=what, path=path)
    return None

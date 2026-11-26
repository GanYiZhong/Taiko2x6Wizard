"""Shared usability helpers for the table editors (bineditor_*.py).

* TableTools   - per-table: edited-cell highlight, integer-range check,
                 Ctrl+C / Ctrl+V of cell blocks (tab/newline separated, so it
                 pastes to and from spreadsheets), row filter, revert.
* ToolBar      - search box + "Revert edits" button + edited counter for one
                 or more TableTools.
* remember_size - restore / persist a dialog's window size.

None of this touches the bytes an editor serializes; it only changes how the
grid is presented and edited.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QEvent
from PySide6.QtGui import QBrush, QColor, QKeySequence
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QWidget)

from taiko2x6.app import appconfig
from taiko2x6.app.i18n import tr

_EDITED = QBrush(QColor(255, 190, 0, 90))
_INVALID = QBrush(QColor(230, 40, 40, 110))
_NONE = QBrush()
I32 = (-(1 << 31), (1 << 32) - 1)     # i32 and u32 both pass


def parse_int(text: str) -> int:
    """Decimal or 0x-hex (plain int() semantics first, so '010' stays 10)."""
    t = str(text).strip()
    try:
        return int(t)
    except ValueError:
        return int(t, 0)


class TableTools(QObject):
    """Decorate a filled QTableWidget. Call after the items are set."""

    def __init__(self, table, int_cols=None, int_range=I32, row_text=None,
                 col_tips=None, max_len=None):
        super().__init__(table)
        self.table = table
        self.int_range = int_range
        self.row_text = row_text            # callable(row) -> extra searchable text
        self._base = {}
        self._busy = False
        self.on_change = []
        self.max_len = dict(max_len or {})  # col -> max characters (text columns)
        self.int_cols = set(range(table.columnCount())) if int_cols is None else set(int_cols)
        for r in range(table.rowCount()):
            for c in range(table.columnCount()):
                it = table.item(r, c)
                if it is not None and it.flags() & Qt.ItemIsEditable:
                    self._base[(r, c)] = it.text()
        for c, tip in enumerate(col_tips or []):
            h = table.horizontalHeaderItem(c)
            if tip and h is not None:
                h.setToolTip(tip)
        table.itemChanged.connect(self._changed)
        table.installEventFilter(self)
        self._tip_range = tr("Integer between {lo} and {hi} (0x... hex allowed)").format(
            lo=int_range[0], hi=int_range[1])

    # -- state ----------------------------------------------------------
    def _bad(self, c, text):
        if c in self.max_len:
            return len(text) > self.max_len[c]
        if c not in self.int_cols:
            return False
        try:
            lo, hi = self.int_range
            return not (lo <= parse_int(text) <= hi)
        except ValueError:
            return True

    def _style(self, it, r, c):
        base = self._base.get((r, c))
        if base is None:
            return
        bad = self._bad(c, it.text())
        edited = it.text() != base
        it.setBackground(_INVALID if bad else _EDITED if edited else _NONE)
        tip = (tr("At most {n} characters").format(n=self.max_len[c])
               if c in self.max_len else self._tip_range)
        it.setToolTip(tip if bad else
                      tr("Edited (was: {old})").format(old=base) if edited else "")

    def _changed(self, it):
        if self._busy:
            return
        self._style(it, it.row(), it.column())
        for cb in self.on_change:
            cb()

    def edited_count(self) -> int:
        n = 0
        for (r, c), base in self._base.items():
            it = self.table.item(r, c)
            if it is not None and it.text() != base:
                n += 1
        return n

    def invalid_count(self) -> int:
        n = 0
        for (r, c) in self._base:
            it = self.table.item(r, c)
            if it is not None and self._bad(c, it.text()):
                n += 1
        return n

    def revert(self):
        self._busy = True
        try:
            for (r, c), base in self._base.items():
                it = self.table.item(r, c)
                if it is not None:
                    it.setText(base)
                    it.setBackground(_NONE)
                    it.setToolTip("")
        finally:
            self._busy = False
        for cb in self.on_change:
            cb()

    # -- filter ---------------------------------------------------------
    def filter(self, text: str):
        words = text.lower().split()
        t = self.table
        for r in range(t.rowCount()):
            if not words:
                t.setRowHidden(r, False)
                continue
            vh = t.verticalHeaderItem(r)
            parts = [vh.text() if vh else "", self.row_text(r) if self.row_text else ""]
            for c in range(t.columnCount()):
                it = t.item(r, c)
                if it is not None:
                    parts.append(it.text())
            hay = " ".join(parts).lower()
            t.setRowHidden(r, not all(w in hay for w in words))

    # -- copy / paste ---------------------------------------------------
    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.KeyPress and obj is self.table:
            if ev.matches(QKeySequence.Copy):
                self.copy()
                return True
            if ev.matches(QKeySequence.Paste):
                self.paste()
                return True
        return False

    def copy(self):
        rng = self.table.selectedRanges()
        if not rng:
            return
        r = rng[0]
        lines = []
        for row in range(r.topRow(), r.bottomRow() + 1):
            if self.table.isRowHidden(row):
                continue
            lines.append("\t".join(
                (self.table.item(row, c).text() if self.table.item(row, c) else "")
                for c in range(r.leftColumn(), r.rightColumn() + 1)))
        QApplication.clipboard().setText("\n".join(lines))

    def paste(self):
        text = QApplication.clipboard().text()
        if not text:
            return
        rows = [ln.split("\t") for ln in text.replace("\r", "").split("\n") if ln != ""]
        cur = self.table.currentIndex()
        sel = self.table.selectedRanges()
        r0 = sel[0].topRow() if sel else cur.row()
        c0 = sel[0].leftColumn() if sel else cur.column()
        if r0 < 0 or c0 < 0:
            return
        # one value over a larger selection fills the selection
        if len(rows) == 1 and len(rows[0]) == 1 and sel and (
                sel[0].rowCount() > 1 or sel[0].columnCount() > 1):
            rows = [[rows[0][0]] * sel[0].columnCount() for _ in range(sel[0].rowCount())]
        r = r0
        for line in rows:
            while r < self.table.rowCount() and self.table.isRowHidden(r):
                r += 1
            if r >= self.table.rowCount():
                break
            for dc, val in enumerate(line):
                it = self.table.item(r, c0 + dc)
                if it is not None and it.flags() & Qt.ItemIsEditable:
                    it.setText(val)
            r += 1


class ToolBar(QWidget):
    """Filter box, edited counter and Revert button for a set of tables."""

    def __init__(self, tools, hint=None, parent=None):
        super().__init__(parent)
        self.tools = list(tools)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel(tr("Filter:")))
        self.ed = QLineEdit()
        self.ed.setClearButtonEnabled(True)
        self.ed.setPlaceholderText(
            hint or tr("Type to filter rows (several words: all must match)"))
        self.ed.textChanged.connect(self._filter)
        lay.addWidget(self.ed, 1)
        self.lbl = QLabel()
        lay.addWidget(self.lbl)
        self.btn = QPushButton(tr("Revert edits"))
        self.btn.setToolTip(tr("Undo every change made in this window since it was opened"))
        self.btn.clicked.connect(self.revert)
        lay.addWidget(self.btn)
        for t in self.tools:
            t.on_change.append(self.refresh)
        self.refresh()

    def _filter(self, text):
        for t in self.tools:
            t.filter(text)

    def revert(self):
        for t in self.tools:
            t.revert()

    def refresh(self):
        n = sum(t.edited_count() for t in self.tools)
        bad = sum(t.invalid_count() for t in self.tools)
        s = tr("{n} edited").format(n=n)
        if bad:
            s += " | " + tr("{n} invalid").format(n=bad)
        self.lbl.setText(s)
        self.btn.setEnabled(n > 0)


def remember_size(dlg, key: str, default=None):
    """Restore the saved size of `dlg` and save it again when it closes."""
    sec = "editor_size"
    try:
        w, h = (int(x) for x in appconfig.get(sec, key, "").split("x"))
        if w >= 300 and h >= 200:
            dlg.resize(w, h)
    except ValueError:
        if default:
            dlg.resize(*default)

    def save(_=0):
        appconfig.set_value(sec, key, f"{dlg.width()}x{dlg.height()}")
    dlg.finished.connect(save)

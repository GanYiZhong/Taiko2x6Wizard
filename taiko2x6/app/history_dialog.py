"""Help > Activity history...: the persistent log of write operations
(``taiko2x6.history``) as a filterable table, with shortcuts to the folder a
change went to and to Backups & undo for its undo journal / .bak."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QDate, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDateEdit,
                               QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from taiko2x6 import history
from taiko2x6.app.dialog_polish import polish
from taiko2x6.app.i18n import tr

_ANY = QDate(2000, 1, 1)               # minimum date = "Any" (special value text)


def filter_entries(entries, tool="", target="", since=None, until=None,
                   failed_only=False):
    """Entries (dicts) matching every given filter. ``since`` / ``until`` are
    'YYYY-MM-DD' strings (inclusive) or empty."""
    out = []
    t = target.strip().lower()
    for e in entries:
        if tool and e.get("tool") != tool:
            continue
        if failed_only and e.get("ok", True):
            continue
        day = str(e.get("time", ""))[:10]
        if since and day < since:
            continue
        if until and day > until:
            continue
        if t and not any(t in x.lower() for x in e.get("targets", [])):
            continue
        out.append(e)
    return out


def undo_start_path(e) -> str:
    """What Backups & undo should be pointed at for entry ``e``: the image
    itself for an undo journal, else the folder holding the .bak files."""
    undo = e.get("undo") or ""
    tg = (e.get("targets") or [""])[0]
    if undo.endswith((".undo", ".undone")) and tg:
        return tg
    base = tg or undo
    return str(Path(base).parent) if base else ""


class HistoryDialog(QDialog):
    NO_DEFAULT_REASON = ""

    def __init__(self, parent=None, entries=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Activity history"))
        self.resize(900, 520)
        self._all = list(history.load() if entries is None else entries)
        self._shown: list[dict] = []

        lay = QVBoxLayout(self)
        info = QLabel(tr("Every write the toolkit made: archive saves, HDD image "
                         "writes and game patches. Only paths and a short summary "
                         "are kept, never file contents."))
        info.setWordWrap(True)
        lay.addWidget(info)

        form = QFormLayout()
        self.cb_tool = QComboBox()
        self.cb_tool.addItem(tr("All tools"), "")
        for t in sorted({str(e.get("tool", "")) for e in self._all if e.get("tool")}):
            self.cb_tool.addItem(tr(t), t)
        form.addRow(tr("Tool:"), self.cb_tool)
        self.ed_target = QLineEdit()
        self.ed_target.setClearButtonEnabled(True)
        form.addRow(tr("Target contains:"), self.ed_target)
        row = QHBoxLayout()
        self.d_from = self._date_edit()
        self.d_to = self._date_edit()
        row.addWidget(self.d_from)
        row.addWidget(QLabel(tr("to")))
        row.addWidget(self.d_to)
        row.addStretch(1)
        form.addRow(tr("From:"), row)
        self.ck_failed = QCheckBox(tr("Failed only"))
        form.addRow("", self.ck_failed)
        lay.addLayout(form)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            [tr("When"), tr("Tool"), tr("Summary"), tr("Target"), tr("Result")])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(2, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._sync)
        lay.addWidget(self.table, 1)

        self.lbl_count = QLabel("")
        lay.addWidget(self.lbl_count)

        row = QHBoxLayout()
        self.btn_folder = QPushButton(tr("Open folder"))
        self.btn_folder.setAutoDefault(False)
        self.btn_folder.clicked.connect(self._open_folder)
        self.btn_undo = QPushButton(tr("Open in Backups && undo…"))
        self.btn_undo.setAutoDefault(False)
        self.btn_undo.clicked.connect(self._open_undo)
        self.btn_close = QPushButton(tr("Close"))
        self.btn_close.clicked.connect(self.reject)
        row.addWidget(self.btn_folder)
        row.addWidget(self.btn_undo)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        lay.addLayout(row)

        self.cb_tool.currentIndexChanged.connect(self.apply_filters)
        self.ed_target.textChanged.connect(self.apply_filters)
        self.d_from.dateChanged.connect(self.apply_filters)
        self.d_to.dateChanged.connect(self.apply_filters)
        self.ck_failed.toggled.connect(self.apply_filters)
        polish(self, primary=self.btn_close)
        self.apply_filters()

    @staticmethod
    def _date_edit():
        d = QDateEdit()
        d.setCalendarPopup(True)
        d.setDisplayFormat("yyyy-MM-dd")
        d.setMinimumDate(_ANY)
        d.setSpecialValueText(tr("Any"))
        d.setDate(_ANY)
        return d

    # -- filtering ---------------------------------------------------------
    def apply_filters(self, *_):
        def day(d):
            return "" if d.date() == _ANY else d.date().toString("yyyy-MM-dd")
        self._shown = filter_entries(
            self._all, tool=self.cb_tool.currentData() or "",
            target=self.ed_target.text(), since=day(self.d_from),
            until=day(self.d_to), failed_only=self.ck_failed.isChecked())
        self.table.setRowCount(len(self._shown))
        for r, e in enumerate(self._shown):
            tg = "; ".join(e.get("targets", []))
            vals = [e.get("time", ""), tr(str(e.get("tool", ""))),
                    e.get("summary", ""), tg,
                    tr("OK") if e.get("ok", True) else tr("Failed")]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c == 4 and not e.get("ok", True) and e.get("error"):
                    it.setToolTip(e["error"])
                if c == 3:
                    it.setToolTip(tg)
                self.table.setItem(r, c, it)
        for c in (0, 1, 4):
            self.table.resizeColumnToContents(c)
        self.table.setColumnWidth(3, 260)
        self.lbl_count.setText(tr("{n} of {total} entries").format(
            n=len(self._shown), total=len(self._all)))
        self._sync()

    def _selected(self):
        r = self.table.currentRow()
        if r < 0 or r >= len(self._shown) or not self.table.selectedItems():
            return None
        return self._shown[r]

    def _sync(self):
        e = self._selected()
        self.btn_folder.setEnabled(bool(e and e.get("targets")))
        self.btn_undo.setEnabled(bool(e and e.get("undo")))

    # -- actions -----------------------------------------------------------
    def _open_folder(self):
        e = self._selected()
        if not e or not e.get("targets"):
            return
        p = Path(e["targets"][0])
        folder = p if p.is_dir() else p.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _open_undo(self):
        e = self._selected()
        if not e or not e.get("undo"):
            return
        from taiko2x6.ps2 import backups_undo
        dlg = backups_undo.BackupsUndoDialog(self, default_path=undo_start_path(e))
        self._undo_dialog = dlg
        dlg.exec()

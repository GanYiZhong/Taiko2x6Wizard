#!/usr/bin/env python3
"""Check TJA...: table of issues per TJA file, with "Save fixed copy...".

The fixed copy is a NEW file (<name>.fixed.tja) written beside the original
after the user confirms what will change; the original is never touched.
"""
from __future__ import annotations

import pathlib

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from taiko2x6.app.dialog_polish import polish
from taiko2x6.app.i18n import tr
from taiko2x6.charts import tja_lint


class TjaCheckDialog(QDialog):
    def __init__(self, paths=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Check TJA"))
        self.resize(900, 520)
        v = QVBoxLayout(self)
        lab = QLabel(tr(
            "Looks for common TJA mistakes (COURSE / LEVEL, BALLOON counts, "
            "notes outside #START / #END, open rolls, branches, OFFSET, WAVE, "
            "encoding). Nothing is changed in your files."))
        lab.setWordWrap(True)
        v.addWidget(lab)
        row = QHBoxLayout()
        b = QPushButton(tr("Add TJA files…"))
        b.setAutoDefault(False)
        b.clicked.connect(self._add)
        row.addWidget(b)
        self.b_clear = QPushButton(tr("Clear"))
        self.b_clear.setAutoDefault(False)
        self.b_clear.clicked.connect(self.clear)
        row.addWidget(self.b_clear)
        self.lb_count = QLabel()
        row.addWidget(self.lb_count, 1)
        v.addLayout(row)
        self.tbl = QTableWidget(0, 6)
        self.tbl.setHorizontalHeaderLabels([
            tr("File"), tr("Line"), tr("Severity"), tr("Problem"),
            tr("Suggested fix"), tr("Auto-fix")])
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.verticalHeader().setVisible(False)
        hh = self.tbl.horizontalHeader()
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(3, QHeaderView.Interactive)
        self.tbl.setColumnWidth(0, 150)
        self.tbl.setColumnWidth(3, 330)
        self.tbl.setColumnWidth(4, 220)
        v.addWidget(self.tbl, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.b_save = QPushButton(tr("Save fixed copy…"))
        self.b_save.setAutoDefault(False)
        self.b_save.setToolTip(tr(
            "Write <name>.fixed.tja next to each TJA that has automatic "
            "fixes. Your original file is never overwritten."))
        self.b_save.clicked.connect(self.save_fixed)
        row.addWidget(self.b_save)
        close = QPushButton(tr("Close"))
        close.setDefault(True)
        close.clicked.connect(self.reject)
        row.addWidget(close)
        v.addLayout(row)
        self._results = {}          # path -> (text, encoding, issues)
        self._refresh()
        polish(self)
        self.add_paths(paths)

    # -- input ---------------------------------------------------------
    def _add(self):
        from taiko2x6.app import appconfig
        ps = appconfig.pick_open_many(
            self, "tja_check_in", tr("Add TJA files"),
            tr("TJA charts (*.tja);;All files (*)"))
        self.add_paths(ps)

    def clear(self):
        self._results.clear()
        self._refresh()

    def add_paths(self, paths):
        for p in paths:
            p = str(pathlib.Path(p))
            if p in self._results:
                continue
            try:
                text, raw, enc = tja_lint.read_tja(p)
                issues = tja_lint.lint_tja(text, base_dir=pathlib.Path(p).parent,
                                           raw=raw)
            except Exception as exc:
                text, enc = "", None
                issues = [tja_lint.Issue(
                    0, "unreadable", tja_lint.WARNING,
                    tr("Could not read the file: {err}").format(err=exc))]
            self._results[p] = (text, enc, issues)
        self._refresh()

    # -- table ---------------------------------------------------------
    def _refresh(self):
        rows = [(p, i) for p, (_, _, iss) in self._results.items() for i in iss]
        self.tbl.setRowCount(len(rows))
        for r, (p, i) in enumerate(rows):
            sev = tr("Warning") if i.severity == tja_lint.WARNING else tr("Info")
            vals = [pathlib.Path(p).name, str(i.line) if i.line else "",
                    sev, i.message, i.suggestion,
                    tr("Yes") if i.fixable else ""]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                it.setToolTip(p if c == 0 else
                              (i.fix_text if c == 5 and i.fix_text else val))
                if c == 1:
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.tbl.setItem(r, c, it)
        n_fix = sum(1 for _, _, iss in self._results.values()
                    if any(i.fixable for i in iss))
        self.lb_count.setText(
            tr("{f} file(s) checked, {n} issue(s)").format(
                f=len(self._results), n=len(rows)) if self._results else "")
        self.b_save.setEnabled(n_fix > 0)

    # -- fixes ---------------------------------------------------------
    def issues_of(self, path):
        return self._results[str(pathlib.Path(path))][2]

    def _confirm(self, body):
        return QMessageBox.question(
            self, tr("Save fixed copy"), body,
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel) == QMessageBox.Yes

    def save_fixed(self):
        """Confirm, then write <name>.fixed.tja for every file with fixes.
        Returns the list of files written."""
        plan = []
        for p, (text, enc, iss) in self._results.items():
            fix = [i for i in iss if i.fixable]
            if fix:
                plan.append((p, text, enc, fix))
        if not plan:
            return []
        body = [tr("These copies will be created (your originals stay "
                   "untouched):"), ""]
        for p, _, _, fix in plan:
            body.append("%s  ->  %s" % (pathlib.Path(p).name,
                                        tja_lint.fixed_copy_path(p).name))
            for i in fix:
                body.append("   • " + (tr("line {n}: ").format(n=i.line)
                                       if i.line else "") + i.fix_text)
        if not self._confirm("\n".join(body)):
            return []
        written = []
        for p, text, enc, fix in plan:
            new, _ = tja_lint.apply_fixes(text, fix)
            written.append(str(tja_lint.write_fixed_copy(p, new, enc)))
        QMessageBox.information(
            self, tr("Save fixed copy"),
            tr("Saved {n} fixed copy file(s):").format(n=len(written)) + "\n"
            + "\n".join(written))
        return written

#!/usr/bin/env python3
"""
SWG visual editor dialog (PySide6).

Edits a Taiko ".swg" scene/layout file:
  * object name + screen resolution
  * transform matrices (element position X/Y and scale X/Y)
  * symbol / element names (in-place, within their byte slot)
  * a visual canvas showing the screen frame, the group's textures, and a
    marker for every transform translation
Saving produces byte-exact SWG bytes (only changed fields differ) via swg.py.

The canvas is a best-effort visual aid: it draws the group's .nut textures
centered in the screen frame and overlays transform markers. Exact node->texture
binding is not fully reverse-engineered, so this is context, not a perfect
game-accurate render.
"""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QPen, QBrush, QFont
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QSpinBox, QTableWidget, QTableWidgetItem, QPushButton, QLabel, QTabWidget,
    QGroupBox, QMessageBox, QScrollArea, QDoubleSpinBox, QSplitter, QHeaderView,
)

import swg as S


class SwgCanvas(QWidget):
    """Draws the screen frame, group textures (centered) and transform markers."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(360, 280)
        self.res = (640, 480)
        self.textures: list[QPixmap] = []
        self.markers: list[tuple[float, float, str]] = []
        self.show_textures = True

    def set_scene(self, res, textures, markers):
        self.res = res if res[0] and res[1] else (640, 480)
        self.textures = textures
        self.markers = markers
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(40, 40, 46))
        rw, rh = self.res
        if rw <= 0 or rh <= 0:
            return
        # fit the screen rect into the widget with margin
        m = 16
        avail_w, avail_h = self.width() - 2 * m, self.height() - 2 * m
        scale = min(avail_w / rw, avail_h / rh)
        fw, fh = rw * scale, rh * scale
        ox = (self.width() - fw) / 2
        oy = (self.height() - fh) / 2
        frame = QRectF(ox, oy, fw, fh)

        # checkerboard inside frame (transparency context)
        cb = 8
        p.save()
        p.setClipRect(frame)
        for yy in range(int(fh // cb) + 1):
            for xx in range(int(fw // cb) + 1):
                if (xx + yy) & 1:
                    p.fillRect(QRectF(ox + xx * cb, oy + yy * cb, cb, cb),
                               QColor(55, 55, 62))
        # textures centered (best-effort visual)
        if self.show_textures:
            for pix in self.textures:
                if pix.isNull():
                    continue
                dw, dh = pix.width() * scale, pix.height() * scale
                p.drawPixmap(QRectF(ox + (fw - dw) / 2, oy + (fh - dh) / 2, dw, dh),
                             pix, QRectF(pix.rect()))
        p.restore()

        # frame border
        p.setPen(QPen(QColor(120, 200, 255), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(frame)
        # center cross
        p.setPen(QPen(QColor(90, 90, 100), 1, Qt.DashLine))
        p.drawLine(QPointF(ox + fw / 2, oy), QPointF(ox + fw / 2, oy + fh))
        p.drawLine(QPointF(ox, oy + fh / 2), QPointF(ox + fw, oy + fh / 2))

        # transform markers — translation is relative to screen center
        cx, cy = ox + fw / 2, oy + fh / 2
        p.setFont(QFont("Consolas", 7))
        for (tx, ty, label) in self.markers:
            mx, my = cx + tx * scale, cy + ty * scale
            p.setPen(QPen(QColor(255, 180, 60), 1))
            p.setBrush(QBrush(QColor(255, 180, 60, 120)))
            p.drawEllipse(QPointF(mx, my), 4, 4)
            p.setPen(QPen(QColor(255, 210, 140), 1))
            p.drawText(QPointF(mx + 6, my - 4), label)
        p.end()


class SwgEditor(QDialog):
    """Modal editor. After exec(), .result_bytes holds new SWG bytes if saved."""

    def __init__(self, swg_bytes: bytes, textures: list, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"SWG Editor — {title}")
        self.resize(1080, 680)
        self.swg = S.SwgFile.parse(swg_bytes)
        self.result_bytes: bytes | None = None
        # Defensive unpack: tolerate a malformed texture list rather than
        # raising inside __init__ (which the GUI relies on constructing).
        self._tex_pixmaps = []
        for item in textures:
            try:
                _name, pix = item
            except (TypeError, ValueError):
                continue
            self._tex_pixmaps.append(pix)

        self._build_ui(textures)
        self._refresh_canvas()

    # -- ui -------------------------------------------------------------------
    def _build_ui(self, textures):
        split = QSplitter(self)

        # ---- left: editable fields ----
        left = QWidget()
        ll = QVBoxLayout(left)

        gen = QGroupBox("General")
        gf = QFormLayout(gen)
        self.ed_name = QLineEdit(self.swg.name)
        self.sp_w = QSpinBox(); self.sp_w.setRange(1, 4096); self.sp_w.setValue(self.swg.width)
        self.sp_h = QSpinBox(); self.sp_h.setRange(1, 4096); self.sp_h.setValue(self.swg.height)
        self.sp_w.valueChanged.connect(self._refresh_canvas)
        self.sp_h.valueChanged.connect(self._refresh_canvas)
        res = QWidget(); rl = QHBoxLayout(res); rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.sp_w); rl.addWidget(QLabel("×")); rl.addWidget(self.sp_h)
        gf.addRow("name:", self.ed_name)
        gf.addRow("resolution:", res)
        gf.addRow(QLabel(f"format const: 0x{self.swg.format_const:08X}"))
        ll.addWidget(gen)

        tabs = QTabWidget()
        # matrices
        self.tbl_mat = QTableWidget(len(self.swg.matrices), 6)
        self.tbl_mat.setHorizontalHeaderLabels(
            ["offset", "pos X", "pos Y", "scale X", "scale Y", "status"])
        self.tbl_mat.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for r, m in enumerate(self.swg.matrices):
            off = QTableWidgetItem(f"0x{m.offset:X}")
            off.setFlags(off.flags() & ~Qt.ItemIsEditable)
            self.tbl_mat.setItem(r, 0, off)
            verified = getattr(m, "verified", False)
            for c, val in enumerate((m.tx, m.ty, m.sx, m.sy), start=1):
                cell = QTableWidgetItem(f"{val:g}")
                if not verified:
                    # heuristic (unverified) matches are display-only: editing a
                    # false positive would corrupt non-matrix data.
                    cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                    cell.setForeground(QColor(150, 150, 150))
                self.tbl_mat.setItem(r, c, cell)
            status = QTableWidgetItem("ok" if verified else "unverified (read-only)")
            status.setFlags(status.flags() & ~Qt.ItemIsEditable)
            self.tbl_mat.setItem(r, 5, status)
        self.tbl_mat.itemChanged.connect(self._refresh_canvas)
        tabs.addTab(self.tbl_mat, f"Transforms ({len(self.swg.matrices)})")

        # strings / symbols
        self.tbl_str = QTableWidget(len(self.swg.strings), 3)
        self.tbl_str.setHorizontalHeaderLabels(["offset", "slot", "text (editable)"])
        self.tbl_str.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for r, s in enumerate(self.swg.strings):
            o = QTableWidgetItem(f"0x{s.offset:X}"); o.setFlags(o.flags() & ~Qt.ItemIsEditable)
            cap = QTableWidgetItem(str(s.capacity)); cap.setFlags(cap.flags() & ~Qt.ItemIsEditable)
            self.tbl_str.setItem(r, 0, o)
            self.tbl_str.setItem(r, 1, cap)
            self.tbl_str.setItem(r, 2, QTableWidgetItem(s.text))
        tabs.addTab(self.tbl_str, f"Symbols ({len(self.swg.strings)})")
        ll.addWidget(tabs, 1)

        # buttons
        btns = QHBoxLayout()
        self.chk_tex = QPushButton("Toggle textures"); self.chk_tex.setCheckable(True)
        self.chk_tex.setChecked(True); self.chk_tex.toggled.connect(self._toggle_tex)
        b_save = QPushButton("Save to archive"); b_save.clicked.connect(self._save)
        b_cancel = QPushButton("Cancel"); b_cancel.clicked.connect(self.reject)
        btns.addWidget(self.chk_tex); btns.addStretch(1)
        btns.addWidget(b_save); btns.addWidget(b_cancel)
        ll.addLayout(btns)

        # ---- right: canvas + texture strip ----
        right = QWidget()
        rl2 = QVBoxLayout(right)
        self.canvas = SwgCanvas()
        rl2.addWidget(self.canvas, 1)
        strip = QScrollArea(); strip.setFixedHeight(96); strip.setWidgetResizable(True)
        sw = QWidget(); sl = QHBoxLayout(sw); sl.setContentsMargins(4, 4, 4, 4)
        for item in textures[:64]:
            try:
                name, pix = item
            except (TypeError, ValueError):
                continue
            lab = QLabel()
            lab.setPixmap(pix.scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            lab.setToolTip(f"{name} ({pix.width()}x{pix.height()})")
            sl.addWidget(lab)
        sl.addStretch(1)
        strip.setWidget(sw)
        rl2.addWidget(QLabel(f"group textures ({len(textures)}):"))
        rl2.addWidget(strip)

        split.addWidget(left); split.addWidget(right)
        split.setStretchFactor(0, 3); split.setStretchFactor(1, 4)
        lay = QVBoxLayout(self); lay.addWidget(split)

    # -- canvas ---------------------------------------------------------------
    def _toggle_tex(self, on):
        self.canvas.show_textures = on
        self.canvas.update()

    def _refresh_canvas(self, *_):
        markers = []
        for r in range(self.tbl_mat.rowCount()):
            try:
                tx = float(self.tbl_mat.item(r, 1).text())
                ty = float(self.tbl_mat.item(r, 2).text())
            except (ValueError, AttributeError):
                continue
            markers.append((tx, ty, f"#{r}"))
        self.canvas.set_scene((self.sp_w.value(), self.sp_h.value()),
                              self._tex_pixmaps, markers)

    # -- save -----------------------------------------------------------------
    @staticmethod
    def _parse_cell(item):
        """Parse a table cell to a finite float.

        Returns (value, None) on success or (None, error_message) on failure
        (blank/non-numeric/NaN/inf). Never raises.
        """
        if item is None:
            return None, "empty cell"
        text = item.text().strip()
        if text == "":
            return None, "blank value"
        try:
            v = float(text)
        except ValueError:
            return None, f"not a number: {text!r}"
        if not math.isfinite(v):
            return None, f"non-finite value: {text!r}"
        return v, None

    def _save(self):
        errors = []   # list of (description, message); the offending field only

        # name (string field — its own try/except so one bad field is isolated)
        if self.ed_name.text() != self.swg.name:
            try:
                self.swg.set_name(self.ed_name.text())
            except Exception as exc:
                errors.append(("name", str(exc)))

        # resolution
        if (self.sp_w.value(), self.sp_h.value()) != (self.swg.width, self.swg.height):
            try:
                self.swg.set_resolution(self.sp_w.value(), self.sp_h.value())
            except Exception as exc:
                errors.append(("resolution", str(exc)))

        # matrices — validate each cell individually; skip unverified rows so a
        # heuristic false positive can never corrupt non-matrix data.
        for r, m in enumerate(self.swg.matrices):
            if not getattr(m, "verified", False):
                continue
            row_vals = {}
            row_ok = True
            for c, key in ((1, "pos X"), (2, "pos Y"), (3, "scale X"), (4, "scale Y")):
                v, err = self._parse_cell(self.tbl_mat.item(r, c))
                if err is not None:
                    errors.append((f"matrix #{r} {key}", err))
                    row_ok = False
                else:
                    row_vals[c] = v
            if not row_ok:
                continue
            tx, ty, sx, sy = row_vals[1], row_vals[2], row_vals[3], row_vals[4]
            try:
                if (tx, ty) != (m.tx, m.ty):
                    self.swg.set_matrix_translation(m.offset, tx, ty)
                if (sx, sy) != (m.sx, m.sy):
                    self.swg.set_matrix_scale(m.offset, sx, sy)
            except Exception as exc:
                errors.append((f"matrix #{r}", str(exc)))

        # strings — each edit isolated; length-changing/non-ASCII edits are
        # refused by set_string and reported per field.
        for r, s in enumerate(list(self.swg.strings)):
            item = self.tbl_str.item(r, 2)
            if item is None:
                continue
            new = item.text()
            if new != s.text:
                try:
                    self.swg.set_string(s.offset, new)
                except Exception as exc:
                    errors.append((f"string @0x{s.offset:X}", str(exc)))

        if errors:
            lines = "\n".join(f"  • {desc}: {msg}" for desc, msg in errors)
            QMessageBox.critical(
                self, "Edit error",
                "Some fields were not saved (other valid edits were applied "
                "to the in-memory copy but NOT written):\n\n" + lines)
            return

        self.result_bytes = self.swg.repack()
        self.accept()

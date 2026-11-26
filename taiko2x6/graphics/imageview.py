"""Zoomable image viewer with a transparency checkerboard (read-only previews).

Used by the explorer's Image tab and the flipbook player. Modes: fit to window,
1:1 pixels, or a manual zoom (mouse wheel, +/- buttons). Drag to pan when the
image is larger than the view.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QPainter, QColor, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
)

from taiko2x6.app.i18n import tr

ZOOM_MIN, ZOOM_MAX = 0.05, 32.0


def _paint_checker(p: QPainter, rect: QRectF, cell: int = 8):
    """Light/dark grey checkerboard so transparent pixels are visible."""
    a, b = QColor(204, 204, 204), QColor(150, 150, 150)
    p.save()
    p.setClipRect(rect)
    p.fillRect(rect, a)
    x0, y0 = int(rect.left()), int(rect.top())
    nx = int(rect.width() // cell) + 2
    ny = int(rect.height() // cell) + 2
    for j in range(ny):
        for i in range(nx):
            if (i + j) & 1:
                p.fillRect(x0 + i * cell, y0 + j * cell, cell, cell, b)
    p.restore()


class _Canvas(QWidget):
    def __init__(self, owner: "ImageViewer"):
        super().__init__()
        self._o = owner
        self.setMinimumSize(160, 120)
        self._drag: QPointF | None = None

    def paintEvent(self, _):
        o = self._o
        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().window())
        pix = o._pix
        if pix is None or pix.isNull() or pix.width() <= 0 or pix.height() <= 0:
            p.setPen(self.palette().color(self.foregroundRole()))
            p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, o._message)
            p.end()
            return
        z = o.zoom()
        dw, dh = pix.width() * z, pix.height() * z
        x = (self.width() - dw) / 2 + o._pan.x()
        y = (self.height() - dh) / 2 + o._pan.y()
        target = QRectF(x, y, dw, dh)
        _paint_checker(p, target.intersected(QRectF(self.rect())))
        # Crisp pixels when magnified, smooth when shrunk.
        p.setRenderHint(QPainter.SmoothPixmapTransform, z < 1.0)
        p.drawPixmap(target, pix, QRectF(pix.rect()))
        p.end()

    def wheelEvent(self, ev):
        dy = ev.angleDelta().y()
        if dy:
            self._o.zoom_by(1.25 if dy > 0 else 0.8)
        ev.accept()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag = ev.position()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, ev):
        if self._drag is not None:
            d = ev.position() - self._drag
            self._drag = ev.position()
            self._o._pan += d
            self.update()

    def mouseReleaseEvent(self, ev):
        self._drag = None
        self.unsetCursor()

    def mouseDoubleClickEvent(self, ev):
        self._o.fit()

    def resizeEvent(self, ev):
        self._o._refresh_label()
        super().resizeEvent(ev)


class ImageViewer(QWidget):
    def __init__(self, parent=None, show_info: bool = True):
        super().__init__(parent)
        self._pix: QPixmap | None = None
        self._message = tr("(no image)")
        self._fit = True
        self._zoom = 1.0
        self._pan = QPointF(0, 0)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.b_fit = QPushButton(tr("Fit"))
        self.b_fit.setToolTip(tr("Fit the image to the window (double-click)"))
        self.b_one = QPushButton("1:1")
        self.b_one.setToolTip(tr("Show actual pixels"))
        self.b_out = QPushButton("-")
        self.b_in = QPushButton("+")
        self.b_out.setToolTip(tr("Zoom out (mouse wheel)"))
        self.b_in.setToolTip(tr("Zoom in (mouse wheel)"))
        for b in (self.b_fit, self.b_one, self.b_out, self.b_in):
            b.setFixedWidth(44)
            bar.addWidget(b)
        self.b_fit.clicked.connect(self.fit)
        self.b_one.clicked.connect(self.actual_size)
        self.b_out.clicked.connect(lambda: self.zoom_by(0.8))
        self.b_in.clicked.connect(lambda: self.zoom_by(1.25))
        self.lbl_zoom = QLabel("")
        bar.addWidget(self.lbl_zoom)
        bar.addStretch(1)
        self.lbl_info = QLabel("")
        self.lbl_info.setVisible(show_info)
        bar.addWidget(self.lbl_info)
        lay.addLayout(bar)
        self.canvas = _Canvas(self)
        lay.addWidget(self.canvas, 1)
        self._set_enabled(False)

    # -- content ---------------------------------------------------------------
    def set_pixmap(self, pix: QPixmap | None, info: str = ""):
        self._pix = pix
        self.lbl_info.setText(info)
        self._set_enabled(pix is not None and not pix.isNull())
        self._refresh_label()
        self.canvas.update()

    def set_message(self, text: str):
        """Show text instead of an image (no image / decode failed)."""
        self._message = text
        self.set_pixmap(None)

    def has_image(self) -> bool:
        return self._pix is not None and not self._pix.isNull()

    def _set_enabled(self, on: bool):
        for b in (self.b_fit, self.b_one, self.b_out, self.b_in):
            b.setEnabled(on)

    # -- zoom ------------------------------------------------------------------
    def _fit_zoom(self) -> float:
        if not self.has_image() or self._pix.width() <= 0 or self._pix.height() <= 0:
            return 1.0
        c = self.canvas
        z = min(c.width() / self._pix.width(), c.height() / self._pix.height())
        return max(ZOOM_MIN, min(z, ZOOM_MAX))

    def zoom(self) -> float:
        return self._fit_zoom() if self._fit else self._zoom

    def fit(self):
        self._fit = True
        self._pan = QPointF(0, 0)
        self._refresh_label()
        self.canvas.update()

    def actual_size(self):
        self._pan = QPointF(0, 0)
        self.set_zoom(1.0)

    def set_zoom(self, z: float):
        self._fit = False
        self._zoom = max(ZOOM_MIN, min(z, ZOOM_MAX))
        self._refresh_label()
        self.canvas.update()

    def zoom_by(self, factor: float):
        if self.has_image():
            self.set_zoom(self.zoom() * factor)

    def _refresh_label(self):
        if not self.has_image():
            self.lbl_zoom.setText("")
            return
        mode = tr("fit") if self._fit else ""
        self.lbl_zoom.setText(f"{self.zoom() * 100:.0f}% {mode}".strip())

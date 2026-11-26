"""Monochrome SVG icon set, tinted with the palette's text colour.

Each ``<name>.svg`` is drawn in black on a 24x24 grid; ``icon(name)`` renders
it and recolours it so it reads in both the light and the dark theme.  Icons
are built from the *current* application palette, so callers re-request them
after a theme change (the main window does this on ApplicationPaletteChange).
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

ICON_DIR = Path(__file__).resolve().parent
SIZES = (16, 20, 24, 32, 48)


def names() -> list[str]:
    return sorted(p.stem for p in ICON_DIR.glob("*.svg"))


def path(name: str) -> Path:
    return ICON_DIR / f"{name}.svg"


def render(name: str, size: int, color: QColor, dpr: float = 1.0) -> QPixmap:
    """Render ``name`` at ``size`` logical px, filled with ``color``."""
    px_size = max(1, round(size * dpr))
    pm = QPixmap(px_size, px_size)
    pm.fill(Qt.transparent)
    renderer = QSvgRenderer(QByteArray(path(name).read_bytes()))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    renderer.render(p, QRectF(0, 0, px_size, px_size))
    p.setCompositionMode(QPainter.CompositionMode_SourceIn)
    p.fillRect(pm.rect(), color)
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def icon(name: str, palette: QPalette | None = None,
         color: QColor | None = None) -> QIcon:
    """A QIcon for ``name`` in the palette's text (and disabled) colours,
    or in a fixed ``color`` (app icon)."""
    if palette is None:
        app = QApplication.instance()
        palette = app.palette() if app is not None else QPalette()
    normal = palette.color(QPalette.Active, QPalette.WindowText)
    disabled = palette.color(QPalette.Disabled, QPalette.WindowText)
    if color is not None:
        normal = disabled = color
    ic = QIcon()
    for s in SIZES:
        ic.addPixmap(render(name, s, normal), QIcon.Normal)
        ic.addPixmap(render(name, s, normal), QIcon.Selected)
        ic.addPixmap(render(name, s, disabled), QIcon.Disabled)
    return ic

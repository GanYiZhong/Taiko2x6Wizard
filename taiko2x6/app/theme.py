"""App-wide look: Fusion style + light/dark palette + a modest stylesheet.

Dialogs should not hard-code greys; use ``color:palette(mid)`` for muted hint
text and ``theme.mono_font()`` for log/hex panes. Mode is "system" (follow
Windows), "light" or "dark", persisted under [ui] theme in config.ini.
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

from taiko2x6.app import appconfig

MODES = ("system", "light", "dark")

_QSS = """
QGroupBox { margin-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 3px; }
QToolTip { padding: 3px; }
QPushButton { padding: 4px 12px; }
"""


def configure_high_dpi() -> None:
    """Call before QApplication is created."""
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass


def saved_mode() -> str:
    m = appconfig.get("ui", "theme", "system")
    return m if m in MODES else "system"


def save_mode(mode: str) -> None:
    if mode in MODES:
        appconfig.set_value("ui", "theme", mode)


def system_is_dark(app: QApplication) -> bool:
    try:                                           # Qt 6.5+
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:
        pal = app.palette()
        return pal.color(QPalette.Window).lightness() < 128


def make_palette(dark: bool) -> QPalette:
    c = QColor
    p = QPalette()
    if dark:
        win, base, text, btn = c("#2b2b2b"), c("#1e1e1e"), c("#e6e6e6"), c("#3a3a3a")
        mid, hl = c("#9a9a9a"), c("#3d7fd6")
        dis = c("#7a7a7a")
    else:
        win, base, text, btn = c("#f0f0f0"), c("#ffffff"), c("#1a1a1a"), c("#e6e6e6")
        mid, hl = c("#7a7a7a"), c("#2f6fc4")
        dis = c("#a0a0a0")
    for role, col in ((QPalette.Window, win), (QPalette.Base, base),
                      (QPalette.AlternateBase, win), (QPalette.WindowText, text),
                      (QPalette.Text, text), (QPalette.ButtonText, text),
                      (QPalette.Button, btn), (QPalette.ToolTipBase, base),
                      (QPalette.ToolTipText, text), (QPalette.Mid, mid),
                      (QPalette.PlaceholderText, mid), (QPalette.Highlight, hl),
                      (QPalette.HighlightedText, c("#ffffff")),
                      (QPalette.Link, c("#6aa9ff") if dark else c("#1a5fb4")),
                      (QPalette.BrightText, c("#ff5555"))):
        p.setColor(role, col)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, dis)
    return p


def mono_font(point_size: int = 9) -> QFont:
    f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    f.setPointSize(point_size)
    return f


def apply(app: QApplication, mode: str | None = None) -> bool:
    """Apply the theme; returns True if the dark variant is active."""
    mode = mode or saved_mode()
    # Setting a style again repolishes every live widget; doing that while a
    # window is being torn down crashed (seen in the test run), and the
    # Settings > Theme switch only needs the palette. Set Fusion once.
    style = app.style()
    if style is None or style.name().lower() != "fusion":
        app.setStyle("Fusion")
    dark = system_is_dark(app) if mode == "system" else mode == "dark"
    if mode != "system":
        try:
            app.styleHints().setColorScheme(
                Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light)
        except AttributeError:
            pass
    app.setPalette(make_palette(dark))
    if app.styleSheet() != _QSS:
        app.setStyleSheet(_QSS)
    return dark

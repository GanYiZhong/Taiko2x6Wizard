"""Dialogs remember size/position; off-screen positions are pulled back."""
from __future__ import annotations

from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QSplitter, QLabel

from taiko2x6.app import appconfig, dialog_polish

_app = QApplication.instance() or QApplication([])


class _Dlg(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        self.sp = QSplitter()
        self.sp.addWidget(QLabel("a"))
        self.sp.addWidget(QLabel("b"))
        lay.addWidget(self.sp)
        dialog_polish.polish(self)


def _cycle(d):
    d.show()
    QApplication.processEvents()
    d.hide()
    QApplication.processEvents()


def test_roundtrip_size_and_position():
    d = _Dlg()
    d.show()
    QApplication.processEvents()
    d.resize(517, 333)
    d.move(40, 50)
    d.sp.setSizes([300, 100])
    d.hide()
    d2 = _Dlg()
    d2.show()
    QApplication.processEvents()
    assert (d2.width(), d2.height()) == (517, 333)
    assert d2.frameGeometry().topLeft().x() == d.frameGeometry().topLeft().x()
    assert d2.sp.sizes()[0] > d2.sp.sizes()[1]
    d2.hide()


def test_offscreen_pulled_back():
    appconfig.set_value(dialog_polish.GEOM_SECTION, dialog_polish._geom_key(_Dlg()),
                        "99999,99999,5000,4000")
    d = _Dlg()
    d.show()
    QApplication.processEvents()
    scr = _app.primaryScreen().availableGeometry()
    assert scr.contains(d.frameGeometry().topLeft())
    assert d.width() <= scr.width() and d.height() <= scr.height()
    d.hide()


def test_fit_rect_multi_monitor():
    screens = [(0, 0, 1920, 1080), (1920, 0, 1280, 1024)]
    assert dialog_polish.fit_rect(3000, 900, 800, 600, screens) == (2400, 424, 800, 600)
    assert dialog_polish.fit_rect(-5000, -5000, 5000, 600, screens)[:3] == (0, 0, 1920)
    assert dialog_polish.fit_rect(0, 0, 100, 100, screens, 300, 200)[2:] == (300, 200)


def test_reset_clears():
    d = _Dlg()
    _cycle(d)
    assert appconfig.get(dialog_polish.GEOM_SECTION, dialog_polish._geom_key(d))
    dialog_polish.reset_layouts()
    assert appconfig.get(dialog_polish.GEOM_SECTION, dialog_polish._geom_key(d)) == ""

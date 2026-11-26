"""Dialog behaviour shared by every dialog: tab order, accelerators, buddies,
Esc, and no hard-coded colours. Companion to test_dialog_consistency.py."""
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView,  # noqa: E402
                               QAbstractSpinBox, QApplication, QComboBox,
                               QFormLayout, QLabel)

from test_dialog_consistency import NAME_LIST, _build, _close, _env  # noqa: E402,F401


def _ancestors(w):
    p = w.parentWidget()
    while p is not None:
        yield p
        p = p.parentWidget()


def _tab_stops(d):
    """Widgets Tab visits, in order, that belong to the dialog proper."""
    out, w = [], d
    for _ in range(2000):
        w = w.nextInFocusChain()
        if w is d or w is None:
            break
        if not (w.focusPolicy() & Qt.TabFocus) or not w.isVisibleTo(d) or not w.isEnabledTo(d):
            continue
        if isinstance(w.parentWidget(), (QAbstractSpinBox, QComboBox)) \
                or w.objectName().startswith("qt_"):
            continue
        if any(isinstance(a, QAbstractItemView) for a in _ancestors(w)):
            continue
        out.append(w)
    return out


@pytest.mark.parametrize("name", NAME_LIST)
def test_tab_order_follows_layout(name):
    """Tab never jumps back up a column: each stop is not above the previous
    one in the same column (moving on to the next column is fine)."""
    d = _build(name)
    try:
        d.show()
        QApplication.processEvents()
        stops = _tab_stops(d)
        bad = []
        for a, b in zip(stops, stops[1:]):
            ra = a.rect().translated(a.mapTo(d, a.rect().topLeft()))
            rb = b.rect().translated(b.mapTo(d, b.rect().topLeft()))
            overlap = min(ra.right(), rb.right()) - max(ra.left(), rb.left())
            same_col = overlap > min(ra.width(), rb.width()) / 2
            if same_col and rb.center().y() < ra.top() - 2:
                bad.append((type(a).__name__, type(b).__name__))
        assert not bad, bad
    finally:
        _close(d)


@pytest.mark.parametrize("lang", ["en", "zh-TW"])
@pytest.mark.parametrize("name", NAME_LIST)
def test_accelerators_do_not_clash(name, lang):
    d = _build(name, lang)
    try:
        seen = {}
        for w in d.findChildren(QAbstractButton) + d.findChildren(QLabel):
            for m in re.findall(r"(?<!&)&(?!&)(\w)", w.text()):
                seen.setdefault(m.lower(), []).append(w.text())
        clashes = {k: v for k, v in seen.items() if len(v) > 1}
        assert not clashes, clashes
    finally:
        _close(d)


@pytest.mark.parametrize("name", NAME_LIST)
def test_form_labels_have_buddies(name):
    d = _build(name)
    try:
        bad = []
        for lay in d.findChildren(QFormLayout):
            for r in range(lay.rowCount()):
                lab = lay.itemAt(r, QFormLayout.LabelRole)
                fld = lay.itemAt(r, QFormLayout.FieldRole)
                if lab and lab.widget() and fld and fld.widget() is not None \
                        and isinstance(lab.widget(), QLabel) and lab.widget().text() \
                        and lab.widget().buddy() is None:
                    bad.append(lab.widget().text())
        assert not bad, bad
    finally:
        _close(d)


@pytest.mark.parametrize("name", NAME_LIST)
def test_escape_closes_idle_dialog(name):
    from PySide6.QtTest import QTest
    d = _build(name)
    d.show()
    QApplication.processEvents()
    QTest.keyClick(d, Qt.Key_Escape)
    QApplication.processEvents()
    try:
        assert not d.isVisible(), "Esc did not close the idle dialog"
    finally:
        _close(d)


def test_no_hard_coded_colours_in_dialog_sources():
    """Colours come from the palette (theme.py) so light and dark both read
    well. The table highlight brushes in table_tools are semi-transparent
    overlays, and the main window's tree is not a dialog; neither is checked."""
    root = Path(__file__).resolve().parents[1] / "taiko2x6"
    skip = {"theme.py", "table_tools.py", "taiko256_explorer_gui6.py", "i18n.py"}
    pat = re.compile(r"(setStyleSheet|_style\s*=|_STYLE\s*=|color:|QColor\()[^\n]*#[0-9a-fA-F]{3,6}\b"
                     r"|QColor\([\"']#")
    hits = []
    for p in root.rglob("*.py"):
        if p.name in skip or "graphics" in p.parts:
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                hits.append("%s:%d" % (p.name, n))
    assert not hits, hits

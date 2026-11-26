"""One finishing pass every dialog runs at the end of its constructor, so they
all behave alike:

* the primary action is the default button (Enter), unless the dialog states a
  reason it has none (``NO_DEFAULT_REASON``: table editors, where Enter belongs
  to the table / filter box);
* a label that sits next to an input becomes that input's buddy;
* buttons, check boxes and labelled inputs get an Alt+letter accelerator that
  does not clash with another one in the same dialog.

Accelerators are generated from the already translated text, so there is no
per-string table to keep in step: an English "Save" becomes "&Save", and a
Chinese "儲存" becomes "儲存(&S)" (the letter comes from the English source).
Esc needs nothing here: QDialog already maps it to reject(), and the dialogs
that run a worker override reject() to refuse while it is busy.
"""
from __future__ import annotations

import base64
import re

from PySide6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QCheckBox,
                               QComboBox, QLabel, QRadioButton, QLayout, QLineEdit,
                               QPushButton, QWidget)

from taiko2x6.app import i18n

_FIELDS = (QLineEdit, QComboBox, QAbstractSpinBox)
_AMP = re.compile(r"(?<!&)&(?!&)(\w)")
_SKIP = {"browse"}                      # one per path row: no accelerator


def polish(dlg, primary: QPushButton | None = None) -> None:
    if primary is not None:
        primary.setAutoDefault(True)
        primary.setDefault(True)
    for lay in dlg.findChildren(QLayout):          # group boxes own layouts too
        _buddies(lay)
    _mnemonics(dlg)
    _filter_bars_first(dlg)
    _wrap_choices(dlg)
    _wrap_labels(dlg)
    _remember_geometry(dlg)


# ------------------------------------------------------------------ buddies
def _field_in(item) -> QWidget | None:
    if item is None:
        return None
    w = item.widget()
    if w is not None:
        if isinstance(w, _FIELDS):
            return w
        edit = getattr(w, "edit", None)           # dialog_kit.PathRow
        return edit if isinstance(edit, QLineEdit) else None
    lay = item.layout()
    if lay is not None:
        for i in range(lay.count()):
            sub = lay.itemAt(i)
            if sub.widget() is not None and isinstance(sub.widget(), QAbstractButton):
                continue
            f = _field_in(sub)
            if f is not None:
                return f
    return None


def _buddies(lay: QLayout) -> None:
    n = lay.count()
    for i in range(n):
        item = lay.itemAt(i)
        w = item.widget()
        if isinstance(w, QLabel) and w.buddy() is None and not w.wordWrap() \
                and w.text().rstrip().endswith((":", "：")) and "<" not in w.text():
            f = _field_in(lay.itemAt(i + 1)) if i + 1 < n else None
            if f is not None:
                w.setBuddy(f)


# ---------------------------------------------------------------- mnemonics
def _used(widgets) -> set[str]:
    out = set()
    for w in widgets:
        out.update(m.lower() for m in _AMP.findall(w.text()))
    return out


def _english(text: str) -> str:
    plain = text.rstrip(":：…. ")
    for en, zh in i18n.ZH_TW.items():
        if zh.rstrip(":：…. ") == plain:
            return en
    return ""


def _candidates(src: str):
    """Letters of `src` in preference order: its first letter, then word
    initials, then every other letter."""
    clean = src.replace("&&", "\0")
    letters = [(i, c) for i, c in enumerate(clean) if c.isascii() and c.isalnum()]
    starts = [(i, c) for i, c in letters if i == 0 or not clean[i - 1].isalnum()]
    seen = []
    for lst in (letters[:1], starts, letters):
        for _i, c in lst:
            if c.lower() not in seen:
                seen.append(c.lower())
                yield c.lower()


def _insert(text: str, letter: str) -> str:
    """Put '&' before the first `letter` of `text` (ignoring '&&')."""
    for m in re.finditer(r"(?<!&)" + re.escape(letter), text, re.I):
        return text[:m.start()] + "&" + text[m.start():]
    return ""


def _mnemonics(dlg) -> None:
    targets = []
    for w in dlg.findChildren(QAbstractButton):
        if w.text() and not w.text().startswith("&"):
            targets.append(w)
    for w in dlg.findChildren(QLabel):
        if w.buddy() is not None and w.text() and "<" not in w.text():
            targets.append(w)
    everything = dlg.findChildren(QAbstractButton) + dlg.findChildren(QLabel)
    used = _used(everything)
    todo = []
    for w in targets:
        if _AMP.search(w.text()):
            continue
        t = w.text().rstrip(":：… ").lower()
        if t in _SKIP:
            continue
        src = w.text() if any(c.isascii() and c.isalpha() for c in w.text()) \
            else _english(w.text())
        if sum(c.isalpha() for c in src) < 2:
            continue
        todo.append((w, src))
    # pass 0 gives every widget its own first letter before any widget takes
    # a later one, so "Save" and "Cancel" keep S and C.
    pending = todo
    for rank in range(2):
        rest = []
        for w, src in pending:
            cands = list(_candidates(src))
            if rank == 0:
                pick = cands[0] if cands and cands[0] not in used else None
            else:
                pick = next((c for c in cands if c not in used), None)
            if pick is None:
                rest.append((w, src))
                continue
            used.add(pick)
            _apply(w, pick)
        pending = rest


def _apply(w, letter: str) -> None:
    text = w.text()
    if any(c.isascii() and c.isalpha() for c in text):
        new = _insert(text, letter)
        if new:
            w.setText(new)
        return
    # translated text without Latin letters: "名稱(&N):" like the menus
    tail = ""
    m = re.search(r"([:：…. ]+)$", text)
    if m:
        text, tail = text[:m.start()], m.group(1)
    w.setText("%s(&%s)%s" % (text, letter.upper(), tail))


# ----------------------------------------------------------------- wrapping
def _wrap_choices(dlg, ems: int = 40) -> None:
    """Check boxes and radio buttons cannot word-wrap, so a long sentence made
    the whole dialog wider than the screen. Break it into lines instead (at
    spaces, or between CJK characters)."""
    for w in dlg.findChildren(QAbstractButton):
        if not isinstance(w, (QCheckBox, QRadioButton)) or "\n" in w.text():
            continue
        fm = w.fontMetrics()
        limit = fm.height() * ems
        text = w.text()
        if fm.horizontalAdvance(text.replace("&", "")) <= limit:
            continue
        lines, cur, brk = [], "", -1
        for ch in text:
            cur += ch
            if ch == " " or not ch.isascii():
                brk = len(cur)
            if fm.horizontalAdvance(cur.replace("&", "")) > limit and len(cur) > 1:
                cut = brk if 0 < brk < len(cur) else len(cur) - 1
                lines.append(cur[:cut].rstrip())
                cur, brk = cur[cut:].lstrip(), -1
        if cur:
            lines.append(cur)
        w.setText("\n".join(lines))


def _has_pixmap(lbl) -> bool:
    pm = lbl.pixmap()
    return pm is not None and not pm.isNull()


def _wrap_labels(dlg, ems: int = 40) -> None:
    """A plain one-line label wider than ~40 line heights set the dialog's
    minimum width; let it wrap instead."""
    for w in dlg.findChildren(QLabel):
        if w.wordWrap() or _has_pixmap(w) or not w.text() or "<" in w.text():
            continue
        fm = w.fontMetrics()
        if fm.horizontalAdvance(w.text()) > fm.height() * ems:
            w.setWordWrap(True)


# ---------------------------------------------------------------- tab order
def _filter_bars_first(dlg) -> None:
    """The table editors build their filter bar after the table (it needs the
    table), so Tab reached it last although it sits at the top. Put it first,
    keeping every other stop in the order it already had."""
    from PySide6.QtCore import Qt
    from taiko2x6.bineditors.table_tools import ToolBar
    bars = dlg.findChildren(ToolBar)
    if not bars:
        return
    first = [w for b in bars for w in (b.ed, b.btn)]
    stops, w = [], dlg
    for _ in range(5000):
        w = w.nextInFocusChain()
        if w is None or w in stops:
            break
        if w not in first and w.focusPolicy() & Qt.TabFocus and w is not dlg                 and not w.objectName().startswith("qt_")                 and not isinstance(w.parentWidget(), (QAbstractSpinBox, QComboBox, QLineEdit)):
            stops.append(w)
    order = first + stops
    for a_, b_ in zip(order, order[1:]):
        QWidget.setTabOrder(a_, b_)


# ------------------------------------------------------------ window geometry
GEOM_SECTION = "dialog_geometry"


def _geom_key(dlg) -> str:
    c = type(dlg)
    return "%s.%s" % (c.__module__.rsplit(".", 1)[-1], c.__name__)


def fit_rect(x, y, w, h, screens, min_w=0, min_h=0):
    """Pull the frame rectangle (x, y, w, h) onto the nearest of `screens`
    (tuples x, y, w, h of available geometry); size is clamped to that screen
    but never below the minimum. Returns (x, y, w, h)."""
    def dist(s):
        sx, sy, sw, sh = s
        dx = max(sx - (x + w), x - (sx + sw), 0)
        dy = max(sy - (y + h), y - (sy + sh), 0)
        return dx * dx + dy * dy
    sx, sy, sw, sh = min(screens, key=dist)
    w = max(min(w, sw), min(min_w, sw))
    h = max(min(h, sh), min(min_h, sh))
    x = min(max(x, sx), sx + sw - w)
    y = min(max(y, sy), sy + sh - h)
    return x, y, w, h


def _avail_screens():
    from PySide6.QtGui import QGuiApplication
    out = []
    for s in QGuiApplication.screens():
        g = s.availableGeometry()
        out.append((g.x(), g.y(), g.width(), g.height()))
    return out


def _enc(b) -> str:
    return base64.b64encode(bytes(b)).decode("ascii")


def _remember_geometry(dlg) -> None:
    from PySide6.QtCore import QObject, QEvent
    class _Filter(QObject):
        shown = False

        def eventFilter(self, obj, ev):
            t = ev.type()
            try:
                if t == QEvent.Show and not ev.spontaneous():
                    if not self.shown:
                        self.shown = True
                        _restore(obj)
                elif t == QEvent.Hide and self.shown and not ev.spontaneous():
                    _save(obj)
            except Exception:
                pass                               # layout memory is a nicety
            return False

    if getattr(dlg, "_geom_filter", None) is None:
        f = _Filter(dlg)
        dlg._geom_filter = f
        dlg.installEventFilter(f)


def _states(dlg):
    from PySide6.QtWidgets import QSplitter, QHeaderView
    for i, sp in enumerate(dlg.findChildren(QSplitter)):
        yield "split%d" % i, sp
    for i, hv in enumerate(dlg.findChildren(QHeaderView)):
        yield "header%d" % i, hv


def _save(dlg) -> None:
    from taiko2x6.app import appconfig
    if dlg.isMaximized() or dlg.isMinimized() or dlg.isFullScreen():
        return
    fg = dlg.frameGeometry()
    key = _geom_key(dlg)
    appconfig.set_value(GEOM_SECTION, key, "%d,%d,%d,%d" % (
        fg.x(), fg.y(), dlg.width(), dlg.height()))
    for name, w in _states(dlg):
        appconfig.set_value(GEOM_SECTION, "%s#%s" % (key, name), _enc(w.saveState()))


def _restore(dlg) -> None:
    from PySide6.QtCore import QByteArray
    from taiko2x6.app import appconfig
    key = _geom_key(dlg)
    raw = appconfig.get(GEOM_SECTION, key)
    screens = _avail_screens()
    if not screens:
        return
    fixed = dlg.minimumSize() == dlg.maximumSize()
    msz = dlg.minimumSizeHint().expandedTo(dlg.minimumSize())
    frame_dx = max(dlg.frameGeometry().width() - dlg.width(), 0)
    frame_dy = max(dlg.frameGeometry().height() - dlg.height(), 0)
    try:
        x, y, w, h = [int(v) for v in raw.split(",")]
    except ValueError:
        x = None
    if x is None:                                  # first open: centre on parent
        par = dlg.parentWidget()
        if par is None:
            return
        pg = par.window().frameGeometry()
        w, h = max(dlg.width(), msz.width()), max(dlg.height(), msz.height())
        x, y = pg.center().x() - w // 2, pg.center().y() - h // 2
        x, y, w, h = fit_rect(x, y, w + frame_dx, h + frame_dy, screens)
        dlg.move(x, y)
    else:
        if fixed:
            w, h = dlg.width(), dlg.height()
        x, y, w, h = fit_rect(x, y, w + frame_dx, h + frame_dy, screens,
                              msz.width() + frame_dx, msz.height() + frame_dy)
        if not fixed:
            dlg.resize(w - frame_dx, h - frame_dy)
        dlg.move(x, y)
    for name, wd in _states(dlg):
        st = appconfig.get(GEOM_SECTION, "%s#%s" % (key, name))
        if st:
            try:
                wd.restoreState(QByteArray(base64.b64decode(st)))
            except Exception:
                pass


def reset_layouts() -> None:
    """Forget every saved dialog size / position / splitter / column state."""
    from taiko2x6.app import appconfig
    appconfig.clear_section(GEOM_SECTION)

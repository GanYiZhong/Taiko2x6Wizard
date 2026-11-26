"""Start page: a grouped, responsive task launcher plus a Recent list.

The page owns no behaviour: every card calls the slot it is given, and the
recent list calls back into the main window.  Colours come from the palette
only, so it follows the light/dark theme.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QVBoxLayout, QWidget)

from taiko2x6.app import icons
from taiko2x6.app.i18n import tr

CARD_MIN_W = 260
MAX_COLS = 4
RECENT_ROLE = Qt.UserRole


def shorten_path(path: str, keep: int = 48) -> str:
    """Shorten a long path to head...tail so it fits a narrow list."""
    p = str(path)
    if len(p) <= keep:
        return p
    parts = Path(p).parts
    tail = parts[-1] if parts else p
    head = parts[0] if parts else ""
    short = f"{head}…{Path(*parts[-2:])}" if len(parts) > 2 else f"…{tail}"
    if len(short) > keep:
        short = "…" + short[-(keep - 1):]
    return short


class TaskCard(QPushButton):
    """A launcher tile: icon, title and a one-line description."""

    def __init__(self, title: str, hint: str, icon, slot: Callable, parent=None):
        super().__init__(parent)
        self.title_text = title
        self.hint_text = hint
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(64)
        self.setToolTip(hint)
        self.setAccessibleName(title)
        self.setAccessibleDescription(hint)
        self.setStyleSheet(
            "TaskCard{text-align:left; border:1px solid palette(mid);"
            " border-radius:6px; background:palette(base);}"
            "TaskCard:hover,TaskCard:focus{border:1px solid palette(highlight);"
            " background:palette(alternate-base);}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        self.icon_name = icon
        ic = self.icon_label = QLabel()
        ic.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.refresh_icon()
        ic.setFixedWidth(32)
        ic.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        lay.addWidget(ic)
        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel(title)
        f = t.font()
        f.setBold(True)
        t.setFont(f)
        t.setWordWrap(True)
        d = QLabel(hint)
        d.setWordWrap(True)
        d.setStyleSheet("color:palette(mid);")
        for w in (t, d):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
            col.addWidget(w)
        col.addStretch(1)
        lay.addLayout(col, 1)
        self.clicked.connect(lambda _=False: slot())

    def refresh_icon(self):
        """(Re)draw the icon in the current palette's text colour."""
        if self.icon_name:
            self.icon_label.setPixmap(icons.icon(self.icon_name).pixmap(28, 28))

    def changeEvent(self, ev):
        if ev.type() in (QEvent.PaletteChange, QEvent.ApplicationPaletteChange):
            self.refresh_icon()
        super().changeEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
            return
        super().keyPressEvent(ev)


class _CardGroup(QWidget):
    """A heading plus a grid of cards that reflows with the available width."""

    def __init__(self, heading: str, cards: list[TaskCard], parent=None):
        super().__init__(parent)
        self.cards = cards
        self._cols = 0
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.heading = QLabel(heading)
        f = self.heading.font()
        f.setBold(True)
        self.heading.setFont(f)
        v.addWidget(self.heading)
        self.grid = QGridLayout()
        self.grid.setSpacing(8)
        v.addLayout(self.grid)
        self.reflow(1)

    def reflow(self, cols: int):
        cols = max(1, min(cols, len(self.cards)))
        if cols == self._cols:
            return
        self._cols = cols
        for c in self.cards:
            self.grid.removeWidget(c)
        for i, c in enumerate(self.cards):
            self.grid.addWidget(c, i // cols, i % cols)
        for col in range(len(self.cards) + 1):
            self.grid.setColumnStretch(col, 1 if col < cols else 0)


class StartPage(QWidget):
    """Task launcher.  ``groups`` = [(heading, [(title, hint, icon, slot), ...])]."""

    recentRemoved = Signal(str)

    def __init__(self, groups, open_recent: Callable[[str, str], None],
                 recent_provider: Callable[[], list[tuple[str, str]]],
                 remove_recent: Callable[[str, str], None], parent=None):
        super().__init__(parent)
        self._open_recent = open_recent
        self._recent_provider = recent_provider
        self._remove_recent = remove_recent

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(self.scroll)
        body = QWidget()
        self.scroll.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(28, 24, 28, 24)
        v.setSpacing(10)

        title = QLabel("Taiko2x6Wizard")
        f = title.font()
        f.setPointSize(f.pointSize() + 10)
        f.setBold(True)
        title.setFont(f)
        v.addWidget(title)
        sub = QLabel(tr("Pick where to start. Everything here is also in the "
                        "File and Tools menus."))
        sub.setWordWrap(True)
        sub.setStyleSheet("color:palette(mid);")
        v.addWidget(sub)

        # setup checklist (owned/refreshed by the main window)
        self.setup_box = QLabel()
        self.setup_box.setWordWrap(True)
        self.setup_box.setStyleSheet(
            "background:rgba(224,160,48,40); color:palette(text);"
            " border:1px solid rgba(224,160,48,160);"
            "border-radius:4px; padding:8px;")
        v.addWidget(self.setup_box)
        self.btn_setup = QPushButton(tr("Open Settings…"))
        v.addWidget(self.btn_setup, 0, Qt.AlignLeft)

        self.cards: list[TaskCard] = []
        self.groups: list[_CardGroup] = []
        for heading, items in groups:
            cards = [TaskCard(t, h, ic, s) for (t, h, ic, s) in items]
            self.cards += cards
            g = _CardGroup(heading, cards)
            self.groups.append(g)
            v.addWidget(g)

        # recent list
        self.recent_heading = QLabel(tr("Recent"))
        f = self.recent_heading.font()
        f.setBold(True)
        self.recent_heading.setFont(f)
        v.addWidget(self.recent_heading)
        self.recent_list = QListWidget()
        self.recent_list.setFrameShape(QFrame.StyledPanel)
        self.recent_list.setContextMenuPolicy(Qt.NoContextMenu)
        self.recent_list.itemActivated.connect(self._on_recent_activated)
        self.recent_list.itemClicked.connect(self._on_recent_activated)
        v.addWidget(self.recent_list)
        self.btn_recent_remove = QPushButton(tr("Remove from list"))
        self.btn_recent_remove.clicked.connect(self.remove_selected_recent)
        v.addWidget(self.btn_recent_remove, 0, Qt.AlignLeft)
        self.recent_list.currentItemChanged.connect(
            lambda *_: self.btn_recent_remove.setEnabled(
                self.recent_list.currentItem() is not None))
        v.addStretch(1)
        self.refresh_recent()

    # ---- recent -------------------------------------------------------
    def refresh_recent(self):
        self.recent_list.clear()
        entries = self._recent_provider()
        dim = self.palette().color(QPalette.Disabled, QPalette.Text)
        for kind, path in entries:
            exists = (Path(path).is_dir() if kind == "folder" else Path(path).exists())
            label = shorten_path(path)
            if kind != "folder":
                label = f"{tr('Disk image')}: {label}"
            if not exists:
                label += "  " + tr("(missing)")
            it = QListWidgetItem(label)
            it.setToolTip(path)
            it.setData(RECENT_ROLE, (kind, path, exists))
            if not exists:
                it.setForeground(dim)
            self.recent_list.addItem(it)
        has = bool(entries)
        self.recent_heading.setVisible(has)
        self.recent_list.setVisible(has)
        self.btn_recent_remove.setVisible(has)
        self.btn_recent_remove.setEnabled(False)
        if has:
            self.recent_list.setFixedHeight(
                min(len(entries), 6) * self.recent_list.sizeHintForRow(0) + 8)

    def _on_recent_activated(self, item: QListWidgetItem):
        kind, path, exists = item.data(RECENT_ROLE)
        if exists:
            self._open_recent(kind, path)

    def remove_selected_recent(self):
        it = self.recent_list.currentItem()
        if it is None:
            return
        kind, path, _ = it.data(RECENT_ROLE)
        self._remove_recent(kind, path)
        self.refresh_recent()

    # ---- responsive layout -------------------------------------------
    def _columns(self) -> int:
        avail = self.scroll.viewport().width() - 56
        return max(1, min(MAX_COLS, avail // (CARD_MIN_W + 8)))

    def relayout(self):
        cols = self._columns()
        for g in self.groups:
            g.reflow(cols)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.relayout()

    def showEvent(self, ev):
        super().showEvent(ev)
        self.relayout()

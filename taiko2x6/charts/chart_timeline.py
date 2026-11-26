"""Read-only visual timeline of a parsed Gen2 .sht chart.

Measures are drawn as columns, notes as coloured dots (don / ka / big / roll /
balloon), GO-GO measures are shaded, a branched chart gets one lane per route,
and BPM / scroll changes are labelled. Nothing is ever written.

``ChartTimelineView`` = zoom buttons + a scroll area around ``ChartTimeline``.
Colours that mean something (note kinds, severities) are fixed mid-tone RGB
values that read on both light and dark windows; everything else comes from
the palette.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from taiko2x6.app.i18n import tr

DON_TYPES = {1, 2, 3}
KA_TYPES = {4, 5}
BIG_TYPES = {7, 8, 11, 13}
ROLL_TYPES = {6, 9}
BALLOON_TYPES = {10, 12}

C_DON = QColor(226, 70, 52)
C_KA = QColor(58, 138, 218)
C_ROLL = QColor(236, 186, 40)
C_BALLOON = QColor(232, 128, 40)
SEV_COLOURS = {"ERROR": QColor(214, 60, 60), "WARN": QColor(222, 160, 30),
               "INFO": QColor(70, 140, 210)}

LABEL_W = 64
RULER_H = 22
LANE_H = 54
MIN_PX, MAX_PX, DEF_PX = 8, 400, 64


def fmt_time(ms: float) -> str:
    s = max(0.0, ms / 1000.0)
    return f"{int(s) // 60}:{s % 60:04.1f}"


def route_lanes(model) -> tuple[bool, list[list[list[dict]]]]:
    from taiko2x6.charts import sht_export
    return sht_export.routes(model)


def lane_names(branched: bool) -> list[str]:
    if not branched:
        return [tr("notes")]
    return [tr("normal route"), tr("professional route"), tr("master route")]


class ChartTimeline(QWidget):
    measureClicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = None
        self._tracks = []
        self._lanes = []              # per track: [notes per route]
        self._branched = False
        self._px = DEF_PX
        self._hi = -1
        self._marks: dict[int, str] = {}
        self.setMouseTracking(True)
        self.setMinimumHeight(RULER_H + LANE_H)

    # ------------------------------------------------------------- data
    def set_model(self, model) -> None:
        self._model = model
        self._tracks = model["tracks"] if model else []
        if model:
            self._branched, self._lanes = route_lanes(model)
        else:
            self._branched, self._lanes = False, []
        self._hi = -1
        self._marks = {}
        self._relayout()

    def set_markers(self, marks: dict[int, str]) -> None:
        """measure index -> worst severity, drawn as a coloured tick."""
        self._marks = dict(marks)
        self.update()

    def set_highlight(self, i: int) -> None:
        self._hi = i
        self.update()

    def highlight(self) -> int:
        return self._hi

    def measure_count(self) -> int:
        return len(self._tracks)

    def lane_count(self) -> int:
        return 3 if self._branched else 1

    def px_per_measure(self) -> int:
        return self._px

    def set_px_per_measure(self, px: int) -> None:
        self._px = max(MIN_PX, min(MAX_PX, int(px)))
        self._relayout()

    def measure_x(self, i: int) -> int:
        return LABEL_W + i * self._px

    def _relayout(self) -> None:
        h = RULER_H + LANE_H * self.lane_count() + 4
        self.setMinimumSize(LABEL_W + max(1, len(self._tracks)) * self._px + 8, h)
        self.resize(self.minimumSize())
        self.update()

    def _measure_at(self, x: float) -> int:
        i = int((x - LABEL_W) // self._px)
        return i if 0 <= i < len(self._tracks) else -1

    # ------------------------------------------------------------ input
    def mousePressEvent(self, e):
        i = self._measure_at(e.position().x())
        if i >= 0:
            self._hi = i
            self.update()
            self.measureClicked.emit(i)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            step = 1.25 if e.angleDelta().y() > 0 else 0.8
            self.set_px_per_measure(round(self._px * step))
            e.accept()
        else:
            e.ignore()

    def tool_tip_for(self, i: int) -> str:
        t = self._tracks[i]
        return tr("measure {n}  at {t}  BPM {b:g}").format(
            n=i + 1, t=fmt_time(t["time"]), b=t["bpm"])

    def mouseMoveEvent(self, e):
        i = self._measure_at(e.position().x())
        self.setToolTip(self.tool_tip_for(i) if i >= 0 else "")

    # ------------------------------------------------------------ paint
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        p.fillRect(self.rect(), pal.base())
        if not self._tracks:
            p.setPen(pal.color(pal.ColorRole.Mid))
            p.drawText(self.rect(), Qt.AlignCenter, tr("(no chart)"))
            return
        text = pal.color(pal.ColorRole.Text)
        mid = pal.color(pal.ColorRole.Mid)
        hl = pal.color(pal.ColorRole.Highlight)
        lanes = self.lane_count()
        px = self._px
        vis = ev.rect()
        first = max(0, (vis.left() - LABEL_W) // px - 1)
        last = min(len(self._tracks) - 1, (vis.right() - LABEL_W) // px + 1)
        body_h = LANE_H * lanes

        # lane labels
        p.setPen(text)
        for r, nm in enumerate(lane_names(self._branched)[:lanes]):
            p.drawText(QRectF(2, RULER_H + r * LANE_H, LABEL_W - 4, LANE_H),
                       Qt.AlignVCenter | Qt.TextWordWrap, nm)

        prev_bpm = self._tracks[first - 1]["bpm"] if first > 0 else None
        prev_sc = self._tracks[first - 1]["scrollSpeeds"][0] if first > 0 else None
        gogo_col = QColor(236, 186, 40, 45)
        for i in range(first, last + 1):
            t = self._tracks[i]
            x = self.measure_x(i)
            if t["gogoFlag"] == 1:
                p.fillRect(x, RULER_H, px, body_h, gogo_col)
            if i == self._hi:
                c = QColor(hl)
                c.setAlpha(70)
                p.fillRect(x, 0, px, RULER_H + body_h, c)
            sev = self._marks.get(i)
            if sev:
                p.fillRect(x, 0, px, 4, SEV_COLOURS.get(sev, mid))
            p.setPen(QPen(mid, 1))
            p.drawLine(x, RULER_H - 6, x, RULER_H + body_h)
            if px >= 22 or i % max(1, 28 // px + 1) == 0:
                p.setPen(text)
                p.drawText(x + 3, RULER_H - 8, str(i + 1))
            # BPM / scroll change marks
            bpm, sc = t["bpm"], t["scrollSpeeds"][0]
            if prev_bpm is not None and bpm != prev_bpm and px >= 36:
                p.setPen(SEV_COLOURS["INFO"])
                p.drawText(x + 3, RULER_H + 11, f"♩{bpm:g}")
            if prev_sc is not None and sc != prev_sc and px >= 36:
                p.setPen(C_BALLOON)
                p.drawText(x + 3, RULER_H + body_h - 4, f"×{sc:g}")
            prev_bpm, prev_sc = bpm, sc
            self._paint_notes(p, i, x, t)
        # lane separators
        p.setPen(QPen(mid, 1))
        for r in range(lanes + 1):
            y = RULER_H + r * LANE_H
            p.drawLine(LABEL_W, y, self.width(), y)
        p.end()

    def _paint_notes(self, p, i, x, t):
        px = self._px
        bar_ms = 240000.0 / t["bpm"] if t["bpm"] > 0 else 0.0
        rad = 4 if px >= 48 else 3 if px >= 20 else 1.5
        for r in range(self.lane_count()):
            cy = RULER_H + r * LANE_H + LANE_H / 2
            for n in self._lanes[i][r]:
                ty = n["type"]
                nx = x + n["measure"] / 48.0 * px
                if ty in ROLL_TYPES and n["longNoteLength"] > 0 and bar_ms:
                    w = n["longNoteLength"] / bar_ms * px
                    c = QColor(C_ROLL)
                    c.setAlpha(150)
                    p.fillRect(QRectF(nx, cy - rad, w, rad * 2), c)
                col = (C_DON if ty in DON_TYPES or ty in (7, 11) else
                       C_KA if ty in KA_TYPES or ty in (8, 13) else
                       C_ROLL if ty in ROLL_TYPES else
                       C_BALLOON if ty in BALLOON_TYPES else C_DON)
                big = ty in BIG_TYPES or ty == 9
                rr = rad * 1.5 if big or ty in BALLOON_TYPES else rad
                p.setPen(QPen(self.palette().color(self.palette().ColorRole.WindowText), 1))
                p.setBrush(col)
                p.drawEllipse(QRectF(nx - rr, cy - rr, rr * 2, rr * 2))


class ChartTimelineView(QWidget):
    """Timeline + zoom buttons + legend, scrollable."""
    measureClicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.b_out = QPushButton("−")
        self.b_in = QPushButton("+")
        self.b_out.setToolTip(tr("Zoom out (Ctrl + wheel)"))
        self.b_in.setToolTip(tr("Zoom in (Ctrl + wheel)"))
        row.addWidget(self.b_out)
        row.addWidget(self.b_in)
        self.legend = QLabel(tr("red = don, blue = ka, large = big note, "
                                "yellow bar = roll, orange = balloon, "
                                "shaded = GO-GO"))
        self.legend.setWordWrap(True)
        row.addWidget(self.legend, 1)
        lay.addLayout(row)
        self.timeline = ChartTimeline()
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.timeline)
        self.scroll.setWidgetResizable(False)
        lay.addWidget(self.scroll, 1)
        self.b_in.clicked.connect(lambda: self.timeline.set_px_per_measure(
            round(self.timeline.px_per_measure() * 1.5)))
        self.b_out.clicked.connect(lambda: self.timeline.set_px_per_measure(
            round(self.timeline.px_per_measure() / 1.5)))
        self.timeline.measureClicked.connect(self.measureClicked)

    def set_model(self, model) -> None:
        self.timeline.set_model(model)

    def set_markers(self, marks) -> None:
        self.timeline.set_markers(marks)

    def show_measure(self, i: int) -> None:
        """Highlight measure `i` (0-based) and scroll it into view."""
        if not 0 <= i < self.timeline.measure_count():
            return
        self.timeline.set_highlight(i)
        x = self.timeline.measure_x(i)
        self.scroll.ensureVisible(x + self.timeline.px_per_measure() // 2, 0,
                                  self.timeline.px_per_measure() * 2, 0)

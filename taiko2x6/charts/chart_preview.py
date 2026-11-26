"""Read-only summary of a Gen2 .sht chart for the explorer's Info tab.

Uses the existing parser (tja2sht.parse_sht) and the route helper from
sht_export; nothing is written.
"""
from __future__ import annotations

from taiko2x6.app.i18n import tr

# Note types (see sht_export._TJA_CHAR): hit notes, drum rolls, balloons.
HIT_TYPES = {1, 2, 3, 4, 5, 7, 8, 11, 13}
ROLL_TYPES = {6, 9}
BALLOON_TYPES = {10, 12}


def is_chart_name(name: str) -> bool:
    n = name.lower()
    return n.endswith(".sht") or n == "sht"


def _mmss(ms: float) -> str:
    s = max(0, int(round(ms / 1000)))
    return f"{s // 60}:{s % 60:02d}"


def parse_chart(data: bytes):
    """Parsed model for the timeline view (read-only)."""
    from taiko2x6.charts import tja2sht
    return tja2sht.parse_sht(data)


def summarize_sht(name: str, data: bytes) -> str:
    """Multi-line summary text; raises ValueError for unparsable data."""
    from taiko2x6.charts import tja2sht, sht_export
    m = tja2sht.parse_sht(data)
    tracks = m["tracks"]
    if not tracks:
        raise ValueError("no measures")
    course = sht_export.course_from_name(name) or "?"
    branched, per_track = sht_export.routes(m)
    names = (tr("normal route"), tr("professional route"), tr("master route"))
    bpms = [t["bpm"] for t in tracks if t["bpm"] > 0]
    lines = [tr("Chart (.sht)"),
             tr("course      : {c}").format(c=course),
             tr("measures    : {n}").format(n=len(tracks))]
    if bpms:
        lo, hi = min(bpms), max(bpms)
        lines.append(tr("BPM         : {b}").format(
            b=f"{lo:g}" if lo == hi else f"{lo:g} - {hi:g}"))
    last = tracks[-1]
    bar = 240000.0 / last["bpm"] if last["bpm"] > 0 else 0.0
    lines.append(tr("length      : about {t}").format(t=_mmss(last["time"] + bar)))
    lines.append(tr("branches    : {b}").format(b=tr("yes") if branched else tr("no")))
    for r in range(3 if branched else 1):
        notes = [n for t in per_track for n in t[r]]
        hits = sum(1 for n in notes if n["type"] in HIT_TYPES)
        rolls = sum(1 for n in notes if n["type"] in ROLL_TYPES)
        balls = sum(1 for n in notes if n["type"] in BALLOON_TYPES)
        label = names[r] if branched else tr("notes")
        lines.append(tr("{label}: {h} hits, {r} rolls, {b} balloons").format(
            label=label, h=hits, r=rolls, b=balls))
    gogo = sum(1 for t in tracks if t["gogoFlag"] == 1)
    if gogo:
        lines.append(tr("GO-GO measures: {n}").format(n=gogo))
    return "\n".join(lines)

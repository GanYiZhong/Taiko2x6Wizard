"""Gen2 sht -> TJA, and Gen2 sht -> Gen3 (Nijiiro) fumen .bin.

The reverse of tja2sht / gen3_convert. Both directions rest on the same facts:

  * Note types are the same enum in Gen2 and Gen3 (see gen3_convert).
  * A Gen2 note position is 1/48 of a 4/4 bar at the track's bpm (240000/bpm
    ms), whatever the measure's length; the measure's length is only the gap
    to the next track's time.
  * Gen2 track.time == Gen3 measure offset, and Gen2 bunkis == Gen3
    branchInfo value for value.

Gen3 header
-----------
Timing windows, soul-gauge clear line and branch point values are constants
per difficulty (identical on every shipped chart of that difficulty). The
soul-gauge gains depend on difficulty, star level and note count; they come
from tja2fumen's table (data/hp_values.csv, MIT), which reproduces the shipped
charts. The per-note score fields and the header word at 0x1FC are written as
tja2fumen writes them (0 and a placeholder), so the game scores the chart
itself.

TJA branches
------------
Gen2 stores a branch threshold as diverge POINTS (20 per don/ka, 30 per balloon
or kusudama) rather than a percentage. The TJA percentage is recovered from the
normal route's points since the previous branch point, so it is approximate; a
section with no such points is written as a drumroll (r) condition.

Round-trip fidelity (measured by tests/test_chart_roundtrip.py on seeded
synthetic charts)
-------------------------------------------------------------------
sht -> Gen3 -> sht is exact: note types, 1/48 positions, roll lengths (whole
ms), balloon counts, track times (float32), bpm, gogo, bar line, bunkis and the
three route speeds all come back identical. What cannot come back, and why:

  * scrollSpeeds[3:6]. Gen3 stores one speed per route (3), Gen2 six; the
    second three are unused copies, rebuilt as copies of the first three.
  * a roll with length <= 0 (Gen2 -1) is written with duration 0 and reads
    back as 0; a balloon with count -1 reads back 0. Neither occurs in charts
    from tja2sht or the game.
  * notes at 1/48 slot >= 48 (outside the grid) are clamped to 47.

sht -> TJA -> sht keeps types (don/ka and big/two-hand), positions, roll
lengths (exact to the ms), balloon counts, bpm/gogo/bar line/scroll, and the
bunkis of any chart whose thresholds are reachable from the notes in the
file. It loses:

  * Gen3 syllable variants (types 2, 3 -> don, 5 -> ka): TJA has no such
    notes.
  * thresholds above the diverge points the normal route offered since the
    previous branch point (written as 100%), and the per-route pairs when they
    do not follow one percentage (TJA has one condition per branch point;
    tja2sht expands it per route). sht does not record #SECTION positions.
  * two notes in one 1/48 slot (sht can hold them, TJA cannot: tja2sht nudges
    the second to a free neighbour, which can move the notes after it by one
    slot).
"""
from __future__ import annotations

import bisect
import csv
import gzip
import math
import os
import pathlib
import struct
from fractions import Fraction

from taiko2x6.charts import gen3_convert, tja2sht

_ROLL = frozenset({6, 9, 10, 12})
_BALLOON = frozenset({10, 12})
_NONE = [-1] * 6
# TJA cells per Gen2 grid unit: notes sit on the 1/48-bar grid, but a roll's
# end is placed on a grid 8x finer so long rolls keep their length.
_SUB = 8
# Finer grids tried, per measure, until every roll end in it lands within
# _TOL_MS of its true time (a longer line only where a roll ends). tja2sht
# rounds a roll's length to whole ms, so an end that is off by up to 0.5 ms
# can still come back 1 ms long or short; staying under 0.3 ms makes every
# whole-ms length round-trip exactly (0.5 gave 35 of 7553 rolls 1 ms off).
_SUBS = (8, 16, 32, 64, 128)
_TOL_MS = 0.3
# An end this close before a track's time belongs to that track (cell 0).
_SNAP_MS = 0.5

COURSES = ("easy", "normal", "hard", "oni")
_TJA_COURSE = {"easy": "Easy", "normal": "Normal", "hard": "Hard",
               "oni": "Oni", "ura": "Edit"}

# Gen3 type -> TJA character. 2/3 and 5 are don/ka variants (Gen3 writes the
# syllable, TJA does not); 11/13 are the two-hand big notes.
_TJA_CHAR = {1: "1", 2: "1", 3: "1", 4: "2", 5: "2", 7: "3", 8: "4",
             6: "5", 9: "6", 10: "7", 12: "9", 11: "A", 13: "B"}


def _norm_course(course):
    c = str(course).lower()
    c = {"e": "easy", "n": "normal", "h": "hard", "m": "oni", "x": "ura",
         "edit": "ura"}.get(c, c)
    if c not in COURSES and c != "ura":
        raise ValueError("unknown course %r" % course)
    return c


def _model(sht):
    return tja2sht.parse_sht(sht) if isinstance(sht, (bytes, bytearray)) else sht


def is_branched(model):
    tr = model["tracks"]
    return (any(list(t["bunkis"]) != _NONE for t in tr) or
            any(t["subtracks"][s]["noteCount"] for t in tr for s in (1, 2)))


def routes(model):
    """(branched, per-track [normal, pro, master] note lists)."""
    div = is_branched(model)
    notes = model["notes"]
    out = []
    for t in model["tracks"]:
        r = []
        for b in range(3):
            s = t["subtracks"][b if div else 0]
            r.append(notes[s["noteIndexSt"]:s["noteIndexSt"] + s["noteCount"]])
        out.append(r)
    return div, out


def _speeds(t):
    sp = t.get("scrollSpeeds") or [1.0] * 6
    return [float(x) for x in sp]


def _bar_ms(t):
    bpm = float(t["bpm"])
    return 240000.0 / bpm if bpm > 0 else 0.0


# --- Gen3 fumen ------------------------------------------------------------

_TIMING = {
    "easy":   (41.7083358764648, 108.441665649414, 125.125),
    "normal": (41.7083358764648, 108.441665649414, 125.125),
    "hard":   (25.0250015258789, 75.075004577637, 108.441665649414),
    "oni":    (25.0250015258789, 75.075004577637, 108.441665649414),
}
_HP_CLEAR = {"easy": 6000, "normal": 7000, "hard": 7000, "oni": 8000}
_STAR_KEY = {
    "oni":    {1: "17", 2: "17", 3: "17", 4: "17", 5: "17", 6: "17", 7: "17",
               8: "8", 9: "910", 10: "910"},
    "hard":   {1: "12", 2: "12", 3: "3", 4: "4", 5: "58", 6: "58", 7: "58",
               8: "58", 9: "58", 10: "58"},
    "normal": {1: "12", 2: "12", 3: "3", 4: "4", 5: "57", 6: "57", 7: "57",
               8: "57", 9: "57", 10: "57"},
    "easy":   {1: "1", 2: "23", 3: "23", 4: "45", 5: "45", 6: "45", 7: "45",
               8: "45", 9: "45", 10: "45"},
}
# Star level used when none is given: a typical level for the difficulty.
DEFAULT_STARS = {"easy": 3, "normal": 5, "hard": 6, "oni": 8}
_HP_TABLE = None


def _hp_table():
    global _HP_TABLE
    if _HP_TABLE is None:
        p = pathlib.Path(__file__).with_name("data") / "hp_values.csv"
        with open(p, newline="", encoding="utf-8") as f:
            _HP_TABLE = list(csv.DictReader(f))
    return _HP_TABLE


def hp_values(course, stars, n_notes):
    """Soul-gauge (good, ok, bad) for a chart, as the game's own charts set it."""
    c = _norm_course(course)
    c = "oni" if c == "ura" else c
    stars = min(10, max(1, int(stars or DEFAULT_STARS[c])))
    key = "%s-%s" % (c.capitalize(), _STAR_KEY[c][stars])
    table = _hp_table()
    i = min(max(1, int(n_notes)), len(table)) - 1
    # The table's values fall to 0 for very long charts (~2300+ notes),
    # which would make the gauge impossible to fill: use the last row that
    # still gives something.
    while i > 0 and int(table[i]["good_" + key]) <= 0:
        i -= 1
    row = table[i]
    return (max(1, int(row["good_" + key])), max(1, int(row["ok_" + key])),
            int(row["bad_" + key]))


def sht_to_gen3(sht, course="oni", stars=None, score_init=0, score_diff=0):
    """Gen2 sht (bytes or parsed model) -> decrypted Gen3 fumen bytes."""
    course = _norm_course(course)
    hc = "oni" if course == "ura" else course
    m = _model(sht)
    tracks = m["tracks"]
    if not tracks:
        raise ValueError("sht has no tracks")
    div, rts = routes(m)

    body = bytearray()
    for k, t in enumerate(tracks):
        bar = _bar_ms(t)
        info = [int(x) for x in t["bunkis"]] if div else _NONE
        body += struct.pack("<ffBBH6ii", float(t["bpm"]), float(t["time"]),
                            1 if t["gogoFlag"] else 0,
                            1 if t["trackLine"] else 0, 0, *info, 0)
        sp = _speeds(t)
        for b in range(3):
            ns = rts[k][b] if (div or b == 0) else []
            # Unbranched charts repeat the normal route's speed in the empty
            # branches, as the shipped ones do.
            body += struct.pack("<HHf", len(ns), 0, sp[b] if div else sp[0])
            for n in ns:
                ty = int(n["type"])
                if ty in _BALLOON:
                    a, c = max(0, int(n["balloonHitCount"])), 0
                else:
                    a = min(65535, max(0, score_init))
                    c = min(65535, max(0, score_diff * 4))
                ln = int(n["longNoteLength"])
                dur = float(ln) if ty in _ROLL and ln > 0 else 0.0
                body += struct.pack("<ifiiHHf", ty, n["measure"] / 48.0 * bar,
                                    0, 0, a, c, dur)
                if ty in gen3_convert._LONG_NOTE_TYPES:
                    body += b"\0" * 8

    def combo(b):
        return sum(1 for r in rts for n in r[b] if n["type"] not in _ROLL)

    good, ok, bad = hp_values(hc, stars, combo(0))

    def ratio(b):
        return int(65536 * combo(0) / combo(b)) if div and combo(b) else 65536

    hdr = struct.pack("<108f", *(_TIMING[hc] * 36))
    hdr += struct.pack(
        "<22i", int(div), 10000, _HP_CLEAR[hc], good, ok, bad,
        65536, ratio(1), ratio(2),
        20, 10, 0, 1, 20, 10, 1, 30, 30, 20,     # diverge points per judgement
        12345678, len(tracks), 0)
    return hdr + bytes(body)


def encrypt_fumen(data, key=gen3_convert.FUMEN_KEY, iv=None):
    """gzip + AES-256-CBC (IV prepended, PKCS7): the inverse of decrypt_fumen."""
    raw = gzip.compress(bytes(data), mtime=0)
    pad = 16 - len(raw) % 16
    raw += bytes([pad]) * pad
    iv = iv if iv is not None else os.urandom(16)
    try:
        from Crypto.Cipher import AES
        enc = AES.new(key, AES.MODE_CBC, iv).encrypt(raw)
    except ImportError:
        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes)
        e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        enc = e.update(raw) + e.finalize()
    return iv + enc


def sht_to_gen3_bin(sht, course="oni", stars=None, encrypt=True, **kw):
    """Gen2 sht -> Gen3 fumen .bin bytes, encrypted like the shipped ones."""
    data = sht_to_gen3(sht, course=course, stars=stars, **kw)
    return encrypt_fumen(data) if encrypt else data


# --- TJA -------------------------------------------------------------------

def _num(x, nd=6):
    s = ("%.*f" % (nd, float(x))).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _points(ns):
    return sum(30 if n["type"] in _BALLOON else 0 if n["type"] in _ROLL else 20
               for n in ns)


class _Course:
    """One sht laid out on a TJA timeline."""

    def __init__(self, model, origin_ms):
        self.tracks = tr = model["tracks"]
        self.div, self.rts = routes(model)
        K = len(tr)
        self.n48 = []
        for k, t in enumerate(tr):
            bar = _bar_ms(t)
            gap = (tr[k + 1]["time"] - t["time"]) if k + 1 < K else bar
            n = int(round(gap / bar * 48)) if bar > 0 else 48
            top = max((x["measure"] + 1 for r in self.rts[k] for x in r),
                      default=0)
            # A track holds at most one 4/4 bar of grid; a longer gap (a
            # pause before a tempo change, a 6/4 tail) is a #DELAY instead,
            # so the TJA does not grow an extra measure.
            self.n48.append(max(min(n, 48), top, 1))
        # #DELAY before each measure so the TJA timeline lands on track times.
        # cur follows the TJA reader's clock, so skipped sub-0.25 ms delays
        # cannot add up.
        self.delay = []
        cur = origin_ms
        for k, t in enumerate(tr):
            d = float(t["time"]) - cur
            d = d if abs(d) >= 0.25 else 0.0
            self.delay.append(d)
            cur += d + self.n48[k] / 48.0 * _bar_ms(t)
        self.times = [t["time"] for t in tr]
        self.sub = self._pick_subs()
        self.balloons = [[], [], []]
        self.grid = [[{} for _ in tr] for _ in range(3)]
        for b in range(3):
            self._fill(b)

    def _pick_subs(self):
        """Cells per 1/48 slot for each track: the smallest of _SUBS that
        puts every roll end of that track within _TOL_MS."""
        tr = self.tracks
        K = len(tr)
        times = [t["time"] for t in tr]
        need = [[] for _ in tr]          # fractional 1/48 slots of the ends
        for k, t in enumerate(tr):
            bar = _bar_ms(t)
            if bar <= 0:
                continue
            for r in self.rts[k]:
                for n in r:
                    if int(n["type"]) not in _ROLL:
                        continue
                    ms = (t["time"] + int(n["measure"]) / 48.0 * bar
                          + max(1, int(n["longNoteLength"])))
                    j = bisect.bisect_right(times, ms + _SNAP_MS) - 1
                    j = max(j, 0)
                    bj = _bar_ms(tr[j])
                    if bj > 0:
                        need[j].append((ms - times[j]) / bj * 48.0)
        subs = []
        for k in range(K):
            tol = _TOL_MS / _bar_ms(tr[k]) * 48.0 if _bar_ms(tr[k]) > 0 else 1
            pick = _SUBS[-1]
            for sub in _SUBS:
                if all(abs(f * sub - round(f * sub)) <= tol * sub
                       for f in need[k]):
                    pick = sub
                    break
            subs.append(pick)
        return subs

    def _locate(self, ms):
        """(measure, cell) of an absolute time, for a roll's end marker."""
        tr = self.tracks
        k = max(0, bisect.bisect_right(self.times, ms + _SNAP_MS) - 1)
        bar = _bar_ms(tr[k])
        sub = self.sub[k]
        p = int(round((ms - tr[k]["time"]) / bar * 48 * sub)) if bar > 0 else 0
        if (p >= self.n48[k] * sub and k + 1 < len(tr)
                and ms >= tr[k + 1]["time"] - _SNAP_MS):
            return k + 1, 0
        # an end in the pause before the next track stays in this one
        return k, max(0, min(p, self.n48[k] * sub - 1))

    def _fill(self, b):
        g = self.grid[b]
        ends = []
        for k, r in enumerate(self.rts):
            # TJA reads a measure left to right (BALLOON counts too); the sht
            # does not always list a measure's notes in that order.
            sub = self.sub[k]
            last = self.n48[k] * sub - 1
            for n in sorted(r[b], key=lambda n: n["measure"]):
                ty = int(n["type"])
                ch = _TJA_CHAR.get(ty)
                if ch is None:
                    continue
                pos = int(n["measure"]) * sub
                # two notes on one 1/48 slot (a chart denser than the grid):
                # the second goes to the next free fine cell, not over it
                while pos in g[k] and pos < last:
                    pos += 1
                g[k][pos] = ch
                if ty in _BALLOON:
                    self.balloons[b].append(max(0, int(n["balloonHitCount"])))
                if ty in _ROLL:
                    t = self.tracks[k]
                    start = t["time"] + pos / (48.0 * sub) * _bar_ms(t)
                    ends.append((k, pos, start + max(1, int(n["longNoteLength"]))))
        # Roll ends go in after every note, so no note can land on (and
        # erase) one. A taken cell moves the end back towards its roll.
        for k, pos, end_ms in ends:
            ek, ep = self._locate(end_ms)
            if (ek, ep) <= (k, pos):
                ek, ep = k, pos + 1
            while ep in g[ek] and (ek, ep) > (k, pos + 1):
                if ep == 0:
                    ek -= 1
                    ep = self.n48[ek] * self.sub[ek]
                ep -= 1
            while ep in g[ek] and ep < self.n48[ek] * self.sub[ek] - 1:
                ep += 1
            g[ek].setdefault(ep, "8")

    def branch_points(self):
        if not self.div:
            return []
        bp = [k for k, t in enumerate(self.tracks)
              if list(t["bunkis"]) != _NONE]
        # Routes that already differ before the first branch point (some dan
        # charts) need the branch block to start at measure 0.
        first = bp[0] if bp else len(self.tracks)
        if not bp or any(self.grid[0][k] != self.grid[b][k]
                         for k in range(first) for b in (1, 2)):
            bp = [0] + [k for k in bp if k]
        return bp

    def condition(self, k, prev):
        info = list(self.tracks[k]["bunkis"])
        pts = sum(_points(self.rts[j][0]) for j in range(prev, k))
        a, b = info[0], info[1]
        if info == _NONE:
            # routes that differ before any real branch point: no decision
            # here (tja2sht reads -1 back as "none")
            return "p,-1,-1"
        if pts <= 0:
            return "r,%d,%d" % (max(a, 0), max(b, 0))

        def pct(v):
            if v <= 0:
                return 0
            if v == 999:            # tja2fumen's "never" marker; real
                return 101          # thresholds do go above it (1196 ...)
            # exact to 1/10000 %, rounded up, so int(points * pct / 100)
            # gives back v (tja2sht and tja2fumen both read it that way)
            # A threshold above what the normal route offered since the last
            # branch point comes from a longer section than the sht records:
            # ask for 100% rather than turning it into "never".
            return min(100.0, math.ceil(v * 1000000.0 / pts - 1e-6) / 10000.0)
        return "p,%s,%s" % (_num(pct(a), 4), _num(pct(b), 4))

    def line(self, b, k):
        cells = self.grid[b][k]
        n = self.n48[k] * self.sub[k]
        step = n
        for p in cells:
            step = math.gcd(step, p)
        s = ["0"] * (n // step)
        for p, ch in cells.items():
            s[p // step] = ch
        return "".join(s) + ","


def _course_lines(c, header_bpm):
    out = []
    st0 = {"bpm": float(header_bpm), "scroll": 1.0, "gogo": False,
           "line": True, "meas": Fraction(1)}

    def emit(k, b, st, force=False):
        """One measure. force: restate everything (start of a branch route,
        so readers that run the routes in sequence and readers that give
        each route its own state both see the right values)."""
        t = c.tracks[k]
        if c.delay[k]:
            out.append("#DELAY %s" % _num(c.delay[k] / 1000.0))
        if force or float(t["bpm"]) != st["bpm"]:
            st["bpm"] = float(t["bpm"])
            out.append("#BPMCHANGE %s" % _num(st["bpm"], 4))
        meas = Fraction(c.n48[k], 48)
        if force or meas != st["meas"]:
            st["meas"] = meas
            num, den = meas.numerator, meas.denominator
            if den < 4 and 4 % den == 0:
                num, den = num * (4 // den), 4
            out.append("#MEASURE %d/%d" % (num, den))
        sp = _speeds(t)[b]
        if force or sp != st["scroll"]:
            st["scroll"] = sp
            out.append("#SCROLL %s" % _num(sp, 4))
        gogo = bool(t["gogoFlag"])
        if force or gogo != st["gogo"]:
            st["gogo"] = gogo
            out.append("#GOGOSTART" if gogo else "#GOGOEND")
        line = bool(t["trackLine"])
        if force or line != st["line"]:
            st["line"] = line
            out.append("#BARLINEON" if line else "#BARLINEOFF")
        out.append(c.line(b, k))

    K = len(c.tracks)
    bps = c.branch_points()
    first = bps[0] if bps else K
    st = st0
    for k in range(first):
        emit(k, 0, st)
    if bps:
        prev = 0
        for i, k0 in enumerate(bps):
            k1 = bps[i + 1] if i + 1 < len(bps) else K
            out.append("#BRANCHSTART %s" % c.condition(k0, prev))
            for b, tag in ((0, "#N"), (1, "#E"), (2, "#M")):
                out.append(tag)
                for k in range(k0, k1):
                    emit(k, b, st, force=(k == k0))
            prev = k0
        out.append("#BRANCHEND")
    return out


def sht_to_tja(charts, title="", subtitle="", wave="", levels=None,
               demostart=None):
    """{course: sht bytes or model} -> TJA text with one COURSE per chart."""
    levels = levels or {}
    order = [c for c in ("ura", "oni", "hard", "normal", "easy")]
    models = {}
    for k, v in charts.items():
        models[_norm_course(k)] = _model(v)
    if not models:
        raise ValueError("no charts")
    lead = next(models[c] for c in reversed(order) if c in models)
    origin = float(lead["tracks"][0]["time"])
    bpm = float(lead["tracks"][0]["bpm"])

    def one_line(v):
        return " ".join(str(v or "").split())
    title, subtitle, wave = one_line(title), one_line(subtitle), one_line(wave)
    lines = ["TITLE:%s" % title]
    if subtitle:
        lines.append("SUBTITLE:%s" % subtitle)
    lines += ["BPM:%s" % _num(bpm, 4), "WAVE:%s" % wave,
              "OFFSET:%s" % _num(-origin / 1000.0)]
    if demostart is not None:
        lines.append("DEMOSTART:%s" % _num(demostart))
    lines.append("")
    for cname in order:
        if cname not in models:
            continue
        c = _Course(models[cname], origin)
        lines.append("COURSE:%s" % _TJA_COURSE[cname])
        lv = levels.get(cname) or DEFAULT_STARS.get(
            "oni" if cname == "ura" else cname)
        lines.append("LEVEL:%d" % int(lv))
        if c.div:
            for tag, b in (("BALLOONNOR", 0), ("BALLOONEXP", 1),
                           ("BALLOONMAS", 2)):
                if c.balloons[b]:
                    lines.append("%s:%s" % (tag, ",".join(map(str, c.balloons[b]))))
            seq = []
            bps = c.branch_points()
            first = bps[0] if bps else len(c.tracks)
            # BALLOON in file order, for players without per-route lists
            pre = [n for k in range(first)
                   for n in sorted(c.rts[k][0], key=lambda n: n["measure"])
                   if n["type"] in _BALLOON]
            seq += [max(0, int(n["balloonHitCount"])) for n in pre]
            for i, k0 in enumerate(bps):
                k1 = bps[i + 1] if i + 1 < len(bps) else len(c.tracks)
                for b in range(3):
                    seq += [max(0, int(n["balloonHitCount"]))
                            for k in range(k0, k1)
                            for n in sorted(c.rts[k][b],
                                            key=lambda n: n["measure"])
                            if n["type"] in _BALLOON]
            if seq:
                lines.append("BALLOON:%s" % ",".join(map(str, seq)))
        elif c.balloons[0]:
            lines.append("BALLOON:%s" % ",".join(map(str, c.balloons[0])))
        lines.append("")
        lines.append("#START")
        lines += _course_lines(c, bpm)
        lines.append("#END")
        lines.append("")
    return "\n".join(lines)


# --- helpers for files -----------------------------------------------------

def song_id_of(path):
    """'…/abc1p_m/sht' or '…/abc_m.sht' -> 'abc'."""
    p = pathlib.Path(path)
    name = p.parent.name if p.name.lower() == "sht" else p.stem
    low = name.lower()
    for mark in ("1p_", "2p_"):
        if mark in low:
            return name[:low.rindex(mark)]
    if len(name) > 2 and name[-2] == "_":
        return name[:-2]
    return name



def course_from_name(name):
    """Guess the course from a chart file/folder name (…_e / …1p_m / …_x)."""
    p = pathlib.Path(name)
    s = (p.parent.name if p.name.lower() == "sht" else p.stem).lower()
    for suf, c in (("_e", "easy"), ("_n", "normal"), ("_h", "hard"),
                   ("_m", "oni"), ("_x", "ura")):
        if s.endswith(suf):
            return c
    return None


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Convert Gen2 (PS2 arcade) .sht charts to TJA or to a "
                    "Gen3 (Nijiiro) fumen .bin.")
    ap.add_argument("sht", nargs="+",
                    help="sht files; for TJA give several (one per course)")
    ap.add_argument("--to", choices=("tja", "bin"), required=True)
    ap.add_argument("-o", "--out", help="output file (TJA) or folder (bin)")
    ap.add_argument("--course", help="course of a single sht "
                    "(easy/normal/hard/oni/ura); default from the file name")
    ap.add_argument("--stars", type=int, help="star level (bin: soul gauge)")
    ap.add_argument("--title", default="")
    ap.add_argument("--wave", default="")
    ap.add_argument("--plain", action="store_true",
                    help="bin: write it unencrypted")
    a = ap.parse_args(argv)

    def course_of(p):
        c = a.course if len(a.sht) == 1 and a.course else course_from_name(p)
        if not c:
            ap.error("cannot tell the course of %s; pass --course" % p)
        return _norm_course(c)

    if a.to == "tja":
        charts = {course_of(p): pathlib.Path(p).read_bytes() for p in a.sht}
        if a.stars and len(charts) > 1:
            ap.error("--stars sets one course's level; give one sht with it")
        lv = {c: a.stars for c in charts} if a.stars else None
        text = sht_to_tja(charts, title=a.title, wave=a.wave, levels=lv)
        out = a.out or str(pathlib.Path(a.sht[0]).with_suffix(".tja"))
        pathlib.Path(out).write_text(text, encoding="utf-8-sig")
        print("wrote %s" % out)
        return 0
    for p in a.sht:
        c = course_of(p)
        data = sht_to_gen3_bin(pathlib.Path(p).read_bytes(), course=c,
                               stars=a.stars, encrypt=not a.plain)
        src = pathlib.Path(p)
        # the Gen3 name, as chart_convert writes it: <id>_<letter>.bin
        name = "%s_%s" % (song_id_of(src), {"easy": "e", "normal": "n",
                                            "hard": "h", "oni": "m",
                                            "ura": "x"}[c])
        dst = pathlib.Path(a.out or src.parent) / (name + ".bin")
        dst.write_bytes(data)
        print("wrote %s (%s, %d bytes)" % (dst, c, len(data)))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

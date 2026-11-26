"""Seeded round trips on synthetic charts: sht -> Gen3 -> sht, sht -> TJA ->
sht and TJA -> sht -> Gen3 -> sht, with numeric thresholds. What is lossy by
design is listed in the sht_export module docstring."""
import random
from collections import Counter

from taiko2x6.charts import gen3_convert, gen3_song, sht_export, tja2sht

_ROLLS = (6, 9, 10, 12)


def synth(seed, branched, tja_safe=False):
    """A random valid sht: 4-14 tracks, random bpm / gogo / bar line / scroll,
    notes on distinct 1/48 slots (rolls end before the next note when
    tja_safe), balloons with counts, optional three routes."""
    r = random.Random(seed)
    div = branched
    t = float(r.randint(500, 3000))
    bpm = float(r.choice([90, 120, 150, 175.5, 200, 233.25]))
    per = []
    for _ in range(r.randint(4, 14)):
        if r.random() < .3:
            bpm = float(r.choice([60, 100, 128, 160, 222.5, 300]))
        bar = 240000.0 / bpm
        nslots = r.choice([48, 48, 36, 24])
        routes = []
        for _b in range(3 if div else 1):
            ns, used = [], set()
            for _k in range(r.randint(0, 10)):
                p = r.randrange(nslots)
                if p in used:
                    continue
                used.add(p)
                ty = r.choice([1, 4, 7, 8, 11, 13, 6, 9, 10, 12] if tja_safe
                              else [1, 2, 3, 4, 5, 7, 8, 11, 13, 6, 9, 10, 12])
                ns.append({"type": ty, "measure": p,
                           "balloonHitCount":
                               r.randint(1, 80) if ty in (10, 12) else -1,
                           "longNoteLength":
                               r.randint(50, 3000) if ty in _ROLLS else -1})
            ns.sort(key=lambda n: n["measure"])
            if tja_safe:
                for i, n in enumerate(ns):
                    if n["longNoteLength"] > 0:
                        nxt = ns[i + 1]["measure"] if i + 1 < len(ns) else nslots
                        gap = (nxt - n["measure"]) / 48.0 * bar
                        n["longNoteLength"] = max(1, int(gap * r.uniform(.2, .9)))
            routes.append(ns)
        per.append((t, bpm, r.random() < .3, int(r.random() < .85), routes))
        t += nslots / 48.0 * bar
    notes, tr = [], []
    for (t, bpm, gogo, line, routes) in per:
        starts = []
        for _copy in range(2):
            for b in range(3):
                rr = routes[b] if div else (routes[0] if b == 0 else [])
                starts.append((len(notes), len(rr)))
                notes.extend(dict(n) for n in rr)
        if div:
            sp = [r.choice([1.0, 1.5, 2.0, 0.5]) for _ in range(3)]
        else:
            sp = [r.choice([1.0, 1.5, 2.0, 0.5])] * 3
        tr.append({"time": float(t), "bpm": bpm, "trackLine": line,
                   "gogoFlag": int(gogo), "_unk": 0, "bunkis": [-1] * 6,
                   "scrollSpeeds": sp + sp,
                   "subtracks": [{"noteIndexSt": s, "noteCount": c,
                                  "pointGain": 0} for s, c in starts]})
    return tja2sht.serialize_sht({
        "noteOffset": 16 + len(tr) * tja2sht.TRACK_NEW_SIZE, "padding": 0,
        "isOld": False, "tracks": tr, "notes": notes, "_gap": b"",
        "_tail": b""})


def _fields(m):
    """Everything that should survive, per track (note order ignored)."""
    _div, rts = sht_export.routes(m)
    out = []
    for t, r in zip(m["tracks"], rts):
        out.append((
            t["bpm"], int(bool(t["gogoFlag"])),
            int(bool(t["trackLine"])), tuple(t["scrollSpeeds"][:3]),
            [sorted((n["type"], n["measure"], n["balloonHitCount"],
                     n["longNoteLength"]) for n in route) for route in r]))
    return out


def _time_err(a, b):
    """Largest track-time difference in ms (the TJA path re-sums float32)."""
    return max((abs(x["time"] - y["time"])
                for x, y in zip(a["tracks"], b["tracks"])), default=0.0)


def _through_gen3(sht):
    g = sht_export.sht_to_gen3(sht, "oni", stars=8)
    return tja2sht.parse_sht(gen3_convert.gen3_to_sht(gen3_convert.parse_gen3(g)))


def _through_tja(sht):
    text = sht_export.sht_to_tja({"oni": sht})
    return tja2sht.parse_sht(tja2sht.convert_tja(text, "oni"))


def test_sht_gen3_sht_is_exact():
    bad = Counter()
    notes = 0
    for seed in range(120):
        a = tja2sht.parse_sht(synth(seed, seed % 2 == 0))
        b = _through_gen3(tja2sht.serialize_sht(a))
        assert _time_err(a, b) == 0.0
        fa, fb = _fields(a), _fields(b)
        notes += sum(len(r) for t in fa for r in t[4])
        bad["tracks"] += (sum(x != y for x, y in zip(fa, fb))
                          + abs(len(fa) - len(fb)))
        bad["bunkis"] += sum(x["bunkis"] != y["bunkis"]
                             for x, y in zip(a["tracks"], b["tracks"]))
    assert notes > 5000
    assert bad["tracks"] == 0 and bad["bunkis"] == 0, bad


def test_sht_tja_sht_keeps_notes_and_roll_lengths():
    bad = 0
    rolls = 0
    for seed in range(120):
        sht = synth(seed, seed % 2 == 0, tja_safe=True)
        a = tja2sht.parse_sht(sht)
        b = _through_tja(sht)
        assert len(a["tracks"]) == len(b["tracks"])
        assert _time_err(a, b) < 0.05
        fa, fb = _fields(a), _fields(b)
        rolls += sum(n[3] > 0 for t in fa for r in t[4] for n in r)
        bad += sum(x != y for x, y in zip(fa, fb))
    assert rolls > 1000
    # roll lengths come back to the ms (was 35 of ~7500 rolls 1 ms off)
    assert bad == 0


def _tja(seed):
    """Random TJA: bpm / meter / scroll / gogo / bar line changes, delays,
    rolls and balloons inside a measure, 1/4 .. 1/48 note grids."""
    r = random.Random(seed)
    bpm = float(r.choice([100, 120, 150, 180, 200, 87.5]))
    body, balloons = [], []
    for _m in range(r.randint(6, 30)):
        if r.random() < .2:
            body.append("#BPMCHANGE %g" % r.choice([90, 110, 135, 160, 210, 77]))
        if r.random() < .2:
            sig = r.choice([(4, 4), (3, 4), (2, 4), (5, 4), (6, 8)])
            body.append("#MEASURE %d/%d" % sig)
        if r.random() < .15:
            body.append("#SCROLL %g" % r.choice([0.5, 1, 1.5, 2]))
        if r.random() < .15:
            body.append(r.choice(["#GOGOSTART", "#GOGOEND", "#BARLINEOFF",
                                  "#BARLINEON"]))
        if r.random() < .08:
            body.append("#DELAY %g" % r.choice([0.25, 0.5, 1.0]))
        n = r.choice([4, 8, 8, 12, 16, 16, 24, 48])
        cells, i = ["0"] * n, 0
        while i < n:
            u = r.random()
            if u < .08 and i + 2 < n:
                ln = r.randint(1, n - i - 1)
                kind = r.choice("5679")
                cells[i], cells[i + ln] = kind, "8"
                if kind in "79":
                    balloons.append(r.randint(3, 40))
                i += ln + 1
                continue
            if u < .55:
                cells[i] = r.choice("1122134")
            i += 1
        body.append("".join(cells) + ",")
    head = ["TITLE:x", "BPM:%g" % bpm, "OFFSET:-2.0", "COURSE:Oni", "LEVEL:8"]
    if balloons:
        head.append("BALLOON:" + ",".join(map(str, balloons)))
    return "\n".join(head + ["", "#START"] + body + ["#END", ""])


def test_tja_sht_gen3_sht_exact_and_tja_path_close():
    n = Counter()
    for seed in range(80):
        sht = tja2sht.convert_tja(_tja(seed), "oni")
        fa = _fields(tja2sht.parse_sht(sht))
        fb = _fields(_through_gen3(sht))
        n["tracks"] += len(fa)
        n["g3_bad"] += sum(x != y for x, y in zip(fa, fb))
        c = _through_tja(sht)
        fc = _fields(c)
        assert _time_err(tja2sht.parse_sht(sht), c) < 0.05
        n["tja_bad"] += (sum(x != y for x, y in zip(fa, fc))
                         if len(fa) == len(fc) else len(fa))
    assert n["tracks"] > 1000
    assert n["g3_bad"] == 0, n
    # TJA path: only two notes sharing one slot (tja2sht nudges one) differ
    assert n["tja_bad"] <= 0.02 * n["tracks"], n


def test_branch_thresholds_survive_tja_with_reachable_points():
    tja = ("TITLE:b\nBPM:120\nOFFSET:-1\nCOURSE:Oni\nLEVEL:8\n\n#START\n"
           "1111,\n1111,\n#BRANCHSTART p,50,100\n#N\n1111,\n#E\n2222,\n#M\n"
           "1212,\n#BRANCHEND\n1111,\n#END\n")
    a = tja2sht.parse_sht(tja2sht.convert_tja(tja, "oni"))
    sht = tja2sht.serialize_sht(a)
    b = _through_tja(sht)
    c = _through_gen3(sht)
    want = [t["bunkis"] for t in a["tracks"]]
    assert any(x != [-1] * 6 for x in want)
    assert want == [t["bunkis"] for t in b["tracks"]]
    assert want == [t["bunkis"] for t in c["tracks"]]


def test_gen3_song_lookups_are_indexed(tmp_path):
    root = tmp_path
    gen3_song._cache[(str(root), "musicinfo.bin")] = {"items": [
        {"id": "a", "uniqueId": 1}, {"id": "b", "uniqueId": 2},
        {"id": "a", "uniqueId": 3}]}
    gen3_song._cache[(str(root), "wordlist.bin")] = {"items": [
        {"key": "song_a", "japaneseText": "A"}, {"key": "song_b"}]}
    assert gen3_song.music_info(root, "a")["uniqueId"] == 1   # first wins
    assert gen3_song.music_info(root, "zz") is None
    assert gen3_song.words(root, "song_a") == "A"
    assert gen3_song.words(root, "song_b") == ""
    assert gen3_song.words(root, "nope") == ""

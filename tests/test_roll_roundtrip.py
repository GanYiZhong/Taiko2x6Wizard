"""Round trip TJA -> sht -> TJA -> sht must keep roll/balloon lengths."""
import random

from taiko2x6.charts import sht_export, tja2sht

_ROLLS = {6, 9, 10, 12}


def random_tja(seed):
    rnd = random.Random(seed)
    lines = ["TITLE:fuzz", "BPM:%d" % rnd.choice([90, 120, 150, 173]),
             "OFFSET:-1.5", "COURSE:Oni", "LEVEL:8", "BALLOON:%s" %
             ",".join("12" for _ in range(60)), "", "#START"]
    for _ in range(rnd.randint(4, 9)):
        if rnd.random() < 0.3:
            lines.append("#BPMCHANGE %d" % rnd.randint(70, 260))
        if rnd.random() < 0.3:
            n, d = rnd.choice([(3, 4), (5, 4), (6, 8), (7, 8), (4, 4)])
            lines.append("#MEASURE %d/%d" % (n, d))
        cells = rnd.choice([8, 12, 16, 24, 32])
        row = ["0"] * cells
        i = rnd.randrange(cells)
        row[i] = rnd.choice("5679")
        # end somewhere later in this or the next measure
        lines.append("".join(row) + ",")
        row2 = ["0"] * cells
        row2[rnd.randrange(cells)] = "8"
        lines.append("".join(row2) + ",")
    lines += ["#END", ""]
    return "\n".join(lines)


def roll_errors(seed):
    """Max |length difference| (ms) over the rolls after one round trip."""
    s1 = tja2sht.convert_tja(random_tja(seed), "oni")
    m1 = tja2sht.parse_sht(s1)
    text = sht_export.sht_to_tja({"oni": m1})
    m2 = tja2sht.parse_sht(tja2sht.convert_tja(text, "oni"))
    a = [n["longNoteLength"] for n in m1["notes"] if n["type"] in _ROLLS]
    b = [n["longNoteLength"] for n in m2["notes"] if n["type"] in _ROLLS]
    assert len(a) == len(b)
    return max((abs(x - y) for x, y in zip(a, b)), default=0), len(text)


def test_roll_end_drift_small():
    worst = 0
    for seed in range(40):
        e, _ = roll_errors(seed)
        worst = max(worst, e)
    assert worst <= 1


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    worst, size = 0, 0
    for s in range(n):
        e, sz = roll_errors(s)
        worst, size = max(worst, e), size + sz
    print("seeds", n, "max roll error ms", worst, "total tja chars", size)

"""The optimised tja2sht / sht_validator must give exactly the old results.

tests/_ref holds the pre-optimisation modules; seeded random charts (long,
dense, BPM/SCROLL/DELAY/MEASURE changes, branches, many courses) are run
through both and compared byte for byte.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "_ref"))
import ref_tja2sht as old  # noqa: E402
import ref_sht_validator as oldv  # noqa: E402
from taiko2x6.charts import sht_validator as newv, tja2sht as new  # noqa: E402


def gen_tja(seed, measures=60, courses=("Easy", "Normal", "Hard", "Oni", "Edit"),
            branch=True):
    r = random.Random(seed)
    out = ["TITLE:Syn %d" % seed, "BPM:%d" % r.randint(90, 240),
           "OFFSET:-%.2f" % r.random(), ""]
    for ci, c in enumerate(courses):
        out += ["COURSE:" + c, "LEVEL:%d" % (ci + 3),
                "BALLOON:" + ",".join(str(r.randint(3, 30)) for _ in range(measures))]
        out.append("#START")
        orl = [False]

        def meas():
            n = r.choice([4, 8, 12, 16, 16, 24, 32, 48, 7, 5, 64, 96])
            s = []
            for _ in range(n):
                x = r.random()
                if orl[0]:
                    if x < .2:
                        s.append("8")
                        orl[0] = False
                    else:
                        s.append("0")
                elif x < .5:
                    s.append("0")
                elif x < .85:
                    s.append(r.choice("1234"))
                elif x < .9:
                    s.append(r.choice("AB x"))
                else:
                    s.append(r.choice("5679"))
                    orl[0] = True
            return "".join(s) + ","
        i = 0
        while i < measures:
            x = r.random()
            if x < .05:
                out.append("#BPMCHANGE %d" % r.randint(80, 260))
            elif x < .10:
                out.append("#SCROLL %.2f" % (r.random() * 3))
            elif x < .13:
                out.append("#GOGOSTART")
            elif x < .16:
                out.append("#GOGOEND")
            elif x < .19:
                out.append("#MEASURE %d/4" % r.randint(2, 6))
            elif x < .21:
                out.append("#DELAY 0.%d" % r.randint(1, 9))
            elif x < .22:
                out.append("#SECTION")
            if branch and x > .9:
                out.append("#BRANCHSTART %s,%d,%d" % (r.choice("prs"),
                                                      r.randint(50, 90),
                                                      r.randint(90, 100)))
                k = r.randint(1, 4)
                for tag in "NEM":
                    out.append("#" + tag)
                    for _ in range(k):
                        out.append(meas() if not orl[0] else "0000,")
                        if r.random() < .2:
                            out.append("#SCROLL 1.5")
                    orl[0] = False
                out.append("#BRANCHEND")
                i += k
                continue
            if r.random() < .08 and not orl[0]:
                out += ["10", "#BPMCHANGE %d" % r.randint(80, 260), "2020,"]
            else:
                out.append(meas())
            i += 1
        out += ["#END", ""]
    return "\n".join(out)


SEEDS = range(40)


def _old_rules(issues):
    """Issues from rules the reference validator already had (the structure
    rules added later are tested in test_sht_validator_rules.py)."""
    return [str(i) for i in issues if i.rule not in newv.STRUCTURE_RULES]


@pytest.mark.parametrize("seed", SEEDS)
def test_convert_and_validate_match_old(seed):
    txt = gen_tja(seed, measures=10 + seed * 3, branch=seed % 4 != 0)
    assert new.parse_tja(txt) == old.parse_tja(txt)
    got = new.tja_to_all_charts(txt)
    assert got == old.tja_to_all_charts(txt)
    for diff in ("easy", "oni"):
        assert (new.convert_tja(txt, diff, branches=False)
                == old.convert_tja(txt, diff, branches=False))
    for diff, data in got.items():
        d = diff.split("_")[0]
        assert _old_rules(newv.validate_sht(data, d)) == \
            [str(i) for i in oldv.validate_sht(data, d)]
        m_new, m_old = new.parse_sht(data), old.parse_sht(data)
        assert m_new == m_old
        assert new.serialize_sht(m_new) == old.serialize_sht(m_old) == data


def test_sht_variants_and_corrupt_validate_match_old():
    txt = gen_tja(7, measures=30)
    data = old.tja_to_all_charts(txt)["oni"]
    model = old.parse_sht(data)
    # old 140-byte records, odd gap/tail, short bunkis/subtracks
    om = dict(model, isOld=True, noteOffset=None,
              _gap=b"\x01\x02\x03", _tail=b"tail")
    om["tracks"] = [dict(t, _unk=5, bunkis=t["bunkis"][:3],
                         subtracks=t["subtracks"][:4]) for t in model["tracks"]]
    assert new.serialize_sht(om) == old.serialize_sht(om)
    om2 = dict(om, _gap=b"", _tail=b"tail")
    assert new.serialize_sht(om2) == old.serialize_sht(om2)
    assert new.parse_sht(new.serialize_sht(om2)) == old.parse_sht(old.serialize_sht(om2))
    # stored noteOffset beyond the table (zero padding)
    pm = dict(model, noteOffset=model["noteOffset"] + 12, _gap=b"")
    assert new.serialize_sht(pm) == old.serialize_sht(pm)
    rnd = random.Random(1)
    for _ in range(60):
        b = bytearray(data)
        for _ in range(rnd.randint(1, 6)):
            b[rnd.randrange(len(b))] = rnd.randrange(256)
        if rnd.random() < .3:
            b = b[:rnd.randrange(len(b))]
        b = bytes(b)
        assert _old_rules(newv.validate_sht(b, "hard")) == \
            [str(i) for i in oldv.validate_sht(b, "hard")]

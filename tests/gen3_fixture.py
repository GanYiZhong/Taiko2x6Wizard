"""Synthetic Gen3 data folder builder (no real game data) for tests/benchmarks."""
import json
import pathlib

from taiko2x6.charts import gen3_convert, sht_export, tja2sht

TJA = """TITLE:t
BPM:150
OFFSET:0
COURSE:Oni
LEVEL:8
BALLOON:12

#START
1020,
1111,
7008,
1020,
#BPMCHANGE 180
1111,
#END
"""

_SUF = {"e": "easy", "n": "normal", "h": "hard", "m": "oni", "x": "ura"}
_STAR = {"e": "starEasy", "n": "starNormal", "h": "starHard",
         "m": "starMania", "x": "starUra"}


def write_table(path, obj, key=gen3_convert.DATATABLE_KEY):
    data = json.dumps(obj).encode("utf-8")
    pathlib.Path(path).write_bytes(
        sht_export.encrypt_fumen(data, key=key, iv=bytes(16)))


def build_root(root, n_songs=3, suffixes="enhm"):
    """Create <root>/fumen/<sid>/*.bin + datatable/*.bin. Returns [sid]."""
    root = pathlib.Path(root)
    (root / "datatable").mkdir(parents=True, exist_ok=True)
    sht = tja2sht.convert_tja(TJA, "oni")
    items, words, sids = [], [], []
    for i in range(n_songs):
        sid = "song%04d" % i
        sids.append(sid)
        d = root / "fumen" / sid
        d.mkdir(parents=True, exist_ok=True)
        info = {"id": sid, "uniqueId": i, "genreNo": i % 7}
        for s in suffixes:
            plain = sht_export.sht_to_gen3(sht, _SUF[s], stars=5)
            (d / ("%s_%s.bin" % (sid, s))).write_bytes(
                sht_export.encrypt_fumen(plain, iv=bytes(16)))
            info[_STAR[s]] = 3 + i % 6
        items.append(info)
        words.append({"key": "song_%s" % sid, "japaneseText": "Title %d" % i})
    write_table(root / "datatable" / "musicinfo.bin", {"items": items})
    write_table(root / "datatable" / "wordlist.bin", {"items": words})
    return sids

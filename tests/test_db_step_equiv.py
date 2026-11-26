"""One-model musicinfo fix pass == the old parse/serialize chain (600 songs)."""
import random

from taiko2x6.bineditors import bineditor_musicinfo as MI
from taiko2x6.songs import omnimix_maker as O

NCOLS = MI.SEC0_NCOLS_T14
NUL = bytes(1)


def build_t14_musicinfo(n_songs=600, seed=1):
    """Stock-like T14 musicinfo: folder headers + genre-scattered song rows."""
    rnd = random.Random(seed)
    pool = bytearray()

    def s(txt):
        o = len(pool)
        pool.extend(txt.encode() + NUL)
        return o
    headers = [9, 7, 10, 11, 12, 13, 14, 15]
    rows = [(s("sel%d" % h), h) for h in headers]
    songs = [(s("song%04d" % i), rnd.choice([0, 1, 2, 3, 4, 5, 6, 8]))
             for i in range(n_songs)]
    rnd.shuffle(songs)
    rows += songs
    sec0 = []
    for i, (o, g) in enumerate(rows):
        r = [0] * NCOLS
        r[0], r[2] = o, g
        r[10] = rnd.randrange(len(rows)) if rnd.random() < .05 else -1
        r[13] = r[14] = i
        r[15] = r[16] = len(rows) - 1 - i
        r[12] = 1000
        for k in range(22, 26):
            r[k] = 1000
        sec0.append(r)
    sec1 = [rnd.randrange(len(rows)) for _ in range(10)]
    sec2 = [(1, 2), (3, 4)]
    sec3 = [s("GALLERY_%d" % i) for i in range(8)]
    sec4 = [(s("GROUP_%d" % i), rnd.randrange(len(rows)), 3) for i in range(12)]
    sec5 = [(s("CAT_%d" % i), 0, 1) for i in range(5)]
    sec6 = [rnd.randrange(len(rows)) for _ in range(12)]
    sec7 = [rnd.randrange(len(rows)) for _ in range(20)]
    m = MI.Model(raw=b"", counts=[len(sec0), len(sec1), len(sec2), len(sec3),
                                  len(sec4), len(sec5), len(sec6), len(sec7)],
                 offs=[], sec0=sec0, sec1=sec1, sec2=sec2, sec3_ptrs=sec3,
                 sec4=sec4, sec5=sec5, pool=bytes(pool), gaps={},
                 variant="t14", sec0_recsz=MI.SEC0_RECSZ_T14, sec0_ncols=NCOLS,
                 header_size=MI.HEADER_SIZE_T14, sec6=sec6, sec7=sec7)
    return MI.serialize(m)


# ---- reference: the previous implementations, verbatim -------------------- #
def _ref_group(m, order_by_sid=None):
    if getattr(m, "variant", "") != "t14" or not m.sec0:
        return
    rows = m.sec0
    headers = [i for i, r in enumerate(rows) if r[2] not in O.SONG_GENRES]
    order = []
    for r in rows:
        g = r[2]
        if g in O.SONG_GENRES and g not in order:
            order.append(g)
    key = {}
    if order_by_sid:
        pool = m.pool
        last = (1 << 30,)
        for i, r in enumerate(rows):
            sid = pool[r[0]:pool.find(NUL, r[0])].decode("ascii", "replace")
            key[i] = order_by_sid.get(sid, last) + (i,)

    def block(g):
        return sorted((i for i, r in enumerate(rows) if r[2] == g),
                      key=lambda i: key.get(i, (i,)))
    new, done = [], set()
    lone = [h for h in headers if O.HEADER_GENRE.get(rows[h][2]) is None]
    new += lone
    for h in headers:
        g = O.HEADER_GENRE.get(rows[h][2])
        if g is None:
            continue
        new.append(h)
        if g not in done:
            new += block(g)
            done.add(g)
    for g in order:
        if g not in done:
            new += block(g)
            done.add(g)
    if new == list(range(len(rows))):
        return
    old2new = {o: n for n, o in enumerate(new)}

    def remap(i):
        return old2new.get(i, i) if i >= 0 else i
    out = []
    for o in new:
        r = list(rows[o])
        r[10] = remap(r[10])
        if r[13] == o:
            r[13] = old2new[o]
        if r[14] == o:
            r[14] = old2new[o]
        out.append(r)
    m.sec0[:] = out
    m.sec1[:] = [remap(i) for i in m.sec1]
    m.sec4[:] = [(remap(a),) + tuple(rest) for a, *rest in m.sec4]
    if m.sec6:
        m.sec6[:] = [remap(i) for i in m.sec6]
    if m.sec7:
        m.sec7[:] = [remap(i) for i in m.sec7]


def _ref_jumps(m):
    sec6 = getattr(m, "sec6", None)
    if not sec6:
        return
    rows = m.sec0
    for genre, slot in O.GENRE_JUMP_SLOT.items():
        if slot >= len(sec6):
            continue
        cur = sec6[slot]
        if 0 <= cur < len(rows) and rows[cur][2] == genre:
            continue
        first = next((i for i, r in enumerate(rows) if r[2] == genre), None)
        if first is not None:
            sec6[slot] = first


def _ref_genres(raw, genres, order=None):
    m = MI.parse(raw)
    pool = m.pool
    for rec in m.sec0:
        o = rec[0]
        sid = pool[o:pool.find(NUL, o)].decode("ascii", "replace")
        if sid in genres:
            rec[2] = int(genres[sid])
    _ref_group(m, order)
    _ref_jumps(m)
    return MI.serialize(m)


def _ref_previews(raw, previews):
    if not previews:
        return raw
    m = MI.parse(raw)
    pool = m.pool
    for rec in m.sec0:
        o = rec[0]
        sid = pool[o:pool.find(NUL, o)].decode("ascii", "replace")
        st = previews.get(sid)
        if st is None or st <= 0:
            continue
        rec[12] = st
        rec[22:26] = [st, st + 3000, st, st + 5000]
    return MI.serialize(m)


def _plan(seed, n=600):
    rnd = random.Random(seed)
    ids = ["song%04d" % i for i in range(n)]
    genres = {s: rnd.choice([0, 1, 2, 3, 4, 5, 6, 8])
              for s in rnd.sample(ids, rnd.choice([0, 5, 80]))}
    order = ({s: (rnd.randrange(4), rnd.randrange(900))
              for s in rnd.sample(ids, min(300, n))} if seed % 2 else {})
    prev = {s: rnd.choice([0, -5, 12345, 40000])
            for s in rnd.sample(ids, rnd.choice([0, 7, 200]))}
    return genres, order, prev


def test_fixes_pass_matches_chained_old_path():
    for seed in range(8):
        raw = build_t14_musicinfo(600, seed)
        genres, order, prev = _plan(seed)
        old = _ref_previews(_ref_genres(raw, genres, order), prev)
        assert O.set_musicinfo_fixes(raw, genres, order, prev) == old
        assert O.set_musicinfo_previews(
            O.set_musicinfo_genres(raw, genres, order), prev) == old


def test_group_and_jumps_match_reference():
    for seed in range(5):
        raw = build_t14_musicinfo(300, seed + 20)
        _, order, _ = _plan(seed, 300)
        a, b = MI.parse(raw), MI.parse(raw)
        O.group_rows_by_genre(a, order)
        O.fix_genre_jumps(a)
        _ref_group(b, order)
        _ref_jumps(b)
        assert MI.serialize(a) == MI.serialize(b)


def test_musicinfo_unedited_roundtrip():
    raw = build_t14_musicinfo(600, 3)
    assert MI.serialize(MI.parse(raw)) == raw

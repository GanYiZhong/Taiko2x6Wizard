"""song_tables: relocating Taiko 14's per-song record tables (synthetic image)."""
import hashlib
import struct

import pytest

from taiko2x6.exe import song_tables as S

SIZE = 0x2A8600 + 0x1000          # covers every site and the cave


def _w(buf, va):
    return struct.unpack_from("<I", buf, va - S.DELTA)[0]


def _put(buf, va, word):
    struct.pack_into("<I", buf, va - S.DELTA, word)


def _stock(monkeypatch):
    """An image holding the stock instruction at every site the patch reads."""
    buf = bytearray(SIZE)
    for va, w in S.IMM_SITES + S.BOUND_SITES:
        _put(buf, va, w)
    for (cva, cw), (bva, bw) in (S.SORT_VECTOR, S.RANK_VECTOR):
        _put(buf, cva, cw)
        _put(buf, bva, bw)
    for va, w, _ in S.RANK_SITES:
        _put(buf, va, w)
    lo, hi = S.RANK_FUNC
    _put(buf, lo, 0x27BDFC50)                 # addiu sp,sp,-0x3B0
    _put(buf, lo + 4, 0xFFBF03A0)             # sd ra,0x3A0(sp)
    _put(buf, lo + 8, 0x27B50350)             # addiu s5,sp,0x350
    _put(buf, lo + 12, 0xAFAA0360)            # sw t2,0x360(sp)
    _put(buf, lo + 16, 0x8FAC0040)            # lw t4,0x40(sp)  (array: untouched)
    _put(buf, hi - 4, 0x27BD03B0)             # addiu sp,sp,0x3B0
    for va, w, _ in S.HOOKS:
        _put(buf, va, w)
    _put(buf, S.ALLOC_VA, 0x24040000 | (0x4A90 + 4 * 331))
    _put(buf, S.TEX_CAP_VA, 0x24050000 | S.TEX_CAP_STOCK)
    cave = bytes(range(256)) * 4
    buf[S.CAVE_VA - S.DELTA:S.CAVE_VA - S.DELTA + S.CAVE_LEN] = cave[:S.CAVE_LEN]
    monkeypatch.setattr(S, "CAVE_SHA1", hashlib.sha1(cave[:S.CAVE_LEN]).hexdigest())
    return bytes(buf)


def test_layout_fits():
    lay = S.layout(S.MAX_SONGS)
    assert lay["alloc"] <= 0xFFFF
    assert max(stock & 0xFFFF for _, stock in S.IMM_SITES) + lay["delta"] < 0x8000


def test_patch_records(monkeypatch):
    dec = _stock(monkeypatch)
    assert S.current_delta(dec) == 0 and not S.is_patched(dec)
    out = S.patch_records(dec, 331, log=lambda s: None)
    lay = S.layout(331)
    assert S.current_delta(out) == lay["delta"]
    for va, stock in S.IMM_SITES:
        assert _w(out, va) == (stock & 0xFFFF0000) | ((stock & 0xFFFF) + lay["delta"])
    for va, stock in S.BOUND_SITES:
        assert _w(out, va) & 0xFFFF == 331
    assert _w(out, S.SORT_VECTOR[0][0]) & 0xFFFF == 331
    assert _w(out, S.SORT_VECTOR[1][0]) & 0xFFFF == 12 * 331
    assert _w(out, S.RANK_VECTOR[0][0]) & 0xFFFF == S.RANK_IDS
    assert _w(out, S.ALLOC_VA) == 0x34040000 | lay["alloc"]
    assert S.alloc_owned(out)
    lo, hi = S.RANK_FUNC
    assert _w(out, lo) == 0x27BDFB50 and _w(out, hi - 4) == 0x27BD04B0
    assert _w(out, lo + 4) == 0xFFBF04A0 and _w(out, lo + 8) == 0x27B50450
    assert _w(out, lo + 12) == 0xAFAA0460 and _w(out, lo + 16) == 0x8FAC0040
    # hooks call into the cave
    for va, _stock_w, _label in S.HOOKS:
        target = (_w(out, va) & 0x3FFFFFF) << 2
        assert S.CAVE_VA <= target < S.CAVE_VA + S.CAVE_LEN
    # idempotent, and re-runnable with another ceiling
    assert S.patch_records(out, 331, log=lambda s: None) == out
    bigger = S.patch_records(out, 400, log=lambda s: None)
    assert S.current_delta(bigger) == S.layout(400)["delta"]
    assert _w(bigger, S.BOUND_SITES[0][0]) & 0xFFFF == 400
    assert _w(bigger, S.TEX_CAP_VA) == 0x24050000 | S.tex_capacity(400)


def test_texture_capacity():
    # 3 select textures per song plus the rest of the game must fit
    assert S.tex_capacity(331) == S.TEX_CAP_STOCK
    for n in (440, 604, S.MAX_SONGS):
        cap = S.tex_capacity(n)
        assert cap >= 3 * n + S.TEX_CAP_SPARE and cap < 0x8000 and cap % 2 == 0


def test_keeps_larger_vector_capacity(monkeypatch):
    dec = bytearray(_stock(monkeypatch))
    _put(dec, S.SORT_VECTOR[0][0], 0x240F0000 | 371)
    out = S.patch_records(bytes(dec), 331, log=lambda s: None)
    assert _w(out, S.SORT_VECTOR[0][0]) & 0xFFFF == 371
    assert _w(out, S.SORT_VECTOR[1][0]) & 0xFFFF == 12 * 371


def test_refuses_other_builds(monkeypatch):
    dec = bytearray(_stock(monkeypatch))
    _put(dec, S.IMM_SITES[3][0], 0)          # a site that is not the instruction
    with pytest.raises(S.SongTablesError):
        S.patch_records(bytes(dec), 331, log=lambda s: None)
    with pytest.raises(S.SongTablesError):
        S.patch_records(_stock(monkeypatch), 210, log=lambda s: None)
    with pytest.raises(S.SongTablesError):
        S.patch_records(_stock(monkeypatch), S.MAX_SONGS + 1, log=lambda s: None)


def test_cave_code_shape():
    code, labels = S._cave(331, S.layout(331)["rec"])
    assert len(code) <= S.CAVE_LEN and len(code) % 4 == 0
    assert set(labels) == {"load", "save", "hist"}
    words = struct.unpack("<%dI" % (len(code) // 4), code)
    jal = lambda f: 0x0C000000 | (f >> 2)                       # noqa: E731
    assert jal(S.F_LOAD) in words and jal(S.F_MEMSET) in words
    assert jal(S.F_MEMCPY) in words and jal(S.F_HIST) in words
    assert (0x08000000 | (S.F_SAVE >> 2)) in words              # save tail-calls

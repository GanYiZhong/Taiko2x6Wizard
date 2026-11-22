"""Song-select countdown freeze (synthetic image)."""
import hashlib
import struct

import pytest

from taiko2x6.exe import taiko_exe as T

SIZE = 0x3A8000 - T.DELTA


def _img(monkeypatch, cave=None):
    buf = bytearray(SIZE)
    struct.pack_into("<I", buf, T._SELECT_STATE - T.DELTA, T._SELECT_UPDATE)
    buf[T._COUNTER_STR - T.DELTA:T._COUNTER_STR - T.DELTA + 8] = b"counter\0"
    cave = cave if cave is not None else bytes(i % 251 for i in range(T._FREEZE_LEN))
    buf[T._FREEZE_VA - T.DELTA:T._FREEZE_VA - T.DELTA + T._FREEZE_LEN] = cave
    monkeypatch.setattr(T, "_FREEZE_STOCK_SHA1", hashlib.sha1(cave).hexdigest())
    return bytes(buf)


def test_freeze(monkeypatch):
    dec = _img(monkeypatch)
    assert not T.is_select_timer_frozen(dec)
    out = T.patch_freeze_select_timer(dec, log=lambda s: None)
    assert T.is_select_timer_frozen(out)
    code = T._freeze_code()
    assert out[T._FREEZE_VA - T.DELTA:][:len(code)] == code
    # ends in `j sub_167A98` + delay slot
    last_j = struct.unpack_from("<I", code, len(code) - 8)[0]
    assert last_j == 0x08000000 | (T._SELECT_UPDATE >> 2)
    assert T.patch_freeze_select_timer(out, log=lambda s: None) == out


def test_freeze_after_record_patch_zeroed_cave(monkeypatch):
    dec = _img(monkeypatch, cave=bytes(T._FREEZE_LEN))
    monkeypatch.setattr(T, "_FREEZE_STOCK_SHA1", "x")
    assert T.is_select_timer_frozen(T.patch_freeze_select_timer(dec, log=lambda s: None))


def test_freeze_refuses_unknown(monkeypatch):
    buf = bytearray(_img(monkeypatch))
    struct.pack_into("<I", buf, T._SELECT_STATE - T.DELTA, 0x12345678)
    with pytest.raises(T.TaikoExeError):
        T.patch_freeze_select_timer(bytes(buf), log=lambda s: None)

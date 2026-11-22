"""Boot / select patches as on-off switches (synthetic image)."""
import hashlib
import struct

from taiko2x6.exe import taiko_exe as T

SIZE = 0x3A8000 - T.DELTA
QUIET = dict(log=lambda s: None)


def _stock(monkeypatch):
    buf = bytearray(SIZE)
    put = lambda va, w: struct.pack_into("<I", buf, va - T.DELTA, w)
    struct.pack_into("<II", buf, T._NOTICE_INIT - T.DELTA, *T._NOTICE_GUARD)
    put(T._COPYRIGHT_SLOT, T._COPYRIGHT_ORIG)
    for va, word in T._OLD_COPYRIGHT:
        put(va, word)
    for va, imm in T._DIFF_TIMER_SITES.items():
        put(va, 0x284F0000 | imm)
    put(T._SELECT_STATE, T._SELECT_UPDATE)
    buf[T._COUNTER_STR - T.DELTA:T._COUNTER_STR - T.DELTA + 8] = b"counter\0"
    cave = bytes(i % 251 for i in range(T._FREEZE_LEN))
    buf[T._FREEZE_VA - T.DELTA:T._FREEZE_VA - T.DELTA + T._FREEZE_LEN] = cave
    monkeypatch.setattr(T, "_FREEZE_STOCK_SHA1", hashlib.sha1(cave).hexdigest())
    return bytes(buf)


def test_all_off_on_stock(monkeypatch):
    assert not any(T.boot_patch_state(_stock(monkeypatch)).values())


def test_each_switch_round_trips(monkeypatch):
    stock = _stock(monkeypatch)
    for key in T.BOOT_PATCHES:
        on = T.set_boot_patches(stock, {key: True}, **QUIET)
        state = T.boot_patch_state(on)
        assert state[key] and sum(state.values()) == 1, key
        off = T.set_boot_patches(on, {key: False}, **QUIET)
        if key == 'freeze_select_timer':      # the wrapper code stays in its cave
            assert not T.boot_patch_state(off)[key]
        else:
            assert off == stock, key


def test_all_on_then_some_off(monkeypatch):
    stock = _stock(monkeypatch)
    out = T.set_boot_patches(stock, {k: True for k in T.BOOT_PATCHES}, **QUIET)
    assert all(T.boot_patch_state(out).values())
    out = T.set_boot_patches(out, {'skip_copyright': False, 'disable_diff_timer': False},
                             **QUIET)
    assert T.boot_patch_state(out) == {'skip_notice': True, 'skip_copyright': False,
                                       'freeze_select_timer': True,
                                       'disable_diff_timer': False}


def test_old_credits_patch_reads_off_and_is_undone(monkeypatch):
    buf = bytearray(_stock(monkeypatch))
    struct.pack_into("<I", buf, 0x00146CDC - T.DELTA, 0)     # the crashing NOP
    assert not T.boot_patch_state(bytes(buf))['skip_copyright']
    out = T.set_boot_patches(bytes(buf), {'skip_copyright': True}, **QUIET)
    assert struct.unpack_from("<I", out, 0x00146CDC - T.DELTA)[0] == 0x54400035
    assert T.boot_patch_state(out)['skip_copyright']

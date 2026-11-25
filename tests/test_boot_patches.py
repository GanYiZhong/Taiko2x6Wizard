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
    monkeypatch.setattr(T, "_ENTRY_STOCK", cave[-T._ENTRY_LEN:])
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
        if key in ('freeze_select_timer', 'skip_copyright'):   # code stays in its cave
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


def _entry_flag_stub(out):
    o = T._ENTRY_VA - T.DELTA
    return out[o:o + T._ENTRY_LEN]


def test_credits_skip_keeps_coin_entry_on(monkeypatch):
    # the slot must go through the stub that re-enables coin entry
    # (dword_3A6638->+0xA3C = 1), not straight to LoopAttract
    out = T.set_boot_patches(_stock(monkeypatch), {'skip_copyright': True}, **QUIET)
    assert struct.unpack_from("<I", out, T._COPYRIGHT_SLOT - T.DELTA)[0] == T._ENTRY_VA
    words = struct.unpack("<6I", _entry_flag_stub(out)[:24])
    assert words == (0x3C08003A, 0x8D086638, 0x24090001, 0xAD090A3C,
                     0x08000000 | (T._COPYRIGHT_NEW >> 2), 0)


def test_old_direct_jump_is_upgraded(monkeypatch):
    buf = bytearray(_stock(monkeypatch))
    struct.pack_into("<I", buf, T._COPYRIGHT_SLOT - T.DELTA, T._COPYRIGHT_NEW)
    old = bytes(buf)
    assert T.boot_patch_state(old)['skip_copyright'] and T.copyright_needs_upgrade(old)
    # any boot-patch write fixes it, unless it turns the skip off
    out = T.set_boot_patches(old, {'disable_diff_timer': True}, **QUIET)
    assert not T.copyright_needs_upgrade(out) and T.boot_patch_state(out)['skip_copyright']
    off = T.set_boot_patches(old, {'skip_copyright': False}, **QUIET)
    assert struct.unpack_from("<I", off, T._COPYRIGHT_SLOT - T.DELTA)[0] == T._COPYRIGHT_ORIG


def test_freeze_and_credits_share_the_cave(monkeypatch):
    stock = _stock(monkeypatch)
    for order in (('skip_copyright', 'freeze_select_timer'),
                  ('freeze_select_timer', 'skip_copyright')):
        out = stock
        for key in order:
            out = T.set_boot_patches(out, {key: True}, **QUIET)
        assert T.boot_patch_state(out)['skip_copyright']
        assert T.boot_patch_state(out)['freeze_select_timer']
        assert _entry_flag_stub(out) == T._entry_code()
        o = T._FREEZE_VA - T.DELTA
        assert out[o:o + len(T._freeze_code())] == T._freeze_code()


def test_credits_stub_refuses_unknown_bytes(monkeypatch):
    buf = bytearray(_stock(monkeypatch))
    o = T._ENTRY_VA - T.DELTA
    buf[o:o + 4] = bytes([0x11, 0x22, 0x33, 0x44])
    import pytest
    with pytest.raises(T.TaikoExeError):
        T.patch_skip_copyright(bytes(buf), **QUIET)

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
    put(T._PLAYED_SITE, T._PLAYED_ORIG)
    from taiko2x6.exe import select_folders as SF
    for va, word in {**SF.ORIG, **SF.OLD_SITES}.items():
        put(va, word)
    struct.pack_into("<3I", buf, SF.SITE_CMD - T.DELTA, *SF.ORIG_CMD)
    fcave = bytes((i * 7) % 253 for i in range(SF.CAVE_LEN))
    buf[SF.CAVE_VA - T.DELTA:SF.CAVE_VA - T.DELTA + SF.CAVE_LEN] = fcave
    monkeypatch.setattr(SF, "CAVE_STOCK_SHA1", hashlib.sha1(fcave).hexdigest())
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
        if key in ('freeze_select_timer', 'skip_copyright',
                   'category_folders'):                         # code stays in its cave
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
                                       'disable_diff_timer': False,
                                       'category_folders': True,
                                       'show_played_songs': True}


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


def _jal(va):
    return 0x0C000000 | ((va >> 2) & 0x3FFFFFF)


def test_category_folders_hook_filter_and_pick(monkeypatch):
    from taiko2x6.exe import select_folders as SF
    stock = _stock(monkeypatch)
    out = T.set_boot_patches(stock, {'category_folders': True}, **QUIET)
    _cave, stubs = SF.build()
    word = lambda b, va: struct.unpack_from("<I", b, va - T.DELTA)[0]
    assert word(out, SF.SITE_FILTER) == _jal(stubs['filter'])
    assert word(out, SF.SITE_PICK) == _jal(stubs['pick'])
    assert struct.unpack_from("<3I", out, SF.SITE_CMD - T.DELTA) == (
        _jal(stubs['cmd']), 0, 0x00400008)                 # jal cmd; nop; jr v0
    o = SF.CAVE_VA - T.DELTA
    assert word(out, SF.CAVE_VA) == 0                     # no folder open
    cards = out[o + 8:o + 24]
    # only the cards open a folder: the one each heads
    assert {g: cards[g] - 1 for g in range(16) if cards[g]} == SF.HEADER_GENRE
    off = T.set_boot_patches(out, {'category_folders': False}, **QUIET)
    for va, orig in SF.ORIG.items():
        assert word(off, va) == orig
    assert struct.unpack_from("<3I", off, SF.SITE_CMD - T.DELTA) == SF.ORIG_CMD
    # turning it back on finds its own stubs in the cave
    again = T.set_boot_patches(off, {'category_folders': True}, **QUIET)
    assert again == out


def test_category_folders_upgrade_early_builds(monkeypatch):
    from taiko2x6.exe import select_folders as SF
    buf = bytearray(_stock(monkeypatch))
    put = lambda va, w: struct.pack_into("<I", buf, va - T.DELTA, w)
    # the first build: filter and settle hooks into the DMA-buffer stub
    put(SF.SITE_FILTER, _jal(SF.OLD_CAVE_VA + 0x20))
    put(SF.SITE_SETTLE, _jal(SF.OLD_CAVE_VA + 0x80))
    o = SF.OLD_CAVE_VA - T.DELTA
    buf[o + 0x20:o + 0x24] = bytes.fromhex('e0ffbd27')
    buf[o + 0x40] = 0x99
    assert T.folders_need_upgrade(bytes(buf))
    out = T.set_boot_patches(bytes(buf), {}, **QUIET)          # fixed unasked
    assert T.boot_patch_state(out)['category_folders']
    assert not T.folders_need_upgrade(out)
    for va, word in SF.OLD_SITES.items():
        assert struct.unpack_from("<I", out, va - T.DELTA)[0] == word
    assert not any(out[o:o + SF.OLD_CAVE_LEN])
    # turning an early build off cleans it up the same way
    off = T.set_boot_patches(bytes(buf), {'category_folders': False}, **QUIET)
    assert not T.boot_patch_state(off)['category_folders']
    assert struct.unpack_from("<I", off, SF.SITE_FILTER - T.DELTA)[0] == SF.ORIG[SF.SITE_FILTER]
    assert not any(off[o:o + SF.OLD_CAVE_LEN])


def test_category_folders_refuse_a_used_cave(monkeypatch):
    import pytest
    from taiko2x6.exe import select_folders as SF
    buf = bytearray(_stock(monkeypatch))
    buf[SF.CAVE_VA - T.DELTA + 0x40] ^= 0xFF
    with pytest.raises(T.TaikoExeError):
        T.patch_folders(bytes(buf), **QUIET)


def test_category_folders_refuse_an_unknown_site(monkeypatch):
    import pytest
    from taiko2x6.exe import select_folders as SF
    for va in (SF.SITE_PICK, SF.SITE_CMD):
        buf = bytearray(_stock(monkeypatch))
        struct.pack_into("<I", buf, va - T.DELTA, 0x12345678)
        with pytest.raises(T.TaikoExeError):
            T.patch_folders(bytes(buf), **QUIET)


def test_show_played_songs_only_flips_the_branch(monkeypatch):
    stock = _stock(monkeypatch)
    out = T.set_boot_patches(stock, {'show_played_songs': True}, **QUIET)
    diff = [i for i in range(0, len(stock), 4) if stock[i:i + 4] != out[i:i + 4]]
    assert diff == [T._PLAYED_SITE - T.DELTA]
    assert struct.unpack_from("<I", out, diff[0])[0] == 0x54000015   # bnel $zero,$zero
    buf = bytearray(stock)
    struct.pack_into("<I", buf, T._PLAYED_SITE - T.DELTA, 0x12345678)
    import pytest
    with pytest.raises(T.TaikoExeError):
        T.patch_show_played(bytes(buf), **QUIET)


def test_category_folders_back_on_over_an_older_build(monkeypatch):
    from taiko2x6.exe import select_folders as SF
    stock = _stock(monkeypatch)
    out = bytearray(T.set_boot_patches(stock, {'category_folders': True}, **QUIET))
    off = bytearray(T.set_boot_patches(bytes(out), {'category_folders': False}, **QUIET))
    off[SF.CAVE_VA - T.DELTA + 0x40] ^= 0xFF        # stubs of another build
    again = T.set_boot_patches(bytes(off), {'category_folders': True}, **QUIET)
    assert T.boot_patch_state(again)['category_folders']
    assert again[SF.CAVE_VA - T.DELTA:][:0x100] == out[SF.CAVE_VA - T.DELTA:][:0x100]

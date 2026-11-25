"""Song select as category folders (Taiko 14 `taiko`).

Stock song select is one long carousel over every musicinfo row, with the
folder header rows (genre 7, 9..15) as divider cards. With 600+ songs that
is a lot of drumming. With this patch the carousel shows only the cards,
plus the songs of the one folder that is open. Don on a card opens it (and
closes the one that was open); the cursor lands on its first song. Don on
the open folder's own card closes it again, the cursor staying put. Ka moves
over the open folder's songs and the cards as usual; moving onto another
card does not open it. Both ka at once closes the open folder from
anywhere inside it, the cursor going back to its card. The open folder is remembered, so after a song the
select comes back into it.

How the stock code works (RE'd on TK14): there is no prebuilt list.
sub_138390 moves the cursor over the musicinfo rows and asks the row filter
sub_138688 at every step whether to skip the row. The filter reads the row's
genre (sub_1078C8 = row[2]) and hides it when bit `genre` of a per-mode mask
in $s0 is set. The mode (ctx->+0x6C, ctx = dword_3A6638) is 0 or 1 in the
song list (song_tables makes every divider card show in both); 2 and 3 are
the masks the select starts with before the first jump. ctx->+0x3E8 is the
cursor row.

Picking a card: the select's Flash asks isPackedGenre (genre 7 or 9..15)
and then calls the native setNamcoOriginalInit, which sets ctx->+0x70. The
per-frame update (sub_167A98) then calls sub_175C20, which reads the cursor
row's genre, sets the mode to 0/1 and moves the cursor to the folder's first
row (musicinfo SEC6 via sub_108218, then sub_138390 with step 0).

The patch hooks two `jal sub_1078C8` genre reads and the both-ka command:
  * 0x175C60 in sub_175C20 (the card pick): a card's folder becomes the
    open folder; the open folder's own card closes it and returns genre 16,
    which the function's switch skips, so the cursor stays on the card.
  * 0x1386E0 in the row filter: in modes 0..3 the mask becomes "every
    song genre except the open folder's" (all of them while none is open).
    Divider genres are never in the mask, so the cards always show.
    Modes 2/3 count too: the select comes back in one of them after a song
    (sub_166F80), and the open folder has to stay open there.
  * 0x167D60 in sub_167A98, where both ka at once (stock: switch to the
    by-level sort, when the Flash has the command enabled) lands: with a
    folder open it asks pick to close it instead, and pick puts the cursor
    on the folder's card (the first card row above the cursor).

The stubs, the open folder and the card table live in sub_214FB8, a libc
routine nothing references (no call, jump, pointer or lui/addiu pair in the
image), right after taiko_exe's freeze-timer cave. An early build used
0x2FF000, a graphics DMA buffer the game does write at run time (it zeroed
words of the stub, which made a card the chosen song and hung difficulty
select); patch/unpatch take that one out again.
"""
from __future__ import annotations

import hashlib
import struct

from taiko2x6.exe.song_tables import R, _Asm, _r

F_GENRE = 0x001078C8          # (rows, index) -> row[2]
SITE_FILTER = 0x001386E0      # in sub_138688, the row filter
SITE_PICK = 0x00175C60        # in sub_175C20, the card pick
SITE_SETTLE = 0x00175E50      # in sub_175C20 (only an early build hooked it)
ORIG = {SITE_FILTER: 0x0C041E32, SITE_PICK: 0x0C041E32}   # jal sub_1078C8
OLD_SITES = {SITE_SETTLE: 0x0C04E0E4}                     # jal sub_138390
SITE_CMD = 0x00167D60         # in sub_167A98, the both-ka command
ORIG_CMD = (0x8C8F006C, 0x11E00005, 0x00000000)   # lw t7,0x6C(a0); beqz t7,+5; nop
RET_JUMP, RET_SORT, RET_KEEP = 0x00167D70, 0x00167D78, 0x00167D84
F_JUMP = 0x00173FD0           # (select) -> sub_175C20, the folder jump
CTX_PTR = 0x003A6638          # dword_3A6638

CAVE_VA = 0x00214FB8
CAVE_LEN = 0x1E8
CAVE_STOCK_SHA1 = '3ebdb8ed7801abb031fee3a31df34d3e4e616b5f'
OLD_CAVE_VA = 0x002FF000
OLD_CAVE_LEN = 0x200
SONG_MASK = 0x17F             # song genres 0..6 and 8 (C-POP)
# divider row genre -> the song genre its folder holds (omnimix_maker.HEADER_GENRE)
HEADER_GENRE = {7: 4, 9: 0, 10: 6, 11: 3, 12: 2, 13: 1, 14: 5, 15: 8}

# cave layout: the open folder (its song genre + 1, 0 = none open), the
# both-ka close request, u8[16] row genre -> folder genre + 1 for cards (0 for songs), then code
_OPEN, _CLOSING, _CARDS, _CODE = 0x00, 0x04, 0x08, 0x20


def _hi(va):
    return ((va + 0x8000) >> 16) & 0xFFFF


def _lo(va):
    return va & 0xFFFF


def _jal(va):
    return (0x03 << 26) | ((va >> 2) & 0x3FFFFFF)


def build() -> tuple[bytes, dict]:
    """(cave bytes, {'filter', 'pick', 'cmd': stub VAs})."""
    cards = bytearray(16)
    for h, g in HEADER_GENRE.items():
        cards[h] = g + 1
    data = struct.pack('<II', 0, 0) + bytes(cards)
    a = _Asm(CAVE_VA + _CODE)

    # pick: v0 = genre(cursor row) as before; a card opens its folder
    a.label('pick')
    a.addiu('sp', 'sp', -32)
    a.sd('ra', 0, 'sp')
    a.sw('a0', 8, 'sp')                    # the rows table
    a.sw('a1', 12, 'sp')                   # the cursor row
    a.jal(F_GENRE)
    a.nop()
    a.lui('t2', _hi(CAVE_VA))
    a.addiu('t2', 't2', _lo(CAVE_VA))
    a.lw('t3', _CLOSING, 't2')
    a.bnez('t3', 'p_close')
    a.sltiu('t0', 'v0', 16)
    a.beqz('t0', 'p_done')
    a.emit(_r('t2', 'v0', 't1', 0x21))     # addu t1, t2, v0
    a.lbu('t0', _CARDS, 't1')
    a.beqz('t0', 'p_done')                 # a song row: leave the folders be
    a.lw('t3', _OPEN, 't2')
    a.emit(_r('t0', 't3', 't4', 0x23))     # subu t4, t0, t3
    a.bnez('t4', 'p_open')
    a.nop()
    # the open folder's own card: close it, and hand sub_175C20 a genre its
    # switch ignores, so the cursor stays on the card instead of jumping
    a.sw('zero', _OPEN, 't2')
    a.beqz('zero', 'p_done')
    a.ori('v0', 'zero', 16)
    a.label('p_open')
    a.beqz('zero', 'p_done')
    a.sw('t0', _OPEN, 't2')
    # both ka (see cmd): close the folder and put the cursor on its card,
    # the first card row above the cursor ($s2 = the song_select object)
    a.label('p_close')
    a.sw('zero', _CLOSING, 't2')
    a.sw('zero', _OPEN, 't2')
    a.label('p_back')
    a.lw('a1', 12, 'sp')
    a.addiu('a1', 'a1', -1)
    a._br((0x01 << 26) | (R['a1'] << 21), 'p_skip')   # bltz a1
    a.sw('a1', 12, 'sp')
    a.jal(F_GENRE)
    a.lw('a0', 8, 'sp')
    a.sltiu('t0', 'v0', 16)
    a.beqz('t0', 'p_back')
    a.lui('t2', _hi(CAVE_VA))
    a.addiu('t2', 't2', _lo(CAVE_VA))
    a.emit(_r('t2', 'v0', 't1', 0x21))     # addu t1, t2, v0
    a.lbu('t0', _CARDS, 't1')
    a.beqz('t0', 'p_back')
    a.lw('a1', 12, 'sp')
    a.sw('a1', 0x1A0, 's2')                # the select's cursor -> the card
    a.label('p_skip')
    a.ori('v0', 'zero', 16)                # skip the folder jump
    a.label('p_done')
    a.ld('ra', 0, 'sp')
    a.jr('ra')
    a.addiu('sp', 'sp', 32)

    # cmd: both ka in song select (sub_167A98 at 0x167D60, $a0 = ctx, $s0 =
    # the select scene, $s4 = player * 8). Stock switches the list to the
    # by-level sort there. With a folder open it closes the folder instead
    # (through sub_173FD0 -> sub_175C20, which lands in pick above) and the
    # sort stays. Returns where to go on: 0x167D70 / 0x167D78 are the stock
    # paths (mode != 0 / == 0), 0x167D84 skips the sort switch.
    a.label('cmd')
    a.lui('t2', _hi(CAVE_VA))
    a.addiu('t2', 't2', _lo(CAVE_VA))
    a.lw('t0', _OPEN, 't2')
    a.bnez('t0', 'c_close')
    a.lw('t7', 0x6C, 'a0')
    a.lui('v0', _hi(RET_SORT))
    a.beqz('t7', 'c_ret')
    a.addiu('v0', 'v0', _lo(RET_SORT))
    a.lui('v0', _hi(RET_JUMP))
    a.addiu('v0', 'v0', _lo(RET_JUMP))
    a.label('c_ret')
    a.jr('ra')
    a.nop()
    a.label('c_close')
    a.addiu('sp', 'sp', -16)
    a.sd('ra', 0, 'sp')
    a.ori('t0', 'zero', 1)
    a.sw('t0', _CLOSING, 't2')
    a.emit(_r('s4', 's0', 't1', 0x21))     # addu t1, s4, s0
    a.sw('zero', 0x2208, 't1')             # drop this player's ka latches
    a.sw('zero', 0x220C, 't1')
    a.jal(F_JUMP)
    a.lw('a0', 0x2204, 's0')
    a.ld('ra', 0, 'sp')
    a.addiu('sp', 'sp', 16)
    a.lui('a0', _hi(CTX_PTR))
    a.lw('a0', _lo(CTX_PTR), 'a0')         # $a0 = ctx again (a delay slot uses it)
    a.lui('v0', _hi(RET_KEEP))
    a.jr('ra')
    a.addiu('v0', 'v0', _lo(RET_KEEP))

    # filter: v0 = genre(row) as before; in modes 0..3 the mask ($s0 of
    # sub_138688) hides every song outside the open folder
    a.label('filter')
    a.addiu('sp', 'sp', -16)
    a.sd('ra', 0, 'sp')
    a.jal(F_GENRE)
    a.nop()
    a.lw('t0', 0x6C, 's2')                 # $s2 = ctx in sub_138688
    a.sltiu('t1', 't0', 4)                 # modes 0..3 (2/3 after a song too)
    a.beqz('t1', 'f_done')
    a.lui('t2', _hi(CAVE_VA))
    a.addiu('t2', 't2', _lo(CAVE_VA))
    a.lw('t0', _OPEN, 't2')
    a.beqz('t0', 'f_set')
    a.ori('t1', 'zero', SONG_MASK)
    a.addiu('t0', 't0', -1)
    a.ori('t3', 'zero', 1)
    a.emit(_r('t0', 't3', 't3', 0x04))     # sllv t3, t3, t0
    a.emit(_r('t3', 'zero', 't3', 0x27))   # nor  t3, t3, zero
    a.emit(_r('t1', 't3', 't1', 0x24))     # and  t1, t1, t3
    a.label('f_set')
    a.emit(_r('t1', 'zero', 's0', 0x25))   # or   s0, t1, zero
    a.label('f_done')
    a.ld('ra', 0, 'sp')
    a.jr('ra')
    a.addiu('sp', 'sp', 16)

    code = a.finish()
    out = data + bytes(_CODE - len(data)) + code
    assert len(out) <= CAVE_LEN
    return out, {'filter': a.labels['filter'], 'pick': a.labels['pick'],
                 'cmd': a.labels['cmd']}


class FolderPatchError(RuntimeError):
    pass


def _word(dec, va, delta):
    return struct.unpack_from('<I', dec, va - delta)[0]


def _jal_into(w, va, n) -> bool:
    tgt = (w & 0x3FFFFFF) << 2
    return w >> 26 == 0x03 and va <= tgt < va + n


def _into_cave(w) -> bool:
    """A `jal` into either cave: this patch (any build of it)."""
    return _jal_into(w, CAVE_VA, CAVE_LEN) or _jal_into(w, OLD_CAVE_VA, OLD_CAVE_LEN)


JR_V0 = 0x00400008


def _cmd(buf, delta) -> tuple:
    return struct.unpack_from('<3I', buf, SITE_CMD - delta)


def _cmd_want(stubs) -> tuple:
    return (_jal(stubs['cmd']), 0, JR_V0)


def _ours(old: bytes, cave: bytes) -> bool:
    return old[_CODE:len(cave)] == cave[_CODE:]


# the data the first build at this cave kept at +8 (every genre -> its folder + 1)
_CURSOR_BUILD_TABLE = bytes([1, 2, 3, 4, 5, 6, 7, 5, 9, 1, 7, 4, 3, 2, 6, 9])


def _any_build(old: bytes, cave: bytes) -> bool:
    """Some build of this patch owns the cave (turning it off leaves the
    stubs there, so a later build must recognise them)."""
    return (_ours(old, cave) or old[_CARDS:_CODE] == cave[_CARDS:_CODE]
            or old[_CARDS:_CARDS + 16] == _CURSOR_BUILD_TABLE)


def is_on(dec: bytes, delta: int) -> bool:
    """Any build of the patch is in (see needs_upgrade)."""
    return any(_into_cave(_word(dec, va, delta)) for va in (*ORIG, *OLD_SITES, SITE_CMD))


def needs_upgrade(dec: bytes, delta: int) -> bool:
    """An early build is in: it hangs the game, so it should be replaced."""
    return is_on(dec, delta) and not is_current(dec, delta)


def is_current(dec: bytes, delta: int) -> bool:
    """Every site calls this build's stubs."""
    cave, stubs = build()
    o = CAVE_VA - delta
    return (_word(dec, SITE_FILTER, delta) == _jal(stubs['filter'])
            and _word(dec, SITE_PICK, delta) == _jal(stubs['pick'])
            and _cmd(dec, delta) == _cmd_want(stubs)
            and _ours(bytes(dec[o:o + CAVE_LEN]), cave)
            and not any(_into_cave(_word(dec, va, delta)) for va in OLD_SITES))


def _clean_old(buf, delta) -> bool:
    """Undo what early builds left: the settle hook and the stub in the DMA
    buffer (zero in a stock file)."""
    changed = False
    for va, orig in OLD_SITES.items():
        if _into_cave(_word(buf, va, delta)):
            struct.pack_into('<I', buf, va - delta, orig)
            changed = True
    o = OLD_CAVE_VA - delta
    old_stub = buf[o + 0x20:o + 0x24] == bytes.fromhex('e0ffbd27')   # addiu sp,sp,-32
    if old_stub and any(buf[o:o + OLD_CAVE_LEN]):
        buf[o:o + OLD_CAVE_LEN] = bytes(OLD_CAVE_LEN)
        changed = True
    return changed


def patch(dec: bytes, delta: int, log=print) -> bytes:
    buf = bytearray(dec)
    cave, stubs = build()
    o = CAVE_VA - delta
    for va, orig in ORIG.items():
        cur = _word(buf, va, delta)
        if cur != orig and not _into_cave(cur):
            raise FolderPatchError(f'{va:08X}: expected {orig:#010x}, got {cur:#010x} — refusing')
    cmd = _cmd(buf, delta)
    if cmd != ORIG_CMD and not _into_cave(cmd[0]):
        raise FolderPatchError(f'{SITE_CMD:08X}: not the stock both-ka code — refusing')
    old = bytes(buf[o:o + CAVE_LEN])
    in_use = any(_jal_into(_word(buf, va, delta), CAVE_VA, CAVE_LEN) for va in ORIG)
    if not (in_use or _any_build(old, cave) or hashlib.sha1(old).hexdigest() == CAVE_STOCK_SHA1):
        raise FolderPatchError(f'{CAVE_VA:08X}: cave is not the stock routine — refusing')
    cleaned = _clean_old(buf, delta)
    want = {SITE_FILTER: _jal(stubs['filter']), SITE_PICK: _jal(stubs['pick'])}
    if (all(_word(buf, va, delta) == w for va, w in want.items())
            and _cmd(buf, delta) == _cmd_want(stubs) and _ours(old, cave) and not cleaned):
        log(f'{CAVE_VA:08X}  category folders already on')
        return bytes(buf)
    buf[o + _CARDS:o + len(cave)] = cave[_CARDS:]
    if not _ours(old, cave):
        buf[o:o + _CARDS] = cave[:_CARDS]
    for va, w in want.items():
        struct.pack_into('<I', buf, va - delta, w)
    struct.pack_into('<3I', buf, SITE_CMD - delta, *_cmd_want(stubs))
    log(f'{SITE_FILTER:08X}  song select as category folders '
        f'(filter -> {stubs["filter"]:08X}, card pick -> {stubs["pick"]:08X}, '
        f'both ka -> {stubs["cmd"]:08X})')
    return bytes(buf)


def unpatch(dec: bytes, delta: int, log=print) -> bytes:
    """Put the calls back. The stubs stay in their cave (nothing calls them,
    and patch accepts them there)."""
    buf = bytearray(dec)
    changed = _clean_old(buf, delta)
    for va, orig in ORIG.items():
        if _into_cave(_word(buf, va, delta)):
            struct.pack_into('<I', buf, va - delta, orig)
            changed = True
    if _into_cave(_cmd(buf, delta)[0]):
        struct.pack_into('<3I', buf, SITE_CMD - delta, *ORIG_CMD)
        changed = True
    if changed:
        log(f'{SITE_FILTER:08X}  category folders off (one long song list)')
    return bytes(buf)

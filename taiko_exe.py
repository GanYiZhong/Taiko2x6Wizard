#!/usr/bin/env python3
"""Decrypt / patch / repack `taiko` — the executable Taiko 14+ actually runs.

The game does NOT run `T14GAME` from the dongle: `NM00057.acgame` sets
``media=HDD``, so `T14LOAD` boots **`pfs0:taiko`** out of HDD partition
``t14jp1400.0001``.  Patching T14GAME has zero effect.

`taiko` is an ELF32 whose body is encrypted with a MODIFIED Blowfish (do not use
a stock library) — reversed from T14LOAD, which is itself a plain ELF32:

    0x1002f08  Blowfish_Init(key, len)  -- also builds the block-shuffle table
    0x1002da0  Blowfish_Encrypt(xl*, xr*)
    0x100340c  bulk decrypt(buf, nsectors, iv_l, iv_r)
    0x1000E20  boot: read pfs0:taiko -> 0x01047880, decrypt(size>>11), load_elf

Modifications vs stock Blowfish: only 10 P entries (=> 8 rounds) and the S-boxes
start at word 10 of the standard 1042-word blob.  F() is unmodified.  Bulk mode
is CBC with IV=(0,1), chained across the whole file in a *shuffled* block order
(seeded Fisher-Yates), and ``size >> 11`` leaves the trailing 628 bytes as
plaintext — which is why the ELF section headers at the tail are readable.

The song-count patch this module applies is documented on :func:`patch_arrayb`.

Decrypted image address map: **VA = file_offset + 0xFF000**.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

M = 0xFFFFFFFF

# --- T14LOAD (source of the cipher constants + key material) --------------- #
T14LOAD_NAME = 'T14LOAD.bin'
T14LOAD_DELTA = 0xFFF000
CONST_VA = 0x01039968       # standard 1042-word Blowfish blob, big-endian, .rodata
KEYBUF_VA = 0x01031608      # 16-byte obfuscated blob, .data
SEED_STR_VA = 0x010390D0    # "T140NBGI"
# Self-check: the key derivation is verified to produce exactly this.
KNOWN_KEY = bytes.fromhex('fbb5753eb98107')

# --- taiko image ----------------------------------------------------------- #
DELTA = 0xFF000             # decrypted taiko: VA = file_off + DELTA
TAIKO_SIZE = 2792052
PRISTINE_MD5 = '10e11b77a93ed98689362798576fc53f'
PARTITION = 't14jp1400.0001'
PFS_PATH = '/taiko'


class TaikoExeError(RuntimeError):
    pass


def find_t14load(hint: str | Path | None = None) -> str:
    """Locate T14LOAD.bin (an explicit hint, $T14LOAD_BIN, or next to this file)."""
    cands = []
    if hint:
        cands.append(Path(hint))
    env = os.environ.get('T14LOAD_BIN')
    if env:
        cands.append(Path(env))
    import apppaths
    cands.append(apppaths.resource_dir() / T14LOAD_NAME)
    cands.append(Path(__file__).resolve().parent / T14LOAD_NAME)
    for c in cands:
        if c.is_file():
            return str(c)
    raise TaikoExeError(
        f'{T14LOAD_NAME} not found — it carries the cipher constants and key '
        f'material for `taiko`. Put it next to this toolkit or set $T14LOAD_BIN.')


class BF:
    """Namco's modified Blowfish: 10 P entries (8 rounds), S-boxes from word 10."""

    def __init__(self, const_words):
        self.const = const_words        # 10 + 4*256 words, already byteswapped
        self.P = []
        self.S = []
        self.table = []

    def init(self, key):                                        # 0x1002f08
        c = self.const
        self.P = list(c[:10])                                   # 0x1002F68: i < 0xa
        self.S = [list(c[10 + b * 256: 10 + (b + 1) * 256]) for b in range(4)]

        j = 0
        for i in range(10):
            d = 0
            for _ in range(4):
                d = ((d << 8) | key[j % len(key)]) & M
                j += 1
            self.P[i] ^= d

        xl = xr = 0
        for i in range(0, 10, 2):                               # 0x1003128: s0 < 0xa
            xl, xr = self.encrypt(xl, xr)
            self.P[i], self.P[i + 1] = xl, xr
        for b in range(4):
            for i in range(0, 256, 2):
                xl, xr = self.encrypt(xl, xr)
                self.S[b][i], self.S[b][i + 1] = xl, xr

        self._build_table(key)

    def _build_table(self, key):                                # 0x1003238
        """identity fill + Fisher-Yates seeded by the key's first 4 bytes (BE)."""
        self.table = list(range(256))
        st = struct.unpack('>I', bytes(key[:4]))[0]             # 0x100326C: BE u32

        def rnd(n):                                             # 0x1003580: LCG
            nonlocal st
            st = (st * 0x41C64E6D + 0x3039) & M
            return (st >> 16) % n

        for i in range(1, 256):                                 # 0x1003294..0x10032CC
            r = rnd(i + 1)
            self.table[i], self.table[r] = self.table[r], self.table[i]

    def F(self, x):
        S = self.S
        h = (S[0][x >> 24] + S[1][(x >> 16) & 0xFF]) & M
        return ((h ^ S[2][(x >> 8) & 0xFF]) + S[3][x & 0xFF]) & M

    def encrypt(self, xl, xr):
        P = self.P
        for i in range(8):
            xl ^= P[i]
            xr ^= self.F(xl)
            xl, xr = xr, xl
        xl, xr = xr, xl
        return xl ^ P[9], xr ^ P[8]

    def decrypt(self, xl, xr):                                  # 0x1003474: v0 9->2
        P = self.P
        for i in range(9, 1, -1):
            xl ^= P[i]
            xr ^= self.F(xl)
            xl, xr = xr, xl
        xl, xr = xr, xl
        return xl ^ P[0], xr ^ P[1]                             # 0x1003508 / 0x1003510


_cipher_cache: dict = {}


def _cipher(t14load: str | None = None) -> BF:
    """Derive the key (0x01000F30..0x01000FFC) and return a ready BF. Cached —
    the key schedule is ~1000 Blowfish ops and both decrypt and encrypt need it.
    """
    path = find_t14load(t14load)
    if path in _cipher_cache:
        return _cipher_cache[path]

    d = open(path, 'rb').read()
    off = CONST_VA - T14LOAD_DELTA
    const = list(struct.unpack_from('>%dI' % (10 + 4 * 256), d, off))
    seed = d[SEED_STR_VA - T14LOAD_DELTA: SEED_STR_VA - T14LOAD_DELTA + 8]  # T140NBGI
    b = bytearray(d[KEYBUF_VA - T14LOAD_DELTA: KEYBUF_VA - T14LOAD_DELTA + 16])

    bf = BF(const)
    bf.init(seed)                                               # Init("T140NBGI", 8)

    for i in range(8):                                          # 0x1000F44
        b[2 * i] ^= 0x0A
        b[2 * i + 1] ^= 0xEB

    def enc_inplace():                                          # 0x1002da0(b+0, b+4)
        xl, xr = struct.unpack_from('<2I', bytes(b), 0)
        xl, xr = bf.encrypt(xl, xr)
        struct.pack_into('<2I', b, 0, xl, xr)

    enc_inplace()                                               # 0x1000F7C
    for i in range(8):                                          # 0x1000F88
        b[i] ^= b[i + 8]
    enc_inplace()                                               # 0x1000FB0
    b[0] ^= b[7]                                                # 0x1000FF8
    key = bytes(b[:7])                                          # Init(b, 7)

    if key != KNOWN_KEY:
        raise TaikoExeError(
            f'key derivation produced {key.hex(" ")}, expected '
            f'{KNOWN_KEY.hex(" ")} — is {path} the right T14LOAD?')

    out = BF(const)
    out.init(key)
    _cipher_cache[path] = out
    return out


def _bulk(data: bytes, encrypting: bool, t14load=None, progress=None) -> bytes:
    bf = _cipher(t14load)
    buf = bytearray(data)
    nsec = len(data) >> 11                                      # 0x1001014: sra 11
    pl, pr = 0, 1                                               # IV = (0, 1)
    for s in range(nsec):
        base = s * 2048
        for v1 in range(256):
            o = base + bf.table[v1] * 8                         # shuffled order
            if encrypting:
                xl, xr = struct.unpack_from('<2I', buf, o)
                cl, cr = bf.encrypt(xl ^ pl, xr ^ pr)
                struct.pack_into('<2I', buf, o, cl, cr)
                pl, pr = cl, cr                                 # chain on ciphertext
            else:
                cl, cr = struct.unpack_from('<2I', buf, o)
                xl, xr = bf.decrypt(cl, cr)
                struct.pack_into('<2I', buf, o, xl ^ pl, xr ^ pr)
                pl, pr = cl, cr
        if progress and (s & 63) == 0:
            progress(s, nsec)
    if progress:
        progress(nsec, nsec)
    return bytes(buf)


def decrypt_taiko(data: bytes, t14load=None, progress=None) -> bytes:
    """Ciphertext `taiko` -> decrypted ELF32 image."""
    out = _bulk(data, False, t14load, progress)
    if out[:4] != b'\x7fELF':
        raise TaikoExeError('decryption did not yield an ELF — wrong input file?')
    return out


def encrypt_taiko(plain: bytes, t14load=None, progress=None) -> bytes:
    """Decrypted image -> ciphertext the game will accept. Byte-exact inverse."""
    return _bulk(plain, True, t14load, progress)


# --------------------------------------------------------------------------- #
#  The song-count patch
# --------------------------------------------------------------------------- #
OLD_B, NEW_B = 0x03D0, 0x4A80        # arrayB base: old -> relocated (grown tail)
OLD_ALLOC, NEW_ALLOC = 0x4A80, 0x4A90
ARRAYA_BASE = 0x88
NEXT_FIELD = 0x3E0                   # first live field after the freed arrayB
BASE_SONGS = 210
MAX_SONGS = (NEXT_FIELD - ARRAYA_BASE) // 4          # 214

# (VA, expected_imm, new_imm, description)
_SITES = [
    (0x001012D0, OLD_ALLOC, NEW_ALLOC, 'addiu $a0,$zero,SIZE   ctx allocation'),
    (0x00135EFC, OLD_B, NEW_B, 'addiu $a0,$s1,OFF      memset arrayB base'),
    (0x00136034, OLD_B, NEW_B, 'sw    $t7,OFF($t6)     arrayB[slot] = musicID'),
    (0x00136CA0, OLD_B, NEW_B, 'lw    $s7,OFF($t7)     read arrayB[slot]'),
    (0x00138094, OLD_B, NEW_B, 'lw    $a1,OFF($a1)     read arrayB[slot]'),
]
_BOUND_VA = 0x00135F20               # sltiu $t7, $t5, N   -- arrayA init count
BOUND_FILE_OFF = _BOUND_VA - DELTA   # 0x36F20


def song_count(dec: bytes) -> int:
    """Current arrayA bound (= the song ceiling the exe is built for).

    Reads the full 16-bit ``sltiu`` immediate, not just its low byte, so a
    ceiling above 255 (the relocate-arrayA path) reports correctly. For the
    stock 0x00d2 / 0x00d4 the high byte is zero, so this is byte-compatible.
    """
    return struct.unpack_from('<H', dec, BOUND_FILE_OFF)[0]


def is_patched(dec: bytes) -> bool:
    """True if arrayB has already been relocated."""
    return struct.unpack_from('<H', dec, _SITES[1][0] - DELTA)[0] == NEW_B


def patch_arrayb(dec: bytes, songs: int, log=print) -> bytes:
    """Raise the song ceiling to ``songs`` by RELOCATING ctx->arrayB.

    ctx->arrayA is a ``songs``-entry "already picked this credit" map at ctx+0x88
    and it ends EXACTLY where ctx->arrayB (the up-to-4-song setlist) begins, so
    simply raising the bound aliases the two arrays::

        arrayA[210] = ctx+0x88+210*4 = ctx+0x3D0 = arrayB[0]
        arrayA[211] = ctx+0x3D4                  = arrayB[1]

    Every pick then corrupts the other array — ``sw $t7,0x3d0($t6)``
    (arrayB[slot]=musicID; slot 0 -> ctx+0x3D0) collides with
    ``sw $t4,0x88($t5)`` (arrayA[musicID]=slot; id 210 -> ctx+0x3D0) — and the
    select filter hides any row whose ``arrayA[musicID] != -1``.  Observed:
    playing song 211 makes song 210 vanish (arrayB[0]=211 lands on arrayA[210]),
    while playing 210 sends both writes to the same word so 211 survives.  That
    asymmetry is the signature of the aliasing.

    Real fix: ctx is ``operator new(0x4A80)`` at VA 0x001012D0, and 0x4A80
    appears exactly ONCE in .text (no struct-size assumption to break), so grow
    the allocation by 16 bytes and move arrayB to the fresh tail at 0x4A80.
    That frees ctx+0x3D0..0x3DF and lets arrayA run to ctx+0x3E0 — the next live
    field — hence :data:`MAX_SONGS` = 214.
    """
    if not BASE_SONGS <= songs <= MAX_SONGS:
        raise TaikoExeError(
            f'songs must be {BASE_SONGS}..{MAX_SONGS} (arrayA spans '
            f'{ARRAYA_BASE:#x}..{NEXT_FIELD:#x} once arrayB moves out)')
    buf = bytearray(dec)
    for va, want, new, what in _SITES:
        off = va - DELTA
        cur = struct.unpack_from('<H', buf, off)[0]
        if cur == new:
            log(f'{va:08X}  already {new:#06x}  {what}')
            continue
        if cur != want:
            raise TaikoExeError(
                f'{va:08X}: immediate is {cur:#06x}, expected {want:#06x} — '
                f'refusing to patch an image I do not recognise')
        struct.pack_into('<H', buf, off, new)
        log(f'{va:08X}  {want:#06x} -> {new:#06x}  {what}')

    off = BOUND_FILE_OFF
    cur = buf[off]
    if cur not in (BASE_SONGS, songs) and not (BASE_SONGS < cur <= MAX_SONGS):
        raise TaikoExeError(
            f'{_BOUND_VA:08X}: arrayA bound is {cur:#x}, expected {BASE_SONGS:#x} '
            f'(pristine) or a previous patch — refusing')
    buf[off] = songs
    log(f'{_BOUND_VA:08X}  {cur:#04x} -> {songs:#04x}  sltiu $t7,$t5,N        '
        f'arrayA init count ({songs} songs)')
    return bytes(buf)


# --------------------------------------------------------------------------- #
#  The BIG song-count patch — RELOCATE arrayA (breaks the 214 wall)
# --------------------------------------------------------------------------- #
# The 214 ceiling is structural: ctx->arrayA at ctx+0x88 is boxed in by
# ctx->selectedMusicID at ctx+0x3E0 (proven at VA 0x136020: `lw $t7,0x3e0($a0)`
# feeds `sll;addu;sw $t4,0x88($t5)` = arrayA[selMusicID]=slot). Relocating arrayB
# only reclaimed the 4 words at 0x3D0..0x3DF, hence 210->214.
#
# To go further we move arrayA ITSELF to a fresh tail at ctx+0x4A90 — past both
# the original struct and the relocated arrayB (4 words at 0x4A80..0x4A90), so
# 0x4A90 is free whether or not arrayB was relocated. arrayA there never aliases
# arrayB (0x3D0) either, so this single relocation fixes BOTH the aliasing and
# the ceiling; the arrayB patch is neither required nor conflicting.
#
# The select filter (the ONLY thing gating a song's visibility) reads
# arrayA[musicID] at 0x13871C, so once arrayA is big enough every musicID shows.
#
# Complete arrayA site set (all `0x88(reg)` hits in .text were classified; only
# the three below are ctx-based arrayA — the other 27 are unrelated structs):
ARRAYA_OLD = 0x88
ARRAYA_NEW = 0x4A90        # fresh tail, past relocated arrayB (0x4A80..0x4A90)
_ARRAYA_SITES = [
    (0x00135F14, ARRAYA_OLD, ARRAYA_NEW, 'addiu $t6,$s1,OFF     arrayA init base'),
    (0x00136038, ARRAYA_OLD, ARRAYA_NEW, 'sw    $t4,OFF($t5)    arrayA[musicID]=slot'),
    (0x0013871C, ARRAYA_OLD, ARRAYA_NEW, 'lw    $t5,OFF($t7)    select filter read'),
]
_ALLOC_VA = 0x001012D0     # addiu $a0,$zero,SIZE  operator new(ctx) size
# arrayA occupies [0x4A90, 0x4A90 + N*4); the alloc size and the base offset are
# both 16-bit signed immediates, so N is capped where 0x4A90 + N*4 would reach
# 0x8000 (a negative addiu immediate). ~3419 songs — far more than the library.
MAX_SONGS_BIG = (0x8000 - ARRAYA_NEW) // 4 - 1     # 3419


def is_arraya_relocated(dec: bytes) -> bool:
    """True if arrayA has been moved out to ctx+0x4A90."""
    return struct.unpack_from('<H', dec, _ARRAYA_SITES[0][0] - DELTA)[0] == ARRAYA_NEW


def patch_arraya(dec: bytes, songs: int, log=print) -> bytes:
    """Raise the ceiling to ``songs`` (up to :data:`MAX_SONGS_BIG`) by RELOCATING
    ctx->arrayA to a fresh tail at ctx+0x4A90 and growing the ctx allocation.

    This is the general fix that breaks the 214 wall. Unlike :func:`patch_arrayb`
    it does not touch arrayB at all (arrayA at 0x4A90 aliases nothing).

    ⚠ EXPERIMENTAL above 214: the select FILTER scales, so songs display and
    play, but the select screen also walks several 210-entry per-song tables
    (VA 0x131240 / 0x131394 / 0x131554 / 0x131A28 and the list widget built via
    0x1353a8) that this patch does NOT enlarge. Those overran harmlessly by 4 at
    214; at a much higher ceiling they can glitch or crash the select screen.
    Test on a COPY and raise the number gradually.
    """
    if not BASE_SONGS <= songs <= MAX_SONGS_BIG:
        raise TaikoExeError(
            f'songs must be {BASE_SONGS}..{MAX_SONGS_BIG} (arrayA at '
            f'{ARRAYA_NEW:#x}; 0x4A90 + N*4 must stay a positive 16-bit immediate)')
    buf = bytearray(dec)

    for va, want, new, what in _ARRAYA_SITES:
        off = va - DELTA
        cur = struct.unpack_from('<H', buf, off)[0]
        if cur == new:
            log(f'{va:08X}  already {new:#06x}  {what}')
            continue
        if cur != want:
            raise TaikoExeError(
                f'{va:08X}: immediate is {cur:#06x}, expected {want:#06x} — '
                f'refusing to patch an image I do not recognise')
        struct.pack_into('<H', buf, off, new)
        log(f'{va:08X}  {want:#06x} -> {new:#06x}  {what}')

    # ctx allocation must cover arrayA's end (0x4A90 + N*4). Accept a pristine
    # 0x4A80, an arrayB-relocated 0x4A90, or a previous big-patch value.
    alloc_off = _ALLOC_VA - DELTA
    alloc_cur = struct.unpack_from('<H', buf, alloc_off)[0]
    alloc_new = ARRAYA_NEW + songs * 4
    if not (0x4A80 <= alloc_cur <= 0x7FFF):
        raise TaikoExeError(
            f'{_ALLOC_VA:08X}: ctx alloc size is {alloc_cur:#06x}, not a value I '
            f'recognise (0x4A80 pristine / 0x4A90 arrayB / prior big patch)')
    struct.pack_into('<H', buf, alloc_off, alloc_new)
    log(f'{_ALLOC_VA:08X}  {alloc_cur:#06x} -> {alloc_new:#06x}  '
        f'addiu $a0,$zero,SIZE   ctx alloc grown for arrayA[{songs}]')

    # arrayA init count (fills N entries with -1); full 16-bit immediate.
    off = BOUND_FILE_OFF
    cur = struct.unpack_from('<H', buf, off)[0]
    if not (BASE_SONGS <= cur <= MAX_SONGS_BIG):
        raise TaikoExeError(
            f'{_BOUND_VA:08X}: arrayA bound is {cur:#06x}, outside '
            f'{BASE_SONGS}..{MAX_SONGS_BIG} — refusing')
    struct.pack_into('<H', buf, off, songs)
    log(f'{_BOUND_VA:08X}  {cur:#06x} -> {songs:#06x}  sltiu $t7,$t5,N        '
        f'arrayA init count ({songs} songs)')
    return bytes(buf)


# --------------------------------------------------------------------------- #
#  Song-select inactivity timer (曲選択タイマー)
# --------------------------------------------------------------------------- #
# The song-select flow has three independent inactivity countdowns, each a
# global initialised to 120 (seconds) with `addiu $t7, $zero, 0x78` and then
# decremented once per tick until it reaches 0, at which point all three call
# the same attract-mode/exit routine (VA 0x18d5b8). One drives the visible
# on-screen countdown; the others cover the difficulty/entry sub-steps. They
# are the ONLY globals initialised to exactly 120 with this countdown-to-exit
# shape (found by sweeping every `li 120 -> sw <gp-field>` in .text), so
# patching all three keeps the whole select flow on the same limit.
#
# imm16 is the low half-word of the little-endian instruction word, so the
# value is a plain u16 at the instruction's file offset. Signed 16-bit, and
# the on-screen counter is 3 digits, so the sane range is 1..999 seconds.
TIMER_DEFAULT = 120
TIMER_MIN = 1
TIMER_MAX = 999
_TIMER_SITES = [0x00140D18, 0x001424C0, 0x001426C8]   # `addiu $t7,$zero,0x78`
# addiu $t7,$zero,imm  encodes (LE) as: <imm:u16> 0F 24
_TIMER_TAIL = bytes.fromhex('0f24')


def select_timer(dec: bytes) -> int:
    """Current song-select timer in seconds (reads the first init site)."""
    return struct.unpack_from('<H', dec, _TIMER_SITES[0] - DELTA)[0]


def patch_select_timer(dec: bytes, seconds: int, log=print) -> bytes:
    """Set the song-select inactivity timer (all three countdown globals).

    `seconds` in 1..999. Each site must currently be the `addiu $t7,$zero,imm`
    instruction (tail 0F 24) or the patch refuses, so a shifted/edited binary
    can never be silently corrupted.
    """
    if not (TIMER_MIN <= seconds <= TIMER_MAX):
        raise TaikoExeError(
            f'timer must be {TIMER_MIN}..{TIMER_MAX} seconds, got {seconds}')
    buf = bytearray(dec)
    for va in _TIMER_SITES:
        off = va - DELTA
        if bytes(buf[off + 2: off + 4]) != _TIMER_TAIL:
            raise TaikoExeError(
                f'{va:08X}: not the expected `addiu $t7,$zero,imm` timer '
                f'instruction (tail {buf[off+2]:02x}{buf[off+3]:02x}) — refusing')
        cur = struct.unpack_from('<H', buf, off)[0]
        struct.pack_into('<H', buf, off, seconds)
        log(f'{va:08X}  {cur:#05x} -> {seconds:#05x}  addiu $t7,$zero,N     '
            f'select timer ({cur} -> {seconds} s)')
    return bytes(buf)


# --------------------------------------------------------------------------- #
#  HDD image I/O
# --------------------------------------------------------------------------- #
def read_from_hdd(img: str | Path, partition: str = PARTITION) -> bytes:
    import ps2hdd
    h = ps2hdd.Ps2Hdd(str(img))
    try:
        return h.pfs_read(partition, PFS_PATH)
    finally:
        h.close()


def write_to_hdd(img: str | Path, data: bytes, partition: str = PARTITION) -> None:
    """Overwrite /taiko in place. The size never changes, so this reuses the
    existing extents; a size change would mean the caller built it wrong."""
    import ps2hdd
    if len(data) != TAIKO_SIZE:
        raise TaikoExeError(
            f'refusing to write {len(data)} B — `taiko` must stay {TAIKO_SIZE} B')
    h = ps2hdd.Ps2Hdd(str(img), writable=True)
    try:
        before = h.pfs_read(partition, PFS_PATH)
        if len(before) != len(data):
            raise TaikoExeError(
                f'size mismatch: {len(before)} B on the image vs {len(data)} B new')
        if before == data:
            return
        h.pfs_write(partition, PFS_PATH, data)
        if h.pfs_read(partition, PFS_PATH) != data:
            raise TaikoExeError('read-back mismatch — the image may be inconsistent!')
    finally:
        h.close()


def patch_hdd(img: str | Path, songs: int, partition: str = PARTITION,
              t14load=None, log=print, progress=None) -> dict:
    """Extract /taiko, relocate arrayB, raise the ceiling, repack, write back.

    Returns a summary dict.  Re-encryption is verified to round-trip before the
    image is touched.
    """
    import hashlib
    enc = read_from_hdd(img, partition)
    log(f'read {PARTITION}:{PFS_PATH}  {len(enc):,} B  md5 {hashlib.md5(enc).hexdigest()}')

    dec = decrypt_taiko(enc, t14load, progress)
    log(f'decrypted OK (ELF32); current song ceiling = {song_count(dec)}, '
        f'arrayB {"already relocated" if is_patched(dec) else "at ctx+0x3d0"}, '
        f'arrayA {"relocated" if is_arraya_relocated(dec) else "at ctx+0x88"}')

    if songs > MAX_SONGS:
        log(f'ceiling {songs} > {MAX_SONGS}: relocating arrayA (experimental path)')
        out = patch_arraya(dec, songs, log)
    else:
        out = patch_arrayb(dec, songs, log)
    new_enc = encrypt_taiko(out, t14load, progress)
    if decrypt_taiko(new_enc, t14load) != out:
        raise TaikoExeError('repack failed its round-trip self-check — not writing')

    write_to_hdd(img, new_enc, partition)
    md5 = hashlib.md5(new_enc).hexdigest()
    log(f'written and read back OK — md5 {md5}')
    return {'songs': songs, 'size': len(new_enc), 'md5': md5}


def patch_hdd_timer(img: str | Path, seconds: int, partition: str = PARTITION,
                    t14load=None, log=print, progress=None) -> dict:
    """Extract /taiko, set the song-select timer, repack, write back.

    Independent of the song-ceiling patch — both edits can coexist. Returns a
    summary dict; re-encryption is round-trip-verified before the image is
    touched.
    """
    import hashlib
    enc = read_from_hdd(img, partition)
    dec = decrypt_taiko(enc, t14load, progress)
    cur = select_timer(dec)
    log(f'decrypted OK (ELF32); current select timer = {cur} s')
    out = patch_select_timer(dec, seconds, log)
    new_enc = encrypt_taiko(out, t14load, progress)
    if decrypt_taiko(new_enc, t14load) != out:
        raise TaikoExeError('repack failed its round-trip self-check — not writing')
    write_to_hdd(img, new_enc, partition)
    md5 = hashlib.md5(new_enc).hexdigest()
    log(f'written and read back OK — md5 {md5}')
    return {'seconds': seconds, 'was': cur, 'md5': md5}


def restore_hdd(img: str | Path, pristine: str | Path,
                partition: str = PARTITION, log=print) -> None:
    """Put a pristine `taiko` back (keep a copy before your first patch!)."""
    import hashlib
    data = Path(pristine).read_bytes()
    md5 = hashlib.md5(data).hexdigest()
    log(f'restoring {pristine} ({len(data):,} B, md5 {md5}'
        f'{" — pristine" if md5 == PRISTINE_MD5 else ""})')
    write_to_hdd(img, data, partition)
    log('restored.')


# --------------------------------------------------------------------------- #
#  GUI dialog
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtCore import Qt, QThread, Signal
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QSpinBox, QLabel,
        QMessageBox, QPlainTextEdit, QLineEdit,
    )
except ImportError:                     # CLI-only environment
    pass
else:
    import appconfig

    class _Worker(QThread):
        done = Signal(object)
        line = Signal(str)

        def __init__(self, fn):
            super().__init__()
            self._fn = fn

        def run(self):
            try:
                self.done.emit(self._fn(self.line.emit))
            except Exception as exc:
                import traceback
                self.done.emit(('ERROR', exc, traceback.format_exc()))

    class TaikoExeDialog(QDialog):
        """Raise the T14+ song ceiling in `taiko` on a PS2 HDD image."""

        def __init__(self, parent=None, default_img=''):
            super().__init__(parent)
            self.setWindowTitle('Patch taiko song limit (T14+)')
            self.resize(720, 460)
            self._workers: list[_Worker] = []
            self._build_ui()
            if default_img:
                self.ed_img.setText(default_img)

        def _build_ui(self):
            lay = QVBoxLayout(self)

            row = QHBoxLayout()
            b = QPushButton('HDD .img…'); b.clicked.connect(self._pick)
            self.ed_img = QLineEdit(); self.ed_img.setReadOnly(True)
            row.addWidget(b); row.addWidget(self.ed_img, 1)
            lay.addLayout(row)

            row = QHBoxLayout()
            row.addWidget(QLabel('Song ceiling:'))
            self.sp = QSpinBox()
            self.sp.setRange(BASE_SONGS, MAX_SONGS_BIG)
            self.sp.setValue(212)
            self.sp.setToolTip(
                f'{BASE_SONGS} = stock.\n'
                f'<= {MAX_SONGS}: proven path (relocate arrayB; arrayA grows into '
                f'the freed 4 words up to the next live field at ctx+{NEXT_FIELD:#x}).\n'
                f'> {MAX_SONGS}: EXPERIMENTAL — relocates arrayA itself to '
                f'ctx+{ARRAYA_NEW:#x} (max {MAX_SONGS_BIG}). Songs display and play, '
                f'but the select screen still walks a few 210-entry tables that are '
                f'not yet enlarged, so a high number may glitch/crash the wheel. '
                f'Test on a COPY and raise it gradually.')
            self.sp.valueChanged.connect(self._ceiling_changed)
            row.addWidget(self.sp)
            self.lbl_range = QLabel(f'(stock {BASE_SONGS}, safe {MAX_SONGS}, '
                                    f'experimental max {MAX_SONGS_BIG})')
            row.addWidget(self.lbl_range)
            row.addStretch(1)
            self.b_check = QPushButton('Check'); self.b_check.clicked.connect(self._check)
            self.b_backup = QPushButton('Back up taiko…'); self.b_backup.clicked.connect(self._backup)
            self.b_patch = QPushButton('Patch'); self.b_patch.clicked.connect(self._patch)
            self.b_restore = QPushButton('Restore…'); self.b_restore.clicked.connect(self._restore)
            for w in (self.b_check, self.b_backup, self.b_patch, self.b_restore):
                row.addWidget(w)
            lay.addLayout(row)

            self.log = QPlainTextEdit(); self.log.setReadOnly(True)
            self.log.setStyleSheet('font-family: Consolas, monospace;')
            lay.addWidget(self.log, 1)

            self.status = QLabel(
                f'<= {MAX_SONGS}: relocates arrayB (proven). > {MAX_SONGS}: relocates '
                f'arrayA (experimental, up to {MAX_SONGS_BIG}). Close PCSX2 first — it '
                f'locks the .img. Back up `taiko` before your first patch.')
            self.status.setWordWrap(True)
            self.status.setStyleSheet('color:#999;')
            lay.addWidget(self.status)

        def _ceiling_changed(self, n):
            # Colour the range hint when the user crosses into experimental territory.
            if n > MAX_SONGS:
                self.lbl_range.setText(
                    f'⚠ experimental (> {MAX_SONGS}); relocates arrayA, max {MAX_SONGS_BIG}')
                self.lbl_range.setStyleSheet('color:#c80;')
            else:
                self.lbl_range.setText(f'(stock {BASE_SONGS}, safe {MAX_SONGS}, '
                                       f'experimental max {MAX_SONGS_BIG})')
                self.lbl_range.setStyleSheet('')

        def _pick(self):
            p = appconfig.pick_open(self, 'hddimg', 'Open PS2 HDD image',
                                    'HDD image (*.img *.raw *.bin);;All files (*)')
            if p:
                self.ed_img.setText(p)

        def _img(self):
            p = self.ed_img.text().strip()
            if not p:
                QMessageBox.information(self, 'Patch taiko', 'Pick an HDD image first.')
                return None
            return p

        def _check(self):
            img = self._img()
            if not img:
                return

            def task(log):
                enc = read_from_hdd(img)
                log(f'read /taiko: {len(enc):,} B')
                dec = decrypt_taiko(enc, progress=lambda s, n: None)
                return (song_count(dec), is_patched(dec), is_arraya_relocated(dec))

            def ok(r):
                n, moved, a_moved = r
                self._say(f'current song ceiling = {n}')
                self._say(f'arrayB relocated = {moved}')
                self._say(f'arrayA relocated = {a_moved}  '
                          f'({"experimental >214 path" if a_moved else "in place at ctx+0x88"})')
                if not moved and not a_moved and n > BASE_SONGS:
                    self._say('')
                    self._say('WARNING: the ceiling is raised but neither array was '
                              'relocated, so arrayA[210]/[211] alias arrayB[0]/[1]. '
                              'Playing any song as the first of a credit hides song '
                              '210. Patch to fix.')
                self.status.setText(
                    f'ceiling {n}, arrayB={moved}, arrayA={a_moved}')

            self._run(task, ok, 'Reading + decrypting…')

        def _backup(self):
            img = self._img()
            if not img:
                return
            dest = appconfig.pick_save(self, 'taiko_backup', 'Back up taiko to',
                                       'taiko.bin')
            if not dest:
                return

            def task(log):
                data = read_from_hdd(img)
                Path(dest).write_bytes(data)
                return len(data)

            self._run(task, lambda n: self._say(f'backed up {n:,} B -> {dest}'),
                      'Extracting /taiko…')

        def _patch(self):
            img = self._img()
            if not img:
                return
            n = self.sp.value()
            extra = ''
            if n > MAX_SONGS:
                extra = (
                    f'\n\n⚠ {n} is above the proven ceiling of {MAX_SONGS}. This '
                    f'uses the EXPERIMENTAL relocate-arrayA path: songs display and '
                    f'play, but the select screen still walks a few 210-entry tables '
                    f'that are not yet enlarged, so the wheel may glitch or crash at '
                    f'a high count. Test on a COPY of the image and raise the number '
                    f'gradually to find where it breaks.')
            if QMessageBox.warning(
                    self, 'Patch taiko',
                    f'Raise the song ceiling to {n} in {Path(img).name}?\n\n'
                    f'The image is modified in place. Make sure you have a backup '
                    f'of `taiko` (and that PCSX2 is closed). Continue?{extra}',
                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            self._run(lambda log: patch_hdd(img, n, log=log),
                      lambda r: self._done_patch(r), f'Patching to {n} songs…')

        def _done_patch(self, r):
            self._say('')
            self._say(f'DONE — ceiling {r["songs"]}, {r["size"]:,} B, md5 {r["md5"]}')
            self.status.setText(f'patched to {r["songs"]} songs ✓')

        def _restore(self):
            img = self._img()
            if not img:
                return
            src = appconfig.pick_open(self, 'taiko_backup',
                                      'Restore taiko from (your backup)')
            if not src:
                return
            self._run(lambda log: restore_hdd(img, src, log=log),
                      lambda r: self.status.setText('restored ✓'),
                      'Restoring /taiko…')

        # -- worker plumbing --
        def _say(self, s):
            self.log.appendPlainText(s)

        def _run(self, fn, on_ok, msg):
            self._say(f'--- {msg}')
            for b in (self.b_check, self.b_backup, self.b_patch, self.b_restore):
                b.setEnabled(False)
            w = _Worker(fn)
            # Hold a ref until finished() — `done` is emitted from run(), so the
            # QThread is still winding down when the callback lands; dropping the
            # last ref there aborts with "QThread: Destroyed while thread is
            # still running".
            self._workers.append(w)

            def finished():
                w.deleteLater()
                try:
                    self._workers.remove(w)
                except ValueError:
                    pass
                for b in (self.b_check, self.b_backup, self.b_patch, self.b_restore):
                    b.setEnabled(True)

            def done(r):
                if isinstance(r, tuple) and r and r[0] == 'ERROR':
                    self._say(f'FAILED: {r[1]}')
                    QMessageBox.critical(self, 'Patch taiko', str(r[1]))
                    self.status.setText('failed — image untouched')
                    return
                on_ok(r)

            w.line.connect(self._say)
            w.done.connect(done)
            w.finished.connect(finished)
            w.start()

        def done(self, r):
            for w in list(self._workers):
                if w.isRunning():
                    w.wait()
            super().done(r)

    class TaikoTimerDialog(QDialog):
        """Set the song-select inactivity timer (曲選択タイマー) in `taiko`."""

        def __init__(self, parent=None, default_img=''):
            super().__init__(parent)
            self.setWindowTitle('Song-select timer (T14+)')
            self.resize(680, 380)
            self._workers: list[_Worker] = []
            self._build_ui()
            if default_img:
                self.ed_img.setText(default_img)

        def _build_ui(self):
            lay = QVBoxLayout(self)

            row = QHBoxLayout()
            b = QPushButton('HDD .img…'); b.clicked.connect(self._pick)
            self.ed_img = QLineEdit(); self.ed_img.setReadOnly(True)
            row.addWidget(b); row.addWidget(self.ed_img, 1)
            lay.addLayout(row)

            row = QHBoxLayout()
            row.addWidget(QLabel('Seconds:'))
            self.sp = QSpinBox()
            self.sp.setRange(TIMER_MIN, TIMER_MAX)
            self.sp.setValue(TIMER_MAX)
            self.sp.setSuffix(' s')
            self.sp.setToolTip(
                f'Song-select countdown, {TIMER_MIN}..{TIMER_MAX} seconds '
                f'(stock {TIMER_DEFAULT}). Applies to all three select-flow '
                f'inactivity timers. When it hits 0 the game returns to the '
                f'attract/demo loop.')
            row.addWidget(self.sp)
            for label, val in (('120 (stock)', 120), ('300', 300), ('999 (max)', 999)):
                pb = QPushButton(label)
                pb.clicked.connect(lambda _=0, v=val: self.sp.setValue(v))
                row.addWidget(pb)
            row.addStretch(1)
            lay.addLayout(row)

            row = QHBoxLayout()
            self.b_check = QPushButton('Check'); self.b_check.clicked.connect(self._check)
            self.b_backup = QPushButton('Back up taiko…'); self.b_backup.clicked.connect(self._backup)
            self.b_patch = QPushButton('Apply'); self.b_patch.clicked.connect(self._patch)
            self.b_restore = QPushButton('Restore…'); self.b_restore.clicked.connect(self._restore)
            for w in (self.b_check, self.b_backup, self.b_patch, self.b_restore):
                row.addWidget(w)
            lay.addLayout(row)

            self.log = QPlainTextEdit(); self.log.setReadOnly(True)
            self.log.setStyleSheet('font-family: Consolas, monospace;')
            lay.addWidget(self.log, 1)

            self.status = QLabel(
                f'Stock is {TIMER_DEFAULT} s. Sets all three select-flow countdowns. '
                f'Close PCSX2 first — it locks the .img. Back up `taiko` before '
                f'your first patch.')
            self.status.setWordWrap(True)
            self.status.setStyleSheet('color:#999;')
            lay.addWidget(self.status)

        def _pick(self):
            p = appconfig.pick_open(self, 'hddimg', 'Open PS2 HDD image',
                                    'HDD image (*.img *.raw *.bin);;All files (*)')
            if p:
                self.ed_img.setText(p)

        def _img(self):
            p = self.ed_img.text().strip()
            if not p:
                QMessageBox.information(self, 'Song-select timer', 'Pick an HDD image first.')
                return None
            return p

        def _check(self):
            img = self._img()
            if not img:
                return

            def task(log):
                enc = read_from_hdd(img)
                log(f'read /taiko: {len(enc):,} B')
                dec = decrypt_taiko(enc, progress=lambda s, n: None)
                return select_timer(dec)

            def ok(secs):
                self._say(f'current song-select timer = {secs} s'
                          f'{" (stock)" if secs == TIMER_DEFAULT else ""}')
                self.status.setText(f'current timer = {secs} s')

            self._run(task, ok, 'Reading + decrypting…')

        def _backup(self):
            img = self._img()
            if not img:
                return
            dest = appconfig.pick_save(self, 'taiko_backup', 'Back up taiko to', 'taiko.bin')
            if not dest:
                return
            self._run(lambda log: (Path(dest).write_bytes(read_from_hdd(img)) or
                                   Path(dest).stat().st_size),
                      lambda n: self._say(f'backed up {n:,} B -> {dest}'),
                      'Extracting /taiko…')

        def _patch(self):
            img = self._img()
            if not img:
                return
            n = self.sp.value()
            if QMessageBox.warning(
                    self, 'Song-select timer',
                    f'Set the song-select timer to {n} s in {Path(img).name}?\n\n'
                    f'The image is modified in place. Make sure PCSX2 is closed and '
                    f'you have a backup of `taiko`. Continue?',
                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            self._run(lambda log: patch_hdd_timer(img, n, log=log),
                      self._done_patch, f'Setting timer to {n} s…')

        def _done_patch(self, r):
            self._say('')
            self._say(f'DONE — timer {r["was"]} -> {r["seconds"]} s, md5 {r["md5"]}')
            self.status.setText(f'timer set to {r["seconds"]} s ✓')

        def _restore(self):
            img = self._img()
            if not img:
                return
            src = appconfig.pick_open(self, 'taiko_backup', 'Restore taiko from (your backup)')
            if not src:
                return
            self._run(lambda log: restore_hdd(img, src, log=log),
                      lambda r: self.status.setText('restored ✓'), 'Restoring /taiko…')

        # -- worker plumbing (same pattern as TaikoExeDialog) --
        def _say(self, s):
            self.log.appendPlainText(s)

        def _run(self, fn, on_ok, msg):
            self._say(f'--- {msg}')
            for b in (self.b_check, self.b_backup, self.b_patch, self.b_restore):
                b.setEnabled(False)
            w = _Worker(fn)
            self._workers.append(w)

            def finished():
                w.deleteLater()
                try:
                    self._workers.remove(w)
                except ValueError:
                    pass
                for b in (self.b_check, self.b_backup, self.b_patch, self.b_restore):
                    b.setEnabled(True)

            def done(r):
                if isinstance(r, tuple) and r and r[0] == 'ERROR':
                    self._say(f'FAILED: {r[1]}')
                    QMessageBox.critical(self, 'Song-select timer', str(r[1]))
                    self.status.setText('failed — image untouched')
                    return
                on_ok(r)

            w.line.connect(self._say)
            w.done.connect(done)
            w.finished.connect(finished)
            w.start()

        def done(self, r):
            for w in list(self._workers):
                if w.isRunning():
                    w.wait()
            super().done(r)


if __name__ == '__main__':
    import argparse
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('img', help='PS2 HDD .img (close PCSX2 first — it locks the file)')
    ap.add_argument('-n', '--songs', type=int, default=212,
                    help=f'song ceiling, {BASE_SONGS}..{MAX_SONGS_BIG} (default 212). '
                         f'<= {MAX_SONGS} = proven (relocate arrayB); > {MAX_SONGS} = '
                         f'experimental (relocate arrayA, see --help notes)')
    ap.add_argument('--partition', default=PARTITION)
    ap.add_argument('--backup', metavar='FILE',
                    help='write the untouched `taiko` here before patching')
    ap.add_argument('--select-timer', type=int, metavar='SECONDS',
                    help=f'set the song-select inactivity timer ({TIMER_MIN}..'
                         f'{TIMER_MAX} s, stock {TIMER_DEFAULT}) instead of '
                         f'patching the song ceiling')
    ap.add_argument('--restore', metavar='FILE',
                    help='write this `taiko` back instead of patching')
    ap.add_argument('--dry-run', action='store_true',
                    help='decrypt + patch + verify, but do not touch the image')
    a = ap.parse_args()

    try:
        if a.restore:
            restore_hdd(a.img, a.restore, a.partition)
        elif a.select_timer is not None:
            if a.backup:
                Path(a.backup).write_bytes(read_from_hdd(a.img, a.partition))
                print(f'backup -> {a.backup}')
            patch_hdd_timer(a.img, a.select_timer, a.partition)
        elif a.dry_run:
            enc = read_from_hdd(a.img, a.partition)
            dec = decrypt_taiko(enc)
            print(f'current ceiling = {song_count(dec)}; '
                  f'arrayB relocated = {is_patched(dec)}')
            out = patch_arrayb(dec, a.songs)
            re_enc = encrypt_taiko(out)
            print(f'\nround-trip exact: {decrypt_taiko(re_enc) == out}   '
                  f'size unchanged: {len(re_enc) == len(enc)}')
            print('dry run — image untouched')
        else:
            if a.backup:
                Path(a.backup).write_bytes(read_from_hdd(a.img, a.partition))
                print(f'backup -> {a.backup}')
            patch_hdd(a.img, a.songs, a.partition)
    except TaikoExeError as exc:
        sys.exit(f'error: {exc}')

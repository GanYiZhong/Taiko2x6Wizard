"""Grow the per-song record tables in Taiko 14's `taiko` past 210 songs.

The arrayA relocation (taiko_exe.patch_arraya) lets the select screen *show*
more than 210 songs, but the music-data block the game keeps per song is still
a fixed 210-entry table, so every song id >= 210 reads and writes past it.
Reversed from the decrypted `taiko` (all addresses are VAs):

  ctx = operator new(SIZE) at 0x1012D0 (the select/music context, dword_3A6638)
  obj = ctx + 0xB58 (16040 bytes, "MusicData" + rankings)

  * per-song records: 68 bytes each at obj+0x9C + 68*id, 210 of them, ending
    at obj+0x3864 where the next table starts. Fields: +0 play count, +1/+2
    category / popularity rank (bytes), +4.. per-difficulty counters (u16),
    +12+16*d best score (u32) and +16+16*d a 4-byte name.
  * obj (with the 152-byte header in front of it, from ctx+2752) is the SRAM
    save image: 16320 bytes, two copies at SRAM+0x10 and SRAM+0x4000, loaded
    by sub_132428 (jal sub_17CC50) and saved by sub_133250 (jal sub_17CC88 =
    checksum, then copy out). The 32 KB SRAM is full, so the table cannot grow
    in place.
  * the popularity ranking (sub_131918) counts the last 100 plays per mode
    into a 210-int STACK array indexed by the played song id (a byte) - any
    played id >= 212 overwrites the saved registers of that frame.

What this patch does (the song ceiling N comes from the arrayA patch):

  1. Moves the record table to ctx+REC (REC = after arrayA's N words) and
     grows the ctx allocation to hold N records. All 17 access sites keep
     their shape; only the immediate that anchors them moves (each site was
     checked to use its base register for record access only).
  2. Raises the 210 loop bounds in the init / sort routines to N, and the
     sort vector's reserved capacity to N.
  3. SRAM stays byte-compatible: a code cave keeps the first 210 records in
     their old place for the save image. On load the saved 210 are copied
     into the new table and songs 210..N-1 get the same defaults the game's
     own init writes; before each save (and the first-boot SRAM init) the
     first 210 are copied back. Songs past 210 play and record normally, but
     their records do not survive a reboot (there is no SRAM room).
  4. Enlarges sub_131918's stack frame by 0x100 so the ranking counts ids up
     to 254, and stores 0xFF (= empty) in the play history for ids >= 255
     instead of a truncated byte that would credit another song.

Limits: N <= MAX_SONGS (the ctx allocation is an unsigned 16-bit immediate).
The rank byte (+2) still wraps past 255 songs, which only affects ordering
of the "popular" list, not memory safety.
"""
from __future__ import annotations

import hashlib
import struct

DELTA = 0xFF000                      # decrypted taiko: VA = file_off + DELTA

ARRAYA_NEW = 0x4A90                  # see taiko_exe.ARRAYA_NEW
OBJ = 0xB58                          # obj = ctx + OBJ
SAVE_BASE = 2752                     # ctx + 2752 = SRAM image (a0 at the hooks)
OLD_REC = OBJ + 0x9C                 # ctx + 3060: record 0 in the stock layout
REC_SIZE = 68
STOCK_SONGS = 210
OLD_TABLE = REC_SIZE * STOCK_SONGS   # 14280 bytes
MAX_SONGS = (0xFFFF - ARRAYA_NEW) // (4 + REC_SIZE) - 1     # 644

ALLOC_VA = 0x001012D0                # li $a0, SIZE (addiu)  -> ori $a0, $zero, SIZE

# Sites whose 16-bit immediate anchors a record access: (VA, stock word).
# New immediate = stock immediate + (REC - OLD_REC).
IMM_SITES = [
    (0x001311D8, 0x24910090),   # sub_1311C8  addiu $s1,$a0,0x90     init loop base
    (0x00131330, 0x267000A8),   # sub_1312D8  addiu $s0,$s3,0xA8     score defaults
    (0x00131550, 0x25AD0090),   # sub_131490  addiu $t5,0x90         sort: push
    (0x00131688, 0x25EF0090),   # sub_131490  addiu $t7,0x90         sort: write rank
    (0x00131718, 0x25F00090),   # sub_131490  addiu $s0,$t7,0x90     sort: read count
    (0x0013174C, 0x904E009C),   # sub_131490  lbu $t6,0x9C($v0)      sort: linked song
    (0x0013626C, 0x25F10074),   # sub_136188  addiu $s1,$t7,0x74     after-play update
    (0x00136864, 0x26490B58),   # sub_1367E8  addiu $t1,$s2,0xB58    obj (records only)
    (0x00136C88, 0x265E0B58),   # sub_136BE0  addiu $fp,$s2,0xB58    best score update
    (0x00138DD4, 0x249E0B58),   # sub_138DB0  addiu $fp,$a0,0xB58    popular list
    (0x00141D00, 0x25D60B58),   # sub_141B90  addiu $s6,$t6,0xB58    bookkeeping print
    (0x0025C29C, 0x25D20B58),   # sub_25C270  addiu $s2,$t6,0xB58    select UI
    (0x0025C3C8, 0x26310B58),   # sub_25C388
    (0x0025C4F4, 0x26310B58),   # sub_25C4B8
    (0x00263BBC, 0x25D20B58),   # sub_263B90
    (0x00263CE8, 0x26310B58),   # sub_263CA8
    (0x00263E14, 0x26310B58),   # sub_263DD8
]
# Loop bounds / counts of 210 -> N.
BOUND_SITES = [
    (0x00131240, 0x2A4F00D2),   # sub_1311C8  slti $t7,$s2,210
    (0x00131394, 0x2ACF00D2),   # sub_1312D8  slti $t7,$s6,210
    (0x00131554, 0x2A6A00D2),   # sub_131490  slti $t2,$s3,210
]
# Reserved capacity of a 12-byte-element vector that is then filled WITHOUT a
# growth check: (count site, bytes site). Raised to at least what is pushed;
# a larger value from an earlier patch is kept.
SORT_VECTOR = ((0x001314B8, 0x240F00D2), (0x001314CC, 0x240409D8))   # sub_131490
RANK_VECTOR = ((0x00131970, 0x240F00D2), (0x00131984, 0x240409D8))   # sub_131918

# sub_131918 (popularity ranking): (VA, stock word, new low 16 bits)
RANK_FUNC = (0x00131918, 0x00131B68)
RANK_IDS = 255                      # history bytes 0..254 (0xFF = empty)
RANK_SITES = [
    (0x00131948, 0x240F00D1, RANK_IDS - 1),   # clear 255 counters (was 210)
    (0x00131A28, 0x294B00D2, RANK_IDS),       # push 255 entries
]
RANK_GROW = 0x100                   # frame 0x3B0 -> 0x4B0; locals >= 0x350 move up
RANK_LOCALS = 0x350

# Hooks: (VA, stock word, cave label)
HOOKS = [
    (0x001327AC, 0x0C05F314, 'load'),   # sub_132428: jal sub_17CC50 (SRAM copy A)
    (0x001327BC, 0x0C05F314, 'load'),   # sub_132428: jal sub_17CC50 (SRAM copy B)
    (0x00132674, 0x0C05F322, 'save'),   # sub_132428: first-boot init, jal sub_17CC88
    (0x001332B8, 0x0C05F322, 'save'),   # sub_133250: periodic save, jal sub_17CC88
    (0x001318C0, 0x0C04E4FA, 'hist'),   # sub_1318A0: jal sub_1393E8 (history id)
]
F_LOAD = 0x0017CC50                 # copy SRAM image into ctx+2752
F_SAVE = 0x0017CC88                 # checksum ctx+2752 (then the caller copies out)
F_HIST = 0x001393E8                 # musicID -> history byte
F_MEMSET = 0x002042F8               # memset(dst, val, n)
F_MEMCPY = 0x00204144               # memcpy(dst, src, n)
DEF_SCORES = 0x002B9FC0             # u32[4] default best score per difficulty
DEF_NAME_GP = -0x6F18               # gp-relative default name (every 2nd byte)

# Code cave: sub_214C30, a libc routine nothing references (no call, jump,
# pointer or lui/addiu pair anywhere in the image).
CAVE_VA = 0x00214C30
CAVE_LEN = 904
CAVE_SHA1 = 'fd1efe8fc29261659b3bfb29cd737df51fc7028a'


class SongTablesError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
#  a tiny R5900 encoder (just what the cave needs)
# --------------------------------------------------------------------------- #
R = {n: i for i, n in enumerate(
    'zero at v0 v1 a0 a1 a2 a3 t0 t1 t2 t3 t4 t5 t6 t7 '
    's0 s1 s2 s3 s4 s5 s6 s7 t8 t9 k0 k1 gp sp fp ra'.split())}


def _i(op, rs, rt, imm):
    return (op << 26) | (R[rs] << 21) | (R[rt] << 16) | (imm & 0xFFFF)


def _r(rs, rt, rd, funct, sa=0):
    return (R[rs] << 21) | (R[rt] << 16) | (R[rd] << 11) | (sa << 6) | funct


class _Asm:
    def __init__(self, base):
        self.base = base
        self.words = []
        self.labels = {}
        self.fix = []                       # (index, label) for branches

    def pc(self):
        return self.base + 4 * len(self.words)

    def label(self, name):
        self.labels[name] = self.pc()

    def emit(self, w):
        self.words.append(w & 0xFFFFFFFF)

    def nop(self):                  self.emit(0)
    def addiu(self, rt, rs, imm):   self.emit(_i(0x09, rs, rt, imm))
    def ori(self, rt, rs, imm):     self.emit(_i(0x0D, rs, rt, imm))
    def lui(self, rt, imm):         self.emit(_i(0x0F, 'zero', rt, imm))
    def sltiu(self, rt, rs, imm):   self.emit(_i(0x0B, rs, rt, imm))
    def lw(self, rt, off, rs):      self.emit(_i(0x23, rs, rt, off))
    def sw(self, rt, off, rs):      self.emit(_i(0x2B, rs, rt, off))
    def lbu(self, rt, off, rs):     self.emit(_i(0x24, rs, rt, off))
    def sb(self, rt, off, rs):      self.emit(_i(0x28, rs, rt, off))
    def ld(self, rt, off, rs):      self.emit(_i(0x37, rs, rt, off))
    def sd(self, rt, off, rs):      self.emit(_i(0x3F, rs, rt, off))
    def move(self, rd, rs):         self.emit(_r(rs, 'zero', rd, 0x2D))   # daddu
    def jr(self, rs):               self.emit(_r(rs, 'zero', 'zero', 0x08))
    def jal(self, target):          self.emit((0x03 << 26) | ((target >> 2) & 0x3FFFFFF))
    def j(self, target):            self.emit((0x02 << 26) | ((target >> 2) & 0x3FFFFFF))

    def _br(self, word, label):
        self.fix.append((len(self.words), label))
        self.emit(word)

    def bnez(self, rs, label):      self._br(_i(0x05, rs, 'zero', 0), label)
    def bgtz(self, rs, label):      self._br(_i(0x07, rs, 'zero', 0), label)
    def bgez(self, rs, label):      self._br((0x01 << 26) | (R[rs] << 21) | (1 << 16), label)

    def finish(self):
        for idx, label in self.fix:
            off = (self.labels[label] - (self.base + 4 * idx + 4)) >> 2
            if not -0x8000 <= off < 0x8000:
                raise SongTablesError(f'branch to {label} out of range')
            self.words[idx] = (self.words[idx] & 0xFFFF0000) | (off & 0xFFFF)
        return b''.join(struct.pack('<I', w) for w in self.words)


def _cave(songs: int, rec: int) -> tuple[bytes, dict]:
    """Assemble the three cave routines. Returns (code, {label: VA})."""
    a = _Asm(CAVE_VA)
    new_off = rec - SAVE_BASE           # a0 + new_off = ctx + REC
    old_off = OLD_REC - SAVE_BASE       # a0 + 308     = old record 0
    total = REC_SIZE * songs

    # cave_load(a0 = ctx+2752, a1 = SRAM buffer): load, then rebuild the table
    a.label('load')
    a.addiu('sp', 'sp', -0x30)
    a.sd('ra', 0x20, 'sp'); a.sd('s0', 0x00, 'sp'); a.sd('s1', 0x08, 'sp')
    a.jal(F_LOAD)
    a.move('s0', 'a0')                                  # delay: keep ctx+2752
    a.addiu('s1', 's0', new_off)                        # s1 = new table
    a.move('a0', 's1'); a.move('a1', 'zero')
    a.jal(F_MEMSET)
    a.ori('a2', 'zero', total)                          # delay
    a.move('a0', 's1'); a.addiu('a1', 's0', old_off)
    a.jal(F_MEMCPY)
    a.ori('a2', 'zero', OLD_TABLE)                      # delay
    # defaults for records 210..N-1 (what sub_1311C8 / sub_1312D8 write)
    a.ori('t0', 'zero', OLD_TABLE)
    a.emit(_r('s1', 't0', 't0', 0x2D))                  # daddu t0, s1, t0
    a.ori('t1', 'zero', songs - STOCK_SONGS)
    a.label('rec')
    a.addiu('t2', 'zero', 0xFF)
    a.sb('t2', 1, 't0'); a.sb('t2', 2, 't0')            # category / rank: none
    a.lui('t3', (DEF_SCORES + 0x8000) >> 16)
    a.addiu('t3', 't3', DEF_SCORES & 0xFFFF)
    a.move('t5', 't0')
    a.addiu('t6', 'zero', 3)
    a.label('diff')
    a.lw('t7', 0, 't3'); a.sw('t7', 12, 't5')           # default best score
    a.addiu('t4', 'gp', DEF_NAME_GP)
    for k in range(4):                                  # default 4-char name
        a.lbu('t8', 2 * k, 't4'); a.sb('t8', 16 + k, 't5')
    a.addiu('t3', 't3', 4)
    a.addiu('t6', 't6', -1)
    a.bgez('t6', 'diff')
    a.addiu('t5', 't5', 16)                             # delay
    a.addiu('t1', 't1', -1)
    a.bgtz('t1', 'rec')
    a.addiu('t0', 't0', REC_SIZE)                       # delay
    a.ld('s1', 0x08, 'sp'); a.ld('s0', 0x00, 'sp'); a.ld('ra', 0x20, 'sp')
    a.jr('ra')
    a.addiu('sp', 'sp', 0x30)                           # delay

    # cave_save(a0 = ctx+2752): first 210 back into the save image, then checksum
    a.label('save')
    a.addiu('sp', 'sp', -0x20)
    a.sd('ra', 0x10, 'sp'); a.sd('s0', 0x00, 'sp')
    a.move('s0', 'a0')
    a.addiu('a0', 's0', old_off)
    a.addiu('a1', 's0', new_off)
    a.jal(F_MEMCPY)
    a.ori('a2', 'zero', OLD_TABLE)                      # delay
    a.move('a0', 's0')
    a.ld('s0', 0x00, 'sp'); a.ld('ra', 0x10, 'sp')
    a.j(F_SAVE)                                         # tail call
    a.addiu('sp', 'sp', 0x20)                           # delay

    # cave_hist(a0, a1): history byte for a song id, 0xFF (= empty) if >= 255
    a.label('hist')
    a.addiu('sp', 'sp', -0x10)
    a.sd('ra', 0, 'sp')
    a.jal(F_HIST)
    a.nop()
    a.sltiu('t0', 'v0', 0xFF)
    a.bnez('t0', 'hist_ok')
    a.ld('ra', 0, 'sp')                                 # delay (harmless both ways)
    a.addiu('v0', 'zero', 0xFF)
    a.label('hist_ok')
    a.jr('ra')
    a.addiu('sp', 'sp', 0x10)                           # delay

    code = a.finish()
    if len(code) > CAVE_LEN:
        raise SongTablesError(f'cave code {len(code)} B > {CAVE_LEN} B')
    return code, {k: a.labels[k] for k in ('load', 'save', 'hist')}


# --------------------------------------------------------------------------- #
#  layout + patch
# --------------------------------------------------------------------------- #
def layout(songs: int) -> dict:
    """ctx offsets for a ceiling of `songs`."""
    rec = (ARRAYA_NEW + 4 * songs + 15) & ~15
    return {'rec': rec, 'delta': rec - OLD_REC, 'alloc': rec + REC_SIZE * songs}


def _w(buf, va):
    return struct.unpack_from('<I', buf, va - DELTA)[0]


def _put(buf, va, word):
    struct.pack_into('<I', buf, va - DELTA, word & 0xFFFFFFFF)


def _lo(word):
    return word & 0xFFFF


def current_delta(dec: bytes):
    """0 for a stock table, the applied shift if patched, None if mixed or
    if any site is not the expected instruction (another build)."""
    deltas = set()
    for va, stock in IMM_SITES:
        w = _w(dec, va)
        if w >> 16 != stock >> 16:
            return None
        deltas.add(_lo(w) - _lo(stock))
    return deltas.pop() if len(deltas) == 1 else None


def is_patched(dec: bytes) -> bool:
    return bool(current_delta(dec))


def _grow_vector(buf, sites, need):
    """Make a pre-reserved vector hold at least `need` 12-byte elements."""
    (cva, cstock), (bva, bstock) = sites
    cw, bw = _w(buf, cva), _w(buf, bva)
    if cw >> 16 != cstock >> 16 or bw >> 16 != bstock >> 16:
        raise SongTablesError(f'{cva:08X}/{bva:08X}: unexpected vector setup')
    count = max(_lo(cw), need)
    if 12 * count >= 0x8000:
        raise SongTablesError(f'{cva:08X}: vector of {count} does not fit')
    _put(buf, cva, (cstock & 0xFFFF0000) | count)
    _put(buf, bva, (bstock & 0xFFFF0000) | (12 * count))
    return count


def _rank_sp_sites(dec):
    """Every $sp-based instruction in sub_131918 whose offset is a local
    (>= RANK_LOCALS), plus the two frame adjustments."""
    lo, hi = RANK_FUNC
    out = []
    for va in range(lo, hi, 4):
        w = _w(dec, va)
        op, rs, imm = w >> 26, (w >> 21) & 31, w & 0xFFFF
        if rs != R['sp']:
            continue
        if op == 0x09 and (w >> 16) & 31 == R['sp']:           # addiu sp,sp,+-F
            out.append((va, 'frame'))
        elif op in (0x09, 0x23, 0x2B, 0x37, 0x3F) and imm >= RANK_LOCALS and imm < 0x8000:
            out.append((va, 'local'))
    return out


def patch_records(dec: bytes, songs: int, log=print) -> bytes:
    """Relocate and grow the per-song record tables for `songs` songs.

    Requires the arrayA relocation (taiko_exe.patch_arraya) for the same
    ceiling. Idempotent and re-runnable with a different ceiling.
    """
    if not STOCK_SONGS < songs <= MAX_SONGS:
        raise SongTablesError(f'songs must be {STOCK_SONGS + 1}..{MAX_SONGS}')
    buf = bytearray(dec)
    lay = layout(songs)
    delta = lay['delta']

    cur = current_delta(buf)
    if cur is None:
        raise SongTablesError('record sites are not the expected instructions '
                              '(another build, or partly patched) - refusing')
    if cur < 0:
        raise SongTablesError('record sites hold an unexpected shift - refusing')
    for va, stock in IMM_SITES:
        new = (stock & 0xFFFF0000) | (_lo(stock) + delta)
        if _lo(stock) + delta >= 0x8000:
            raise SongTablesError(f'{va:08X}: offset overflows a 16-bit immediate')
        _put(buf, va, new)
    log(f'{len(IMM_SITES)} record sites  +{delta:#x}  (table at ctx+{lay["rec"]:#x})')

    for va, stock in BOUND_SITES:
        w = _w(buf, va)
        if w >> 16 != stock >> 16:
            raise SongTablesError(f'{va:08X}: unexpected instruction {w:08x}')
        _put(buf, va, (stock & 0xFFFF0000) | songs)
    cap = _grow_vector(buf, SORT_VECTOR, songs)
    log(f'init / sort loops 210 -> {songs}, sort vector capacity {cap}')

    # ranking frame (independent of N; skip if already done)
    if _w(buf, RANK_FUNC[0]) == 0x27BDFC50:                 # addiu sp,-0x3B0
        for va, stock, new in RANK_SITES:
            if _w(buf, va) != stock:
                raise SongTablesError(f'{va:08X}: unexpected instruction')
            _put(buf, va, (stock & 0xFFFF0000) | new)
        _grow_vector(buf, RANK_VECTOR, RANK_IDS)
        for va, kind in _rank_sp_sites(buf):
            w = _w(buf, va)
            imm = w & 0xFFFF
            if kind == 'frame':
                imm = (imm - RANK_GROW) & 0xFFFF if imm & 0x8000 else imm + RANK_GROW
            else:
                imm += RANK_GROW
            _put(buf, va, (w & 0xFFFF0000) | imm)
        log('ranking: counts song ids 0..254 (stack frame +0x100)')
    elif _w(buf, RANK_FUNC[0]) != 0x27BDFB50:               # addiu sp,-0x4B0
        raise SongTablesError(f'{RANK_FUNC[0]:08X}: unexpected prologue')

    # code cave + hooks
    cave = bytes(buf[CAVE_VA - DELTA: CAVE_VA - DELTA + CAVE_LEN])
    code, labels = _cave(songs, lay['rec'])
    if hashlib.sha1(cave).hexdigest() != CAVE_SHA1 and cur == 0:
        raise SongTablesError(f'{CAVE_VA:08X}: cave area is not the stock routine')
    buf[CAVE_VA - DELTA: CAVE_VA - DELTA + CAVE_LEN] = code + bytes(CAVE_LEN - len(code))
    for va, stock, label in HOOKS:
        w = _w(buf, va)
        want = 0x0C000000 | ((labels[label] >> 2) & 0x3FFFFFF)
        if w not in (stock, want):
            raise SongTablesError(f'{va:08X}: unexpected call {w:08x}')
        _put(buf, va, want)
    log(f'cave {len(code)} B at {CAVE_VA:08X}: SRAM load/save sync of the first '
        f'{STOCK_SONGS} records, history clamp')

    # ctx allocation: ori $a0,$zero,SIZE (unsigned)
    w = _w(buf, ALLOC_VA)
    if w >> 16 not in (0x2404, 0x3404):
        raise SongTablesError(f'{ALLOC_VA:08X}: unexpected allocation {w:08x}')
    _put(buf, ALLOC_VA, 0x34040000 | lay['alloc'])
    log(f'ctx allocation -> {lay["alloc"]:#x} B')
    return bytes(buf)


def alloc_owned(dec: bytes) -> bool:
    """True once this patch owns the ctx allocation (ori form)."""
    return _w(dec, ALLOC_VA) >> 16 == 0x3404

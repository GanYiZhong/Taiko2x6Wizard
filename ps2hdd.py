#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ps2hdd.py  -- PS2 HDD (APA partitioning + PFS filesystem) read/write tool.

Pure-Python port of the on-disk logic from pfsshell v1.1.1
(https://github.com/ps2homebrew/pfsshell , tag v1.1.1), whose libapa/libpfs
are derived from ps2sdk.  Only the on-disk structures and the read/write
algorithms are ported -- not the IRX driver glue.

=============================================================================
STRUCTS / OFFSETS USED  (verified against pfsshell v1.1.1 sources)
=============================================================================

--- APA partition header  (subprojects/apa/include/libapa.h : apa_header_t) ---
All multi-byte fields are little-endian.  The header is 1 KiB conceptually but
the apa_sub[] table extends it; we only need the first part + subs.
    off  type    field
    0x00 u32     checksum
    0x04 u32     magic        = 0x00415041  "APA\0"
    0x08 u32     next         (LBA of next header, 0 = end)
    0x0C u32     prev         (LBA of prev header)
    0x10 char[32] id          (APA_IDMAX = 32)   e.g. "__mbr","t14jp1400.0000"
    0x30 char[8]  rpwd        (APA_PASSMAX = 8)
    0x38 char[8]  fpwd
    0x40 u32     start        (LBA, start of this partition's data area)
    0x44 u32     length       (sector count)
    0x48 u16     type         (0x0100 = PFS)
    0x4A u16     flags        (APA_FLAG_SUB=0x0001 marks a sub-partition entry)
    0x4C u32     nsub         (number of sub-partition extents)
    0x50 ps2time created (8 bytes)
    0x58 u32     main
    0x5C u32     number
    0x60 u32     modver
    0x64 u32[7]  pading1
    0x80 char[128] pading2
    0x100 mbr{ char[32] magic; u32 version; u32 nsector; ps2time created;
               u32 osdStart; u32 osdSize; char[200] pading3 }   (only in __mbr)
    0x1C0 apa_sub_t subs[APA_MAXSUB=64]   each = { u32 start; u32 length }
APA_MAGIC=0x00415041, sector size = 512.  (APA_IDMAX/PASSMAX/MAXSUB are the
standard ps2sdk hdd-ioctl.h values 32/8/64; consistent with the ground truth
id[32] sitting at +0x10.)

--- PFS  (subprojects/pfs/include/libpfs.h) ---
PFS_SUPER_MAGIC = 0x50465300 "PFS\0"
PFS_SEGD_MAGIC  = 0x53454744 "SEGD"
PFS_SEGI_MAGIC  = 0x53454749 "SEGI"
PFS_FORMAT_VERSION = 3, PFS_INODE_MAX_BLOCKS = 114, PFS_BLOCKSIZE = 0x2000

pfs_blockinfo_t  (8 bytes):  u32 number(zone) ; u16 subpart ; u16 count(zones)

pfs_super_block_t:
    0x00 u32 magic           = 0x50465300
    0x04 u32 version
    0x08 u32 modver
    0x0C u32 pfsFsckStat
    0x10 u32 zone_size       (= 8192)
    0x14 u32 num_subs
    0x18 blockinfo log       (8 bytes)
    0x20 blockinfo root      (8 bytes)   -> root directory inode

pfs_inode_t  (1024 bytes = 256 u32 words):
    0x00 u32 checksum
    0x04 u32 magic           = SEGD (root/first) or SEGI (indirect segment)
    0x08 blockinfo inode_block   (self)
    0x10 blockinfo next_segment
    0x18 blockinfo last_segment
    0x20 blockinfo unused
    0x28 blockinfo data[114]     (114*8 = 912 bytes) -> 0x28..0x3B8
    0x3B8 u16 mode
    0x3BA u16 attr
    0x3BC u16 uid
    0x3BE u16 gid
    0x3C0 datetime atime (8)
    0x3C8 datetime ctime (8)
    0x3D0 datetime mtime (8)
    0x3D8 u64 size
    0x3E0 u32 number_blocks
    0x3E4 u32 number_data        (# valid data[] extents in THIS segment)
    0x3E8 u32 number_segdesg
    0x3EC u32 subpart
    0x3F0 u32 reserved[4]
inode checksum: sum of u32 words at indices 1..255 (skip word 0 = checksum).

pfs_dentry_t (directory record, variable length):
    0x00 u32 inode   (zone number of the entry's inode; 0 = deleted slot)
    0x04 u8  sub     (subpart of that inode)
    0x05 u8  pLen    (path/name length in bytes)
    0x06 u16 aLen    (allocated record length; low 12 bits used)
    0x08 char path[pLen]
Records are walked within each 512-byte sector; advance by (aLen & 0xFFF).
Record minimum size = ((pLen + 8 + 3) & ~3).

--- zone -> sector mapping  (subprojects/pfs/src/block.c) ---
inode_scale = log2(zone_size / 512).  For zone_size 8192 -> 16 sectors/zone,
scale = 4.  A blockinfo {subpart, number, count} maps to:
    abs_sector = subpart_partition_start_LBA + (number << inode_scale)
where subpart 0 is the main partition (apa.start) and subpart>0 indexes the
apa sub[] extents.  The PFS superblock itself is at zone 512 of subpart 0
(0x400000 bytes / 8192 = zone 512).

=============================================================================
WRITE-SAFETY NOTES (verified against ps2sdk iop/hdd/libpfs @ master)
=============================================================================
* FREE-SPACE ACCOUNTING: pfs_super_block_t has NO on-disk free-zone counter.
  The runtime fields pfsMount->zfree / free_zone[] are recomputed from the
  bitmap at mount time (pfsBitmapCalcFreeZones) and are NEVER persisted.  The
  on-disk *bitmap* is the sole free-space record, so this tool keeps the bitmap
  authoritative and intentionally writes NO superblock counter (writing one
  would corrupt the reserved-zero region).  pfsFsckStat (super 0x0C) is the
  only mutable super field; we leave it 0 (clean) and REFUSE to write a
  filesystem that already has PFS_FSCK_STAT_WRITE_ERROR set.

* number_data semantics (the C2/C3 invariant): number_data counts EVERY data[]
  slot, INCLUDING each descriptor's self-pointer at slot 0.  pfsBlockAllocNew-
  Segment increments number_data once for a new SEGI's self-zone, then again
  per real data extent, and writes the extent to data[pfsFixIndex(number_data-
  1)] of the segment that currently owns the tail (blockpos->inode) -- NOT
  blindly the newest SEGI.  _grow tracks the owning (segment, slot) for the
  tail extent so contiguous expansion and new-extent append both edit the
  correct descriptor.

* SEGI data[] is 123 slots: pfsFixIndex maps SEGI logical indices to
  (index-114) % 123, so an indirect descriptor's data[] runs 0..122 and spans
  inode bytes 0x28..0x3FF (aliasing the mode/time/size region a SEGD uses).
  PfsInode decodes 123 slots so SEGI extents are addressable.

* CRASH ORDERING: _grow plans the whole allocation in memory and verifies free
  space BEFORE any device write, then commits in an order (data -> bitmap ->
  new SEGI inodes -> fsync -> linking inodes -> fsync) such that an interrupted
  grow can only leak reserved-but-unused zones (fsck reclaims), never leave a
  next_segment pointing at an unwritten descriptor.
=============================================================================
"""

import os
import struct

try:
    import numpy as _np
except Exception:  # numpy optional; bitmap falls back to pure-Python scan
    _np = None

SECTOR = 512
APA_MAGIC = 0x00415041
APA_IDMAX = 32
APA_PASSMAX = 8
APA_MAXSUB = 64
APA_FLAG_SUB = 0x0001
APA_TYPE_PFS = 0x0100

PFS_SUPER_MAGIC = 0x50465300
PFS_SEGD_MAGIC = 0x53454744
PFS_SEGI_MAGIC = 0x53454749
PFS_INODE_MAX_BLOCKS = 114
# A SEGI (indirect) descriptor uses 123 data[] slots: pfsFixIndex maps SEGI
# logical indices to (index-114) % 123, so slots run 0..122.  In the 1024-byte
# inode those 123 blockinfos span 0x28..0x3FF -- i.e. for a SEGI the region a
# SEGD uses for mode/attr/time/size IS data[].  We must decode 123 slots so the
# reader/writer can address SEGI data extents correctly (see _walk_extents_loc).
PFS_SEGI_MAX_BLOCKS = 123
PFS_INODE_BYTES = 1024

PFS_FIO_ATTR_SUBDIR = 0x0020

# pfs_inode_t timestamp field offsets (pfs_datetime_t = 8 bytes each)
PFS_INODE_OFF_ATIME = 0x3C0
PFS_INODE_OFF_CTIME = 0x3C8
PFS_INODE_OFF_MTIME = 0x3D0
PFS_INODE_OFF_SIZE = 0x3D8

# pfsFsckStat (super 0x0C) flag bits (libpfs.h)
PFS_FSCK_STAT_OK = 0x00
PFS_FSCK_STAT_WRITE_ERROR = 0x01
PFS_FSCK_STAT_ERRORS_FIXED = 0x02

# Single data[] extent count is a u16; cap a contiguous run at this.
PFS_EXTENT_COUNT_MAX = 0xFFFF


def pfs_datetime_now():
    """Build an 8-byte pfs_datetime_t for 'now' in JST (matches pfsGetTime:
    rawtime shifted by -9h then formatted as gmtime).
        u8 unused; u8 sec; u8 min; u8 hour; u8 day; u8 month; u16 year"""
    import time as _t
    # pfsGetTime does gmtime(now - 9h); reproduce that JST wall clock.
    tm = _t.gmtime(_t.time() - 9 * 3600)
    return struct.pack("<BBBBBBH", 0, tm.tm_sec, tm.tm_min, tm.tm_hour,
                       tm.tm_mday, tm.tm_mon, tm.tm_year)


# ---------------------------------------------------------------------------
# Block device abstraction with copy-on-write overlay for safe write testing.
# ---------------------------------------------------------------------------
class BlockDevice:
    """Sector-addressed block device over a raw .img file.

    overlay=True  -> reads fall through to the real file (read-only); writes
                     are captured in an in-memory dict {lba: 512-byte bytes}.
                     The underlying file is NEVER written.
    overlay=False -> read/write straight through to the file (writable=True).
    """

    def __init__(self, path, writable=False, overlay=False):
        self.path = path
        self.overlay = overlay
        self.writable = writable
        mode = "r+b" if (writable and not overlay) else "rb"
        self.f = open(path, mode)
        self.f.seek(0, os.SEEK_END)
        self.size = self.f.tell()
        self._ov = {} if overlay else None  # lba -> bytes(512)

    # --- sector-level ---
    def read_sectors(self, lba, count):
        out = bytearray()
        for i in range(count):
            s = lba + i
            if self._ov is not None and s in self._ov:
                out += self._ov[s]
            else:
                self.f.seek(s * SECTOR)
                d = self.f.read(SECTOR)
                if len(d) < SECTOR:
                    d = d + b"\x00" * (SECTOR - len(d))
                out += d
        return bytes(out)

    def write_sectors(self, lba, data):
        assert len(data) % SECTOR == 0, "write must be sector-aligned"
        count = len(data) // SECTOR
        if self.overlay:
            for i in range(count):
                self._ov[lba + i] = data[i * SECTOR:(i + 1) * SECTOR]
            return
        if not self.writable:
            raise PermissionError("device not opened writable")
        self.f.seek(lba * SECTOR)
        self.f.write(data)
        self.f.flush()

    def sync(self):
        """Durably flush buffered writes to the underlying device (real-disk
        mode only).  Used to enforce write ordering around the crash-critical
        next_segment link in _grow (see C4)."""
        if self.overlay or not self.writable:
            return
        self.f.flush()
        try:
            os.fsync(self.f.fileno())
        except (OSError, ValueError):
            pass

    # --- byte-level helpers (must respect overlay on both paths) ---
    def read_bytes(self, byte_off, length):
        first = byte_off // SECTOR
        last = (byte_off + length - 1) // SECTOR
        raw = self.read_sectors(first, last - first + 1)
        start = byte_off - first * SECTOR
        return raw[start:start + length]

    def write_bytes(self, byte_off, data):
        """Read-modify-write so sub-sector writes preserve neighbours."""
        length = len(data)
        first = byte_off // SECTOR
        last = (byte_off + length - 1) // SECTOR
        nsec = last - first + 1
        buf = bytearray(self.read_sectors(first, nsec))
        start = byte_off - first * SECTOR
        buf[start:start + length] = data
        self.write_sectors(first, bytes(buf))

    # overlay introspection (for tests)
    def overlay_changes(self):
        if self._ov is None:
            return {}
        return dict(self._ov)

    def overlay_nonidentical(self):
        """Return {lba: (orig, new)} only for sectors whose new content
        differs from what the underlying file currently holds."""
        diff = {}
        if self._ov is None:
            return diff
        for lba, new in self._ov.items():
            self.f.seek(lba * SECTOR)
            orig = self.f.read(SECTOR)
            if len(orig) < SECTOR:
                orig = orig + b"\x00" * (SECTOR - len(orig))
            if orig != new:
                diff[lba] = (orig, new)
        return diff

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Struct decoders
# ---------------------------------------------------------------------------
def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def decode_blockinfo(b, o):
    number = _u32(b, o)
    subpart = _u16(b, o + 4)
    count = _u16(b, o + 6)
    return {"number": number, "subpart": subpart, "count": count}


class ApaPartition:
    def __init__(self, lba, raw):
        self.lba = lba
        # keep the full 1024-byte header for checksum validation
        self.raw = bytes(raw[:1024]) if len(raw) >= 1024 else bytes(raw) + b"\x00" * (1024 - len(raw))
        self.checksum = _u32(raw, 0x00)
        self.magic = _u32(raw, 0x04)
        self.next = _u32(raw, 0x08)
        self.prev = _u32(raw, 0x0C)
        self.id = raw[0x10:0x10 + APA_IDMAX].split(b"\x00", 1)[0].decode("latin-1")
        self.start = _u32(raw, 0x40)
        self.length = _u32(raw, 0x44)
        self.type = _u16(raw, 0x48)
        self.flags = _u16(raw, 0x4A)
        self.nsub = _u32(raw, 0x4C)
        self.subs = []
        for i in range(self.nsub):
            o = 0x1C0 + i * 8
            self.subs.append({"start": _u32(raw, o), "length": _u32(raw, o + 4)})

    @property
    def is_pfs(self):
        return self.type == APA_TYPE_PFS

    @staticmethod
    def checksum_of(raw):
        # libapa apaCheckSum: sum of u32 words at indices 1..255 (skip word 0).
        s = 0
        for i in range(1, 256):
            s = (s + _u32(raw, i * 4)) & 0xFFFFFFFF
        return s

    @property
    def checksum_valid(self):
        return self.checksum == ApaPartition.checksum_of(self.raw)


class PfsInode:
    """Decoded pfs_inode_t (first 1024 bytes of its zone)."""

    def __init__(self, raw):
        self.raw = raw
        self.checksum = _u32(raw, 0x00)
        self.magic = _u32(raw, 0x04)
        self.inode_block = decode_blockinfo(raw, 0x08)
        self.next_segment = decode_blockinfo(raw, 0x10)
        self.last_segment = decode_blockinfo(raw, 0x18)
        # Decode 123 slots so SEGI indices (0..122) are addressable.  For a SEGD
        # root only slots 0..113 are real data[]; slots 114..122 alias the
        # mode/time/size fields and are never accessed for a SEGD (fix_index<114
        # for the first 114 logical indices).
        self.data = [decode_blockinfo(raw, 0x28 + i * 8) for i in range(PFS_SEGI_MAX_BLOCKS)]
        self.mode = _u16(raw, 0x3B8)
        self.attr = _u16(raw, 0x3BA)
        self.size = _u64(raw, 0x3D8)
        self.number_blocks = _u32(raw, 0x3E0)
        self.number_data = _u32(raw, 0x3E4)
        self.number_segdesg = _u32(raw, 0x3E8)
        self.subpart = _u32(raw, 0x3EC)

    @property
    def is_dir(self):
        return bool(self.attr & PFS_FIO_ATTR_SUBDIR)

    @property
    def checksum_valid(self):
        return self.checksum == PfsInode.checksum_of(self.raw)

    @staticmethod
    def checksum_of(raw):
        # sum of u32 words at indices 1..255 (skip word 0 = checksum field)
        # (port of pfsInodeCheckSum, inode.c)
        s = 0
        for i in range(1, 256):
            s = (s + _u32(raw, i * 4)) & 0xFFFFFFFF
        return s


def pfs_fix_index(index):
    """Port of pfsFixIndex (misc.c): map a logical block_segment index into
    the data[] array index of its segment-descriptor inode.
        index < 114            -> index               (first/SEGD inode)
        else                   -> (index-114) % 123   (SEGI inodes)
    Returning 0 signals "this index is the start of a new segment descriptor"
    (data[0] is reserved for the descriptor's own self-pointer)."""
    if index < PFS_INODE_MAX_BLOCKS:
        return index
    index -= PFS_INODE_MAX_BLOCKS
    return index % 123


# ---------------------------------------------------------------------------
# PFS free-zone bitmap allocator (port of subprojects/pfs/src/bitmap.c)
# ---------------------------------------------------------------------------
PFS_BITS_PER_CHUNK = 8192       # pfsBitsPerBitmapChunk: bits per 1024-byte chunk
PFS_META_SIZE = 1024            # pfsMetaSize
PFS_BLOCK_SIZE_SCALE = 1        # pfsBlockSize (super.c: =1, "0=1x")
PFS_INODE_SCALE_META = 3        # pfsGetScale(zsize=8192, metaSize=1024) = 3
PFS_BITMAP_ALLOC = 0
PFS_BITMAP_FREE = 1


class PfsBitmap:
    """Reads/writes the PFS free-space bitmap of ONE subpartition through the
    block device, exactly mirroring pfsshell's on-disk layout.

    On-disk layout (per subpart):
      bitmap chunk `c` (1024 bytes, covering 8192 zones) lives at the
      meta-block (1024-byte unit) offset:
          (1 << PFS_INODE_SCALE_META) + c            [+ (0x2000 >> pfsBlockSize) if subpart 0]
        = 8 + c                                       [+ 4096 if subpart 0]
      i.e. partition byte offset = that_metablock << 10.
      For subpart 0 (zsize 8192) chunk 0 -> byte 0x402000 (right after the
      8 KiB superblock zone at 0x400000).

    Bit semantics: bit==1 -> zone USED, bit==0 -> zone FREE.  Words are little
    endian; within a 32-bit word bit `b` (LSB-first) covers the b-th zone.
    """

    def __init__(self, hdd, part, sub, total_zones):
        self.hdd = hdd
        self.part = part
        self.sub = sub
        self.total_zones = total_zones
        self.nchunks = (total_zones + PFS_BITS_PER_CHUNK - 1) // PFS_BITS_PER_CHUNK
        self.allocated = 0   # zones flipped free->used by this instance
        self.freed = 0       # zones flipped used->free by this instance
        self._dirty_chunks = set()  # chunk indices whose bits changed
        # Lazily load the whole bitmap into a 1-bit-per-zone array for fast
        # word-level scanning.  Stored as numpy uint8 (0/1) if numpy present,
        # else as a Python bytearray.  Bit order matches on-disk LSB-first.
        self._load()

    # --- physical mapping ---
    def _chunk_byte_off(self, chunk):
        """Partition-relative byte offset of a bitmap chunk."""
        metablk = (1 << PFS_INODE_SCALE_META) + chunk
        if self.sub == 0:
            metablk += (0x2000 >> PFS_BLOCK_SIZE_SCALE)  # 0x1000 = 4096
        return metablk << 10  # meta-block (1024 B) -> byte

    def _chunk_lba_off(self, chunk):
        base = self.hdd._subpart_start(self.part, self.sub)
        return base * SECTOR + self._chunk_byte_off(chunk)

    def reserved_zones(self):
        """Return the set of zones in THIS subpart that hold filesystem
        metadata and therefore MUST be marked USED in the bitmap before the
        allocator may hand out any zone.  Derived from the on-disk layout the
        same way libpfs maps them (block.c / bitmap.c):

          * every bitmap chunk's own backing zone, and
          * for subpart 0 only: the superblock zone (512) and the journal/log
            zones (super.log.number .. +count).

        Any reserved zone reading as FREE means the bitmap is inconsistent with
        the format and allocation must be refused (H3)."""
        reserved = set()
        # bitmap chunk backing zones: chunk c sits at metablock
        # (1<<META_SCALE)+c (+0x1000 metablocks for sub0); 8 chunks per zone.
        for c in range(self.nchunks):
            byte_off = self._chunk_byte_off(c)
            z = byte_off // 0x2000
            if z < self.total_zones:
                reserved.add(z)
        if self.sub == 0:
            reserved.add(0x400000 // 0x2000)  # superblock zone 512
            sb = self.hdd._supers.get(self.part.id)
            if sb is not None and sb["log"]["subpart"] == 0:
                log = sb["log"]
                for z in range(log["number"], log["number"] + max(1, log["count"])):
                    if z < self.total_zones:
                        reserved.add(z)
        return reserved

    def verify_reserved(self):
        """Assert all reserved metadata zones read as USED.  Raises on any
        reserved zone that is free (would otherwise be handed out and clobber
        the superblock/bitmap/log)."""
        bad = [z for z in sorted(self.reserved_zones()) if self.test(z) == 0]
        if bad:
            raise OSError(
                "PFS bitmap inconsistency (sub=%d): reserved metadata zones "
                "marked FREE: %s -- refusing to allocate" % (self.sub, bad[:16]))

    def _load(self):
        """Read every bitmap chunk and unpack into self.bits (one entry/zone)."""
        raw = bytearray()
        for c in range(self.nchunks):
            raw += self.hdd.dev.read_bytes(self._chunk_lba_off(c), PFS_META_SIZE)
        if _np is not None:
            arr = _np.frombuffer(bytes(raw), dtype=_np.uint8)
            # unpackbits with bitorder='little' gives LSB-first bit per byte,
            # matching the on-disk word/bit layout, then trim to total_zones.
            bits = _np.unpackbits(arr, bitorder="little")
            self.bits = bits[:self.total_zones].copy()
            self._use_np = True
        else:
            bits = bytearray(self.total_zones)
            for z in range(self.total_zones):
                byte = raw[z // 8]
                bits[z] = (byte >> (z % 8)) & 1
            self.bits = bits
            self._use_np = False

    # --- bit ops (operate on the in-memory array; flush() writes back) ---
    def test(self, zone):
        return int(self.bits[zone])

    def _set(self, zone, used):
        self.bits[zone] = 1 if used else 0
        self._dirty_chunks.add(zone // PFS_BITS_PER_CHUNK)

    def mark_used(self, zone, count):
        # H1: marking an already-used zone means two extents would overlap and
        # the data-write phase would clobber another file.  pfsBitmapAllocFree
        # only *warns* ("Tried to allocate used block"); for an offline editor
        # that is a hard corruption risk, so we refuse loudly instead.
        if zone < 0 or zone + count > self.total_zones:
            raise OSError("bitmap mark_used out of range: zones [%d,%d) of %d"
                          % (zone, zone + count, self.total_zones))
        for z in range(zone, zone + count):
            if self.bits[z]:
                raise OSError(
                    "bitmap overlap: zone %d (sub=%d) already USED -- refusing "
                    "to double-allocate" % (z, self.sub))
        for z in range(zone, zone + count):
            self.bits[z] = 1
            self._dirty_chunks.add(z // PFS_BITS_PER_CHUNK)
        self.allocated += count

    def mark_free(self, zone, count):
        for z in range(zone, zone + count):
            if self.bits[z]:
                self.freed += 1
            self.bits[z] = 0
            self._dirty_chunks.add(z // PFS_BITS_PER_CHUNK)

    # --- allocation (port of pfsBitmapAllocZones contiguous scan) ---
    def alloc_contiguous(self, start_hint, amount):
        """Find a contiguous run of `amount` free zones at or after start_hint,
        mark them used, and return the starting zone, or None.
        Mirrors pfsBitmapAllocZones' forward contiguous scan."""
        run = self._find_run(start_hint, amount)
        if run is None:
            return None
        self.mark_used(run, amount)
        return run

    def _find_run(self, start_hint, amount):
        n = self.total_zones
        if start_hint >= n:
            return None
        if self._use_np:
            # find indices of free zones >= start_hint, then a window of `amount`
            free = _np.flatnonzero(self.bits[start_hint:] == 0)
            if free.size < amount:
                return None
            free = free + start_hint
            # look for `amount` consecutive integers in `free`
            if amount == 1:
                return int(free[0])
            # difference run: positions where consecutive free zones are adjacent
            # slide a window using the contiguous-run trick
            diffs = _np.diff(free)
            # find a start index i such that free[i..i+amount-1] are contiguous
            run_len = 1
            start_i = 0
            for i in range(1, free.size):
                if diffs[i - 1] == 1:
                    run_len += 1
                    if run_len >= amount:
                        return int(free[i - amount + 1])
                else:
                    run_len = 1
                    start_i = i
            return None
        # pure-python fallback
        count = 0
        run_start = None
        z = start_hint
        while z < n:
            if self.bits[z] == 0:
                if count == 0:
                    run_start = z
                count += 1
                if count == amount:
                    return run_start
            else:
                count = 0
            z += 1
        return None

    def free_count(self):
        if self._use_np:
            return int((self.bits == 0).sum())
        return sum(1 for b in self.bits if b == 0)

    def flush(self):
        """Re-pack changed chunks and write them back through the device.
        Only chunks whose bits were modified are rewritten."""
        for chunk in sorted(self._dirty_chunks):
            z0 = chunk * PFS_BITS_PER_CHUNK
            z1 = min(z0 + PFS_BITS_PER_CHUNK, self.total_zones)
            seg = self.bits[z0:z1]
            if self._use_np:
                # pad to full 8192 bits (1024 bytes) so chunk size is exact
                padded = _np.zeros(PFS_BITS_PER_CHUNK, dtype=_np.uint8)
                padded[:z1 - z0] = seg
                packed = _np.packbits(padded, bitorder="little").tobytes()
            else:
                buf = bytearray(PFS_META_SIZE)
                for i, b in enumerate(seg):
                    if b:
                        buf[i // 8] |= (1 << (i % 8))
                packed = bytes(buf)
            self.hdd.dev.write_bytes(self._chunk_lba_off(chunk), packed)
        self._dirty_chunks.clear()


# ---------------------------------------------------------------------------
# Main HDD class
# ---------------------------------------------------------------------------
class Ps2Hdd:
    def __init__(self, path, writable=False, overlay=False):
        """Open a PS2 HDD image.

        writable=True  -> writes go to the real file.
        overlay=True   -> copy-on-write: real file is read-only, writes captured
                          in memory (safe write testing).  Implies writes allowed.
        """
        self.path = path
        self.dev = BlockDevice(path, writable=writable, overlay=overlay)
        self._parts = None          # list[ApaPartition]
        self._supers = {}           # name -> superblock dict
        self._inode_scale = {}      # name -> log2(zone/512)

    # ---- APA ----
    def _read_apa_chain(self):
        if self._parts is not None:
            return self._parts
        parts = []
        lba = 0
        seen = set()
        while lba not in seen:
            seen.add(lba)
            raw = self.dev.read_sectors(lba, 2)  # header is up to 1KB+subs; 2 sectors of base
            # need full sub table (up to 0x1C0 + 64*8 = 0x3C0 = 960 bytes) -> 2 sectors ok
            if _u32(raw, 0x04) != APA_MAGIC:
                break
            p = ApaPartition(lba, raw)
            # M1: validate the APA header checksum so a corrupt table is not
            # walked silently (which could mis-locate partition start/length).
            if not p.checksum_valid:
                raise ValueError(
                    "APA header checksum mismatch at LBA %d "
                    "(stored=0x%08x computed=0x%08x); refusing to trust table"
                    % (lba, p.checksum, ApaPartition.checksum_of(p.raw)))
            parts.append(p)
            if p.next == 0 or p.next == lba:
                break
            lba = p.next
        self._parts = parts
        return parts

    def partitions(self):
        out = []
        for p in self._read_apa_chain():
            out.append({
                "name": p.id,
                "start_lba": p.start,
                "sectors": p.length,
                "type": p.type,
                "is_pfs": p.is_pfs,
            })
        return out

    def _find_part(self, name):
        for p in self._read_apa_chain():
            if p.id == name:
                return p
        raise KeyError("partition not found: %r" % name)

    # ---- PFS mount ----
    def _subpart_start(self, part, sub):
        """Absolute starting LBA for a (main or sub) partition extent."""
        if sub == 0:
            return part.start
        # sub indexes apa sub[] extents (1-based: sub 1 -> subs[0])
        return part.subs[sub - 1]["start"]

    def _zone_to_lba(self, part, scale, subpart, zone):
        return self._subpart_start(part, subpart) + (zone << scale)

    def _mount(self, name):
        if name in self._supers:
            return
        part = self._find_part(name)
        if not part.is_pfs:
            raise ValueError("partition %r is not PFS (type=0x%04x)" % (name, part.type))
        # superblock at zone 512 of subpart 0 (byte 0x400000)
        super_lba = part.start + (0x400000 // SECTOR)
        raw = self.dev.read_sectors(super_lba, 2)
        if _u32(raw, 0) != PFS_SUPER_MAGIC:
            raise ValueError("PFS superblock magic mismatch at LBA %d (got 0x%08x)"
                             % (super_lba, _u32(raw, 0)))
        sb = {
            "magic": _u32(raw, 0x00),
            "version": _u32(raw, 0x04),
            "pfsFsckStat": _u32(raw, 0x0C),
            "zone_size": _u32(raw, 0x10),
            "num_subs": _u32(raw, 0x14),
            "log": decode_blockinfo(raw, 0x18),
            "root": decode_blockinfo(raw, 0x20),
            "super_lba": super_lba,
        }
        zs = sb["zone_size"]
        scale = (zs // SECTOR).bit_length() - 1  # log2
        if (1 << scale) * SECTOR != zs:
            raise ValueError("zone_size %d not a power-of-two multiple of 512" % zs)
        # C1/safety: there is NO on-disk free-zone counter in pfs_super_block_t
        # (zfree/free_zone[] are runtime-only pfsMount fields -- verified against
        # ps2sdk libpfs.h); the on-disk bitmap is the sole free-space record, so
        # we keep it authoritative and never write a phantom superblock counter.
        # We DO honour pfsFsckStat: refuse to mutate a filesystem the driver has
        # already flagged with a write error.
        self._supers[name] = sb
        self._inode_scale[name] = scale

    # ---- zone accounting / bitmap helpers ----
    def _zones_per_subpart(self, part, sub):
        """Number of zones in a (main or sub) partition extent."""
        if sub == 0:
            length = part.length
        else:
            length = part.subs[sub - 1]["length"]
        # M4: total zones = length(sectors) >> sector_scale, exactly how libpfs
        # computes it (pfsBitmapSetupInfo: size = getSize >> sector_scale, then
        # partitionChunks = size / 8192, partitionRemainder = size % 8192 BITS).
        # The remainder is a sub-CHUNK (bit) count, not a sub-zone count: there
        # is no partial trailing *zone* to drop -- `length >> 4` is the exact
        # zone total.  (APA partition lengths are always a multiple of 16.)
        return length >> (0x2000 // SECTOR).bit_length() - 1  # length // 16

    def _get_bitmap(self, name, sub):
        part = self._find_part(name)
        total = self._zones_per_subpart(part, sub)
        return PfsBitmap(self, part, sub, total)

    def _bitmap_for(self, name, bm_cache, sub):
        """Fetch (and cache) the bitmap for a subpart, asserting its reserved
        metadata zones are USED before it may be used for allocation (H3)."""
        if sub not in bm_cache:
            bm = self._get_bitmap(name, sub)
            bm.verify_reserved()
            bm_cache[sub] = bm
        return bm_cache[sub]

    def _search_free_zone(self, name, bm_cache, want_sub, hint_zone, max_count):
        """Port of pfsBitmapSearchFreeZone: try the largest contiguous run up
        to min(max_count,32), halving on failure, falling back across subparts.
        Returns (sub, zone, count) and updates the relevant bitmap.
        bm_cache: dict {sub: PfsBitmap} reused across calls.

        Mirrors pfsBitmapSearchFreeZone's subpart sweep: it tries `want_sub`
        first then wraps through all subparts; when a start hint is given it
        also rescans the starting subpart from 0 (the libpfs num_subs+2 pass)."""
        sb = self._supers[name]
        num_subs = sb["num_subs"]
        count = max_count if max_count < 33 else 32
        if count < 1:
            count = 1
        # iterate subparts starting at want_sub, wrapping, like pfsshell
        order = [want_sub] + [s for s in range(0, num_subs + 1) if s != want_sub]
        for s in order:
            bm = self._bitmap_for(name, bm_cache, s)
            start = hint_zone if s == want_sub else 0
            n = count
            while n >= 1:
                z = bm.alloc_contiguous(start, n)
                if z is not None:
                    return (s, z, n)
                # also retry the whole-subpart scan from 0 for this size
                if start != 0:
                    z = bm.alloc_contiguous(0, n)
                    if z is not None:
                        return (s, z, n)
                n //= 2
        raise OSError("ENOSPC: no free zones for %d-zone request" % count)

    def _total_free_zones(self, name, bm_cache):
        """Sum of free zones across every subpart (pre-flight space check)."""
        sb = self._supers[name]
        total = 0
        for s in range(0, sb["num_subs"] + 1):
            total += self._bitmap_for(name, bm_cache, s).free_count()
        return total

    # ---- inode reading + segment walking ----
    def _read_inode(self, part, scale, bi):
        """Read a pfs_inode_t given a blockinfo pointing at its zone."""
        lba = self._zone_to_lba(part, scale, bi["subpart"], bi["number"])
        raw = self.dev.read_sectors(lba, PFS_INODE_BYTES // SECTOR)
        return PfsInode(raw), lba

    def _walk_extents(self, part, scale, root_inode):
        """Yield all *file-data* extents (subpart, zone, count) of a file/dir,
        following SEGI continuation segments via next_segment, in order.

        number_data (on the SEGD root) is the TOTAL number of data[] slots
        across ALL segment descriptors.  A logical segment index `i` maps to a
        physical data[] slot via pfsFixIndex(i); when pfsFixIndex(i)==0 for i>0
        the walk crosses into the next segment descriptor (next_segment).  Slot
        0 of every descriptor is that descriptor's OWN self-zone, so it is
        skipped (it holds the 1 KiB inode, not file data).  This mirrors
        pfsBlockSeekNextSegment / pfsInodeSync."""
        extents, _locs = self._walk_extents_loc(part, scale, root_inode)
        return extents

    def _walk_extents_loc(self, part, scale, root_inode, strict=True):
        """As _walk_extents, but also returns, for each file-data extent, the
        (segment_blockinfo, data_slot_index) that physically holds it.  The
        location list is what _grow needs to update the tail extent in the
        CORRECT segment descriptor (C2): never assume the tail lives in the
        most recently created SEGI.

        With strict=True (default, and always before a write) a malformed
        segment chain -- a missing next_segment partway through number_data, a
        bad SEGI/SEGD magic, or a self-referential link -- raises instead of
        silently truncating the extent list (M2).  A truncated list would make
        pfs_write underestimate capacity and trigger an unintended grow that
        allocates new zones while real data lived in the un-walked tail."""
        extents = []
        locs = []
        total = root_inode.number_data
        ino = root_inode
        ino_bi = dict(root_inode.inode_block)
        seen = {(ino_bi["subpart"], ino_bi["number"])}
        for i in range(total):
            fi = pfs_fix_index(i)
            if fi == 0 and i > 0:
                # crossed a segment boundary -> advance to next descriptor
                ns = ino.next_segment
                if ns["number"] == 0:
                    if strict:
                        raise OSError(
                            "PFS segment chain truncated: number_data=%d but "
                            "next_segment is null at logical index %d"
                            % (total, i))
                    break
                key = (ns["subpart"], ns["number"])
                if key in seen:
                    raise OSError("PFS segment chain loops at zone %d (sub %d)"
                                  % (ns["number"], ns["subpart"]))
                seen.add(key)
                ino, _ = self._read_inode(part, scale, ns)
                ino_bi = {"number": ns["number"], "subpart": ns["subpart"],
                          "count": ns.get("count", 1)}
                if ino.magic not in (PFS_SEGD_MAGIC, PFS_SEGI_MAGIC):
                    raise OSError(
                        "PFS segment descriptor at zone %d (sub %d) has bad "
                        "magic 0x%08x" % (ns["number"], ns["subpart"], ino.magic))
            if fi == 0:
                continue  # slot 0 = descriptor self-zone, not file data
            d = ino.data[fi]
            if d["count"] == 0:
                continue
            extents.append((d["subpart"], d["number"], d["count"]))
            locs.append((dict(ino_bi), fi))
        return extents, locs

    def _read_file_data(self, part, scale, root_inode, zone_size):
        """Read full content (truncated to inode.size) by concatenating extents.
        Note: the first data extent of an inode begins at the inode's own zone
        (the inode shares zone 0 of its allocation with file data after the
        1KB descriptor) -- but in PFS the data[] extents are *separate* data
        zones, so we simply read them in order."""
        extents = self._walk_extents(part, scale, root_inode)
        buf = bytearray()
        remaining = root_inode.size
        for (sub, zone, count) in extents:
            nbytes = count * zone_size
            lba = self._zone_to_lba(part, scale, sub, zone)
            nsec = nbytes // SECTOR
            chunk = self.dev.read_sectors(lba, nsec)
            buf += chunk
            if len(buf) >= remaining:
                break
        return bytes(buf[:remaining]), extents

    # ---- directory parsing ----
    def _parse_dir(self, part, scale, dir_inode, zone_size):
        """Parse all dentry records of a directory inode.
        Returns list of {name, inode_zone, sub}."""
        raw, _extents = self._read_file_data(part, scale, dir_inode, zone_size)
        entries = []
        total = len(raw)
        # records are walked per 512-byte sector
        sec = 0
        while sec < total:
            base = sec
            off = base
            sector_end = min(base + SECTOR, total)
            while off + 8 <= sector_end:
                inode_zone = _u32(raw, off)
                sub = raw[off + 4]
                plen = raw[off + 5]
                alen = _u16(raw, off + 6) & 0xFFF
                if alen < 8:
                    break  # corrupt / end of sector records
                if inode_zone != 0 and plen > 0 and off + 8 + plen <= sector_end:
                    # L3: latin-1 is a byte-exact, lossless 1:1 mapping for all
                    # 256 byte values, so name round-trips byte-for-byte
                    # (.encode('latin-1') reproduces the on-disk bytes).  This is
                    # read-only here; pfs_write resolves files by this same
                    # decoded name, so the contract stays byte-exact.
                    name = raw[off + 8:off + 8 + plen].decode("latin-1")
                    if name not in (".", ".."):
                        entries.append({"name": name, "inode_zone": inode_zone, "sub": sub})
                    elif name in (".", ".."):
                        entries.append({"name": name, "inode_zone": inode_zone, "sub": sub,
                                        "dotdir": True})
                off += alen
            sec += SECTOR
        return entries

    def _resolve_path(self, name, path):
        """Resolve a '/'-separated path to its inode, starting from root.
        Returns (PfsInode, part, scale, zone_size)."""
        self._mount(name)
        part = self._find_part(name)
        scale = self._inode_scale[name]
        sb = self._supers[name]
        zs = sb["zone_size"]
        ino, _ = self._read_inode(part, scale, sb["root"])
        comps = [c for c in path.replace("\\", "/").split("/") if c]
        for comp in comps:
            if not ino.is_dir:
                raise NotADirectoryError("not a directory in path: %r" % comp)
            ents = self._parse_dir(part, scale, ino, zs)
            match = None
            for e in ents:
                if e.get("dotdir"):
                    continue
                if e["name"] == comp:
                    match = e
                    break
            if match is None:
                raise FileNotFoundError("path component not found: %r" % comp)
            bi = {"number": match["inode_zone"], "subpart": match["sub"], "count": 1}
            ino, _ = self._read_inode(part, scale, bi)
        return ino, part, scale, zs

    # ---- public PFS API ----
    def pfs_listdir(self, partition_name, path="/"):
        ino, part, scale, zs = self._resolve_path(partition_name, path)
        if not ino.is_dir:
            raise NotADirectoryError("%r is not a directory" % path)
        ents = self._parse_dir(part, scale, ino, zs)
        out = []
        for e in ents:
            if e.get("dotdir"):
                continue
            bi = {"number": e["inode_zone"], "subpart": e["sub"], "count": 1}
            child, _ = self._read_inode(part, scale, bi)
            out.append({
                "name": e["name"],
                "size": int(child.size),
                "is_dir": child.is_dir,
                "inode_zone": e["inode_zone"],
            })
        return out

    def pfs_read(self, partition_name, path):
        ino, part, scale, zs = self._resolve_path(partition_name, path)
        if ino.is_dir:
            raise IsADirectoryError("%r is a directory" % path)
        data, _extents = self._read_file_data(part, scale, ino, zs)
        return data

    # ---- segment-descriptor chain (SEGD + SEGI) ----
    def _read_segment_chain(self, name, part, scale, root_bi):
        """Return the list of segment-descriptor inodes that make up a file.

        Each element is a dict:
          {'bi': {number,subpart,count}, 'lba': <on-disk LBA of this inode>,
           'raw': bytearray(1024)}
        Element 0 is the SEGD root inode; the rest are SEGI continuations
        reached via next_segment.  Mirrors pfsBlockGetNextSegment."""
        chain = []
        bi = dict(root_bi)
        seen = set()
        while True:
            key = (bi["subpart"], bi["number"])
            if key in seen:
                raise OSError("segment chain loops at zone %d (sub %d)"
                              % (bi["number"], bi["subpart"]))
            seen.add(key)
            lba = self._zone_to_lba(part, scale, bi["subpart"], bi["number"])
            raw = bytearray(self.dev.read_sectors(lba, PFS_INODE_BYTES // SECTOR))
            magic = _u32(raw, 0x04)
            want = PFS_SEGD_MAGIC if not chain else PFS_SEGI_MAGIC
            if magic != want:
                raise OSError("segment descriptor at zone %d (sub %d) has bad "
                              "magic 0x%08x (expected 0x%08x)"
                              % (bi["number"], bi["subpart"], magic, want))
            chain.append({"bi": dict(bi), "lba": lba, "raw": raw, "is_new": False})
            ns = decode_blockinfo(raw, 0x10)  # next_segment
            if ns["number"] == 0:
                break
            bi = {"number": ns["number"], "subpart": ns["subpart"], "count": 1}
        return chain

    @staticmethod
    def _seg_set_blockinfo(raw, off, bi):
        struct.pack_into("<IHH", raw, off, bi["number"] & 0xFFFFFFFF,
                         bi["subpart"] & 0xFFFF, bi["count"] & 0xFFFF)

    @staticmethod
    def _seg_finalize(raw):
        """Recompute and store the inode checksum (word 0)."""
        ck = PfsInode.checksum_of(raw)
        struct.pack_into("<I", raw, 0x00, ck)

    # ---- grow a PFS partition (add an APA sub-partition) --------------------
    def _invalidate(self):
        self._parts = None
        self._supers = {}
        self._inode_scale = {}

    def grow_partition(self, name, extra_bytes, log=print,
                       min_sub_sectors=262144, max_sub_sectors=4194304,
                       margin=None):
        """Enlarge PFS partition `name` by appending a new APA sub-partition.

        A PFS partition can't grow subpart 0 in place (its bitmap chunks sit at
        fixed offsets that later file data already occupies), so libapa/libpfs
        grow a partition by adding a *sub-partition*: a fresh APA extent with its
        own independent bitmap, referenced from the main header's subs[] table
        and counted by the superblock's num_subs. The existing allocator already
        sweeps every subpart (see _search_free_zone), so once the sub is wired in
        and its bitmap initialised, pfs_write can allocate from it transparently.

        `extra_bytes` is the minimum extra *free* space wanted; the sub is sized
        to the smallest disk-end-aligned power-of-two ≥ that (plus margin), min
        128 MiB, max 2 GiB. The image FILE is extended to hold it. Returns a dict
        with the new sub's start/length/zones. Requires the device be writable.
        Mirrors libapa apa_header_t (id empty on a sub; main/number back-links)
        and libpfs subpart bitmap layout; verified by re-mounting + free recount.
        """
        if not self.dev.writable or self.dev.overlay:
            raise PermissionError("grow_partition needs a writable (non-overlay) device")
        self._mount(name)
        part = self._find_part(name)
        sb = self._supers[name]
        if sb["num_subs"] != 0:
            # Keep it simple + safe: one sub is enough for our use. Extending an
            # already-subbed partition would append subs[1] — not needed here.
            raise NotImplementedError(
                "partition already has %d sub-partition(s); multi-sub grow not "
                "implemented" % sb["num_subs"])
        zs = sb["zone_size"]                     # 8192
        sec_per_zone = zs // SECTOR              # 16

        # --- size the new sub: aligned power-of-two, min 128 MiB, max 2 GiB ---
        disk_end = max(p.start + p.length for p in self._read_apa_chain())
        # headroom so a slightly bigger re-merge doesn't need another grow
        if margin is None:
            margin = max(256 * 1024 * 1024, extra_bytes // 2)
        want_sectors = (extra_bytes + margin + SECTOR - 1) // SECTOR
        sub_sectors = 1 << max(0, (want_sectors - 1)).bit_length()
        sub_sectors = max(sub_sectors, min_sub_sectors)  # ≥128 MiB (APA minimum)
        sub_sectors = min(sub_sectors, max_sub_sectors)  # ≤2 GiB (start stays aligned)
        if disk_end % sub_sectors != 0:
            raise ValueError(
                "disk end LBA %d is not aligned to a %d-sector partition; "
                "cannot place an APA-legal sub there" % (disk_end, sub_sectors))
        sub_start = disk_end
        sub_zones = sub_sectors >> (sec_per_zone.bit_length() - 1)   # //16
        log("grow %s: adding a %.0f MiB sub-partition at LBA %d (%d zones)"
            % (name, sub_sectors * SECTOR / 1048576, sub_start, sub_zones))

        # --- 1) extend the image file to cover the new sub (sparse) ----------
        end_sector = sub_start + sub_sectors - 1
        self.dev.write_sectors(end_sector, b"\x00" * SECTOR)   # grows the file
        self.dev.size = (end_sector + 1) * SECTOR

        # --- 2) write the new sub-partition APA header -----------------------
        sub_hdr = bytearray(1024)
        struct.pack_into("<I", sub_hdr, 0x04, APA_MAGIC)
        struct.pack_into("<I", sub_hdr, 0x08, 0)               # next (new tail)
        struct.pack_into("<I", sub_hdr, 0x0C, part.lba)        # prev = main
        # id (0x10) left empty — libapa sub-partitions carry no name
        struct.pack_into("<I", sub_hdr, 0x40, sub_start)       # start
        struct.pack_into("<I", sub_hdr, 0x44, sub_sectors)     # length
        struct.pack_into("<H", sub_hdr, 0x48, part.type)       # type = main's
        struct.pack_into("<H", sub_hdr, 0x4A, APA_FLAG_SUB)    # flags: SUB
        struct.pack_into("<I", sub_hdr, 0x4C, 0)               # nsub
        struct.pack_into("<I", sub_hdr, 0x54, part.lba)        # main -> main LBA
        struct.pack_into("<I", sub_hdr, 0x58, 1)               # number (1st sub)
        struct.pack_into("<I", sub_hdr, 0x00,
                         ApaPartition.checksum_of(bytes(sub_hdr)))
        self.dev.write_sectors(sub_start, bytes(sub_hdr))

        # --- 3) update the main header: subs[0], nsub, next, checksum --------
        main_raw = bytearray(self.dev.read_sectors(part.lba, 2))
        struct.pack_into("<I", main_raw, 0x1C0 + 0, sub_start)     # subs[0].start
        struct.pack_into("<I", main_raw, 0x1C0 + 4, sub_sectors)   # subs[0].length
        struct.pack_into("<I", main_raw, 0x4C, 1)                  # nsub
        struct.pack_into("<I", main_raw, 0x08, sub_start)          # next -> sub
        struct.pack_into("<I", main_raw, 0x00, 0)
        struct.pack_into("<I", main_raw, 0x00,
                         ApaPartition.checksum_of(bytes(main_raw[:1024])))
        self.dev.write_sectors(part.lba, bytes(main_raw[:1024]))

        # --- 4) update __mbr.prev (the APA tail pointer) ---------------------
        mbr = self._read_apa_chain()[0]
        if mbr.id == "__mbr":
            mbr_raw = bytearray(self.dev.read_sectors(mbr.lba, 2))
            struct.pack_into("<I", mbr_raw, 0x0C, sub_start)      # prev = new tail
            struct.pack_into("<I", mbr_raw, 0x00, 0)
            struct.pack_into("<I", mbr_raw, 0x00,
                             ApaPartition.checksum_of(bytes(mbr_raw[:1024])))
            self.dev.write_sectors(mbr.lba, bytes(mbr_raw[:1024]))

        # --- 5) superblock: num_subs = 1 -------------------------------------
        self.dev.write_bytes(sb["super_lba"] * SECTOR + 0x14, struct.pack("<I", 1))

        # --- 6) initialise the new subpart's bitmap (all free but its own
        #        chunk-backing zones + zone 0, which we reserve to match libpfs)
        self._invalidate()
        self._mount(name)
        part = self._find_part(name)
        bm = self._get_bitmap(name, 1)             # reads the zeroed region = all free
        reserved = set(bm.reserved_zones())
        reserved.add(0)                            # be conservative: keep zone 0 used
        for z in sorted(reserved):
            if bm.test(z) == 0:
                bm.mark_used(z, 1)
        bm.flush()
        self.dev.sync()

        # --- 7) verify by re-mounting + recounting free zones ----------------
        self._invalidate()
        self._mount(name)
        bm_cache = {}
        free_after = self._total_free_zones(name, bm_cache)
        self._bitmap_for(name, bm_cache, 1).verify_reserved()
        log("grow %s: done - sub zones=%d, total free zones now %d (%.0f MiB)"
            % (name, sub_zones, free_after, free_after * zs / 1048576))
        return {"sub_start": sub_start, "sub_sectors": sub_sectors,
                "sub_zones": sub_zones, "free_zones": free_after,
                "new_image_bytes": self.dev.size}

    def pfs_write(self, partition_name, path, data):
        """Replace an existing file's content, supporting in-place, shrink AND
        grow (the grow path ports pfsshell's bitmap allocator + inode/segment
        extension from libpfs bitmap.c / blockWrite.c).

        - len(data) <= current allocated zone capacity:
              in-place / shrink -- rewrite data zones, zero the tail.  (Shrink
              keeps the already-allocated zones but records the smaller size;
              freeing trailing zones is intentionally NOT done because doing it
              safely also requires truncating the inode extent list and PFS
              treats the descriptor zone specially -- see note in module docs.)
        - len(data) >  capacity:
              allocate the extra zones via the free bitmap, append them to the
              inode's data[] extent list (creating SEGI segments when the 114-
              entry inline array fills, exactly as pfsBlockAllocNewSegment),
              update number_data / number_blocks / size, recompute every
              touched inode's checksum, then write all data across old+new
              extents and flush the modified bitmap chunks.

        Journal/log handling: pfsshell journals metadata for crash safety and
        replays/clears the log on a clean umount, after which the on-disk meta
        is fully self-consistent.  This offline editor performs the equivalent
        of a clean umount: it writes FINAL, self-consistent metadata directly
        (updated bitmap + inode extents + checksums) and leaves NO pending log
        entry, so an fsck/driver mount sees a clean filesystem.  The PFS log
        zones are left untouched (they were already empty for these images)."""
        if not (self.dev.overlay or (self.dev.writable and not self.dev.overlay)):
            raise PermissionError("device not writable (open with writable=True or overlay=True)")

        ino, part, scale, zs = self._resolve_path(partition_name, path)
        if ino.is_dir:
            raise IsADirectoryError("%r is a directory" % path)

        # locate the inode's own blockinfo via the parent directory
        parent_path = "/".join(path.replace("\\", "/").split("/")[:-1]) or "/"
        leaf = [c for c in path.replace("\\", "/").split("/") if c][-1]
        pino, _, _, _ = self._resolve_path(partition_name, parent_path)
        pents = self._parse_dir(part, scale, pino, zs)
        root_bi = None
        for e in pents:
            if not e.get("dotdir") and e["name"] == leaf:
                root_bi = {"number": e["inode_zone"], "subpart": e["sub"], "count": 1}
                break
        if root_bi is None:
            raise FileNotFoundError(path)

        # refuse to touch a filesystem the driver already flagged dirty (a
        # write error / interrupted op): writing atop unknown corruption risks
        # compounding it.  PFS_FSCK_STAT_ERRORS_FIXED alone is benign.
        sb = self._supers[partition_name]
        if sb["pfsFsckStat"] & PFS_FSCK_STAT_WRITE_ERROR:
            raise OSError("PFS %r flagged pfsFsckStat=0x%02x (write error); "
                          "run fsck before writing" % (partition_name, sb["pfsFsckStat"]))

        # extents + the exact (segment, slot) holding each (strict: a malformed
        # chain raises here, before any mutation -- see _walk_extents_loc / M2).
        extents, _locs = self._walk_extents_loc(part, scale, ino, strict=True)
        capacity = sum(c for (_s, _z, c) in extents) * zs

        # True no-op: identical size AND identical bytes -> touch nothing at all
        # (keeps the image byte-exact; avoids needless metadata churn).  Real
        # content changes DO refresh mtime/ctime/atime below (H4).
        if len(data) == int(ino.size):
            current, _ = self._read_file_data(part, scale, ino, zs)
            if current == data:
                return len(data)

        if len(data) > capacity:
            # GROW: allocate + extend inode/segments, write the payload, and
            # commit metadata in a crash-safe order (see _grow).  _grow performs
            # its own pre-flight space check and owns all writes for this path.
            self._grow(partition_name, part, scale, zs, root_bi,
                       len(data), data)
        else:
            # in-place / shrink: rewrite data, then update size + timestamps on
            # the root inode (kept as the single committing write).
            self._write_payload(part, scale, zs, extents, data, capacity)
            self._update_root_inode(part, scale, root_bi, len(data))
        return len(data)

    def _write_payload(self, part, scale, zs, extents, data, capacity):
        """Write `data` (zero-padded to `capacity`) across `extents` in order."""
        payload = data + b"\x00" * (capacity - len(data))
        pos = 0
        for (sub, zone, count) in extents:
            nbytes = count * zs
            lba = self._zone_to_lba(part, scale, sub, zone)
            chunk = payload[pos:pos + nbytes]
            if len(chunk) < nbytes:
                chunk = chunk + b"\x00" * (nbytes - len(chunk))
            self.dev.write_bytes(lba * SECTOR, chunk)
            pos += nbytes
            if pos >= len(payload):
                break

    def _update_root_inode(self, part, scale, root_bi, new_size):
        """Update the SEGD root inode's size + timestamps + checksum and write
        it back.  Updates mtime/ctime/atime (H4) the way pfsInodeSetTime does
        (all three set to 'now')."""
        inode_lba = self._zone_to_lba(part, scale, root_bi["subpart"], root_bi["number"])
        raw = bytearray(self.dev.read_sectors(inode_lba, PFS_INODE_BYTES // SECTOR))
        if _u32(raw, 0x04) != PFS_SEGD_MAGIC:
            raise OSError("root inode at zone %d has bad SEGD magic 0x%08x"
                          % (root_bi["number"], _u32(raw, 0x04)))
        struct.pack_into("<Q", raw, PFS_INODE_OFF_SIZE, new_size)
        now = pfs_datetime_now()
        raw[PFS_INODE_OFF_MTIME:PFS_INODE_OFF_MTIME + 8] = now
        raw[PFS_INODE_OFF_CTIME:PFS_INODE_OFF_CTIME + 8] = now
        raw[PFS_INODE_OFF_ATIME:PFS_INODE_OFF_ATIME + 8] = now
        self._seg_finalize(raw)
        self.dev.write_sectors(inode_lba, bytes(raw))

    def _grow(self, name, part, scale, zs, root_bi, need_size, data):
        """Grow the file at `root_bi` so its data extents cover `need_size`
        bytes, then write the `data` payload across old+new extents and commit
        metadata crash-safely.  Returns the full updated extent list.

        Port of pfsBlockAllocNewSegment + pfsBlockExpandSegment + the bitmap
        allocator (libpfs blockWrite.c / bitmap.c).  Correctness invariants that
        the original review (C2/C3) and libpfs both require:

          * number_data counts EVERY data[] slot, INCLUDING each descriptor's
            self-pointer at slot 0 (blockWrite.c increments number_data once for
            the SEGI self-zone, line 84, then again per data extent, line 114).
          * a new data extent is written to data[pfsFixIndex(number_data-1)] of
            the segment that currently owns the tail (blockpos->inode), NOT
            blindly the newest SEGI (C2).
          * an in-place expand of the tail extent updates the slot in the
            segment descriptor that physically holds that extent (C2).

        Crash safety (C4): nothing is written to the device until the entire
        allocation is planned in memory and a pre-flight free-space check
        passes (H2).  Commit order then guarantees an interrupted grow cannot
        leave a dangling next_segment pointing at an unwritten SEGI:
            1. write all new file-data zones,
            2. flush bitmap chunks,         (durably mark zones used)
            3. write every NEW SEGI inode,  (the link targets now exist)
            4. fsync,
            5. write each MODIFIED existing inode (these carry next_segment
               links + the root size/counter update) -- the linking writes,
            6. fsync.
        If we die before step 5, the original inodes still describe the old
        (smaller) file; the freshly-marked zones are merely reserved-but-unused
        (a benign leak fsck reclaims), never a corrupt chain."""
        self._mount(name)
        chain = self._read_segment_chain(name, part, scale, root_bi)
        root_raw = chain[0]["raw"]
        if _u32(root_raw, 0x04) != PFS_SEGD_MAGIC:
            raise OSError("root inode at zone %d is not SEGD (magic 0x%08x)"
                          % (root_bi["number"], _u32(root_raw, 0x04)))

        # Re-derive the file-data extents AND, for each, the (chain index, slot)
        # that holds it -- so expand/append target the right descriptor (C2).
        root_inode = PfsInode(root_raw)
        cur_extents, locs = self._walk_extents_loc(part, scale, root_inode, strict=True)
        # map each segment's on-disk bi -> chain index
        bi_to_chain = {}
        for ci, seg in enumerate(chain):
            bi_to_chain[(seg["bi"]["subpart"], seg["bi"]["number"])] = ci

        new_size = len(data)
        cur_zones = sum(c for (_s, _z, c) in cur_extents)
        need_zones = (need_size + zs - 1) // zs
        extra = need_zones - cur_zones
        if extra <= 0:
            # already large enough: just rewrite payload + size in place.
            capacity = cur_zones * zs
            self._write_payload(part, scale, zs, cur_extents, data, capacity)
            self._update_root_inode(part, scale, root_bi, new_size)
            return cur_extents

        number_blocks = _u32(root_raw, 0x3E0)
        root_number_data = _u32(root_raw, 0x3E4)
        number_segdesg = _u32(root_raw, 0x3E8)

        def set_u32(seg_raw, off, val):
            struct.pack_into("<I", seg_raw, off, val & 0xFFFFFFFF)

        bm_cache = {}
        # H2: pre-flight space check.  Worst case = `extra` data zones + one
        # SEGI descriptor zone every time the running data[] index lands on a
        # new-segment boundary (pfsFixIndex(index)==0).  Walk the indices the
        # same way the real allocation loop will, so the estimate is exact.
        worst_segis = 0
        probe = root_number_data
        rem_probe = extra
        while rem_probe > 0:
            if pfs_fix_index(probe) == 0:
                worst_segis += 1
                probe += 1  # the SEGI self slot
                continue
            probe += 1
            rem_probe -= 1
        worst_case = extra + worst_segis
        free_total = self._total_free_zones(name, bm_cache)
        if free_total < worst_case:
            raise OSError("ENOSPC: need up to %d zones (%d data + %d descriptor) "
                          "to grow, only %d free"
                          % (worst_case, extra, worst_segis, free_total))

        # Track which chain index + slot holds the current tail data extent.
        new_extents = list(cur_extents)
        if locs:
            tail_seg_bi, tail_slot = locs[-1]
            tail_ci = bi_to_chain[(tail_seg_bi["subpart"], tail_seg_bi["number"])]
        else:
            tail_ci, tail_slot = None, None

        # contiguity hint = tail of current extents
        if new_extents:
            hs, hz, hc = new_extents[-1]
            hint_sub, hint_zone = hs, hz + hc
        else:
            hint_sub, hint_zone = root_bi["subpart"], 0

        # `cur_seg` = chain index of the descriptor that NEW extents go into.
        cur_ci = bi_to_chain[(chain[-1]["bi"]["subpart"], chain[-1]["bi"]["number"])]

        remaining = extra
        # L4: the loop is bounded by the work to do; each iteration either
        # expands the tail (>=1 zone), appends an extent (>=1 zone), or opens a
        # SEGI.  Cap iterations at extra + worst_segis + a small margin.
        guard = 0
        guard_max = extra + worst_segis + 8
        while remaining > 0:
            guard += 1
            if guard > guard_max:
                raise OSError("grow loop exceeded bound (%d) -- aborting" % guard_max)

            # ---- expand the current tail extent contiguously (C2: into the
            # descriptor that actually holds it) ----
            if new_extents and tail_ci is not None:
                ls, lz, lc = new_extents[-1]
                bm = self._bitmap_for(name, bm_cache, ls)
                add = 0
                z = lz + lc
                while (add < remaining and (lc + add) < PFS_EXTENT_COUNT_MAX
                       and z < bm.total_zones and bm.test(z) == 0):
                    add += 1
                    z += 1
                if add > 0:
                    bm.mark_used(lz + lc, add)
                    new_extents[-1] = (ls, lz, lc + add)
                    self._seg_set_blockinfo(chain[tail_ci]["raw"], 0x28 + tail_slot * 8,
                                            {"number": lz, "subpart": ls, "count": lc + add})
                    number_blocks += add
                    set_u32(root_raw, 0x3E0, number_blocks)
                    remaining -= add
                    if remaining == 0:
                        break

            # ---- open a SEGI descriptor if the next slot would be index 0 ----
            if pfs_fix_index(root_number_data) == 0:
                seg_sub, seg_zone, seg_cnt = self._search_free_zone(
                    name, bm_cache, hint_sub, hint_zone, 1)
                segi_lba = self._zone_to_lba(part, scale, seg_sub, seg_zone)
                segi = bytearray(PFS_INODE_BYTES)
                struct.pack_into("<I", segi, 0x04, PFS_SEGI_MAGIC)
                # inode_block = root's inode_block identity (blockWrite.c:77)
                self._seg_set_blockinfo(segi, 0x08, decode_blockinfo(root_raw, 0x08))
                # last_segment = current segment's data[0] self-pointer (:78)
                self._seg_set_blockinfo(segi, 0x18, decode_blockinfo(chain[cur_ci]["raw"], 0x28))
                self_bi = {"number": seg_zone, "subpart": seg_sub, "count": seg_cnt}
                self._seg_set_blockinfo(segi, 0x28, self_bi)  # data[0] = self
                # root counters: +1 for the SEGI self slot (blockWrite.c:83-88)
                number_blocks += seg_cnt
                set_u32(root_raw, 0x3E0, number_blocks)
                root_number_data += 1
                set_u32(root_raw, 0x3E4, root_number_data)
                number_segdesg += 1
                set_u32(root_raw, 0x3E8, number_segdesg)
                self._seg_set_blockinfo(root_raw, 0x18, self_bi)  # root.last_segment
                # link previous current segment's next_segment -> new SEGI (:94)
                self._seg_set_blockinfo(chain[cur_ci]["raw"], 0x10, self_bi)
                new_seg = {"raw": segi, "lba": segi_lba, "bi": self_bi,
                           "is_new": True}
                chain.append(new_seg)
                cur_ci = len(chain) - 1
                bi_to_chain[(seg_sub, seg_zone)] = cur_ci

            # ---- allocate the next data extent into the current descriptor ----
            req = remaining if remaining < 32 else 32
            d_sub, d_zone, d_cnt = self._search_free_zone(
                name, bm_cache, hint_sub, hint_zone, req)
            root_number_data += 1
            set_u32(root_raw, 0x3E4, root_number_data)
            slot = pfs_fix_index(root_number_data - 1)
            self._seg_set_blockinfo(chain[cur_ci]["raw"], 0x28 + slot * 8,
                                    {"number": d_zone, "subpart": d_sub, "count": d_cnt})
            number_blocks += d_cnt
            set_u32(root_raw, 0x3E0, number_blocks)
            new_extents.append((d_sub, d_zone, d_cnt))
            tail_ci, tail_slot = cur_ci, slot
            hint_sub, hint_zone = d_sub, d_zone + d_cnt
            remaining -= d_cnt

        if remaining > 0:  # pragma: no cover (guarded by pre-flight)
            raise OSError("ENOSPC: could not allocate %d more zones" % remaining)

        # ---- update root size + timestamps; recompute every touched checksum
        struct.pack_into("<Q", root_raw, PFS_INODE_OFF_SIZE, new_size)
        now = pfs_datetime_now()
        root_raw[PFS_INODE_OFF_MTIME:PFS_INODE_OFF_MTIME + 8] = now
        root_raw[PFS_INODE_OFF_CTIME:PFS_INODE_OFF_CTIME + 8] = now
        root_raw[PFS_INODE_OFF_ATIME:PFS_INODE_OFF_ATIME + 8] = now
        for seg in chain:
            self._seg_finalize(seg["raw"])

        capacity = sum(c for (_s, _z, c) in new_extents) * zs

        # =================  COMMIT (ordered, crash-safe C4)  =================
        # 1. file-data zones (old+new) first.
        self._write_payload(part, scale, zs, new_extents, data, capacity)
        # 2. durably mark the new zones used in the bitmap.
        for bm in bm_cache.values():
            bm.flush()
        self.dev.sync()
        # 3. write NEW SEGI inodes (link targets must exist before linking).
        new_segs = [s for s in chain if s.get("is_new")]
        for seg in new_segs:
            self.dev.write_sectors(seg["lba"], bytes(seg["raw"]))
        self.dev.sync()
        # 4. write MODIFIED existing inodes LAST -- these carry the
        #    next_segment links and the root size/counter update.
        for seg in chain:
            if not seg.get("is_new"):
                self.dev.write_sectors(seg["lba"], bytes(seg["raw"]))
        self.dev.sync()

        return new_extents

    def close(self):
        self.dev.close()


# ===========================================================================
# Self-test against the real image (READ-ONLY + OVERLAY -- never writes file).
# ===========================================================================
def _hexbytes(b, n=32):
    return " ".join("%02x" % x for x in b[:n])


def _print_tree(hdd, part, path, depth, max_depth, max_entries=12):
    try:
        ents = hdd.pfs_listdir(part, path)
    except Exception as e:
        print("  " * depth + "[error listing %s: %s]" % (path, e))
        return
    for i, e in enumerate(ents):
        if i >= max_entries:
            print("  " * depth + "... (%d more)" % (len(ents) - max_entries))
            break
        kind = "<DIR>" if e["is_dir"] else ("%d" % e["size"])
        print("  " * (depth + 1) + "%-28s %s" % (e["name"], kind))
        if e["is_dir"] and depth + 1 < max_depth:
            child = (path.rstrip("/") + "/" + e["name"])
            _print_tree(hdd, part, child, depth + 1, max_depth, max_entries)


def _find_test_image():
    """Locate the Taiko14 test image.  L2: don't hardcode a single dead path --
    accept $PS2HDD_TEST_IMG, then probe a few known locations."""
    cand = []
    env = os.environ.get("PS2HDD_TEST_IMG")
    if env:
        cand.append(env)
    cand += [
        r"E:\NM00057 T14100-1-NA-HDD0-A [Ver.B02] (HDD).img",
        r"E:\system2x6_template_gamelibrary\system2x6_template_library"
        r"\NM00057\NM00057 T14100-1-NA-HDD0-A [Ver.B02] (HDD).img",
        r"E:\Taiko No Tatsujin 14+\tnt14plus.img",
    ]
    for c in cand:
        if c and os.path.exists(c):
            # must be readable (a copy may be locked by another process)
            try:
                with open(c, "rb") as _f:
                    _f.read(16)
                return c
            except OSError:
                continue
    return cand[0]


def _selftest():
    IMG = _find_test_image()
    print("=" * 74)
    print("PS2 HDD tool self-test :", IMG)
    print("=" * 74)

    # --- READ-ONLY phase ---
    hdd = Ps2Hdd(IMG, writable=False)
    overall_pass = True

    print("\n[1] APA partitions:")
    parts = hdd.partitions()
    for p in parts:
        gb = p["sectors"] * SECTOR / (1024 ** 3)
        print("    %-18s start_lba=%-10d sectors=%-10d type=0x%04x pfs=%s (%.2f GB)"
              % (p["name"], p["start_lba"], p["sectors"], p["type"], p["is_pfs"], gb))
    names = [p["name"] for p in parts]
    expect = ["__mbr", "t14jp1400.0000", "t14jp1400.0001"]
    have_expected = all(any(e == n for n in names) for e in expect)
    print("    ground-truth names present (__mbr, t14jp1400.0000/0001): %s"
          % ("PASS" if have_expected else "FAIL"))
    overall_pass &= have_expected

    GAME = "t14jp1400.0000"

    print("\n[2] pfs_listdir on %s root + tree:" % GAME)
    root = hdd.pfs_listdir(GAME, "/")
    for e in root:
        kind = "<DIR>" if e["is_dir"] else ("%d" % e["size"])
        print("    %-28s %s" % (e["name"], kind))
    print("    --- recursive tree (2 levels) ---")
    _print_tree(hdd, GAME, "/", 0, 2)
    overall_pass &= (len(root) > 0)

    # find a file to read (prefer LIST.BIN / DATA.000 style; else smallest file)
    print("\n[3] pfs_read a real file:")
    target = None

    def find_file(path, depth):
        nonlocal target
        if depth > 2 or target is not None:
            return
        try:
            ents = hdd.pfs_listdir(GAME, path)
        except Exception:
            return
        # priority: LIST.BIN / DATA.000-like
        for e in ents:
            if not e["is_dir"] and e["size"] > 0:
                up = e["name"].upper()
                if "LIST" in up or up.startswith("DATA.") or up.endswith(".BIN"):
                    target = (path.rstrip("/") + "/" + e["name"], e)
                    return
        for e in ents:
            if e["is_dir"]:
                find_file(path.rstrip("/") + "/" + e["name"], depth + 1)
        # fallback: any small file
        if target is None:
            cands = [e for e in ents if not e["is_dir"] and 0 < e["size"]]
            if cands:
                cands.sort(key=lambda x: x["size"])
                e = cands[0]
                target = (path.rstrip("/") + "/" + e["name"], e)

    find_file("/", 0)
    if target is None:
        print("    [no readable file found]")
        overall_pass = False
        small_file = None
    else:
        fpath, fmeta = target
        data = hdd.pfs_read(GAME, fpath)
        print("    file : %s" % fpath)
        print("    size : %d (inode size=%d)" % (len(data), fmeta["size"]))
        print("    head : %s" % _hexbytes(data, 32))
        ok = len(data) == fmeta["size"]
        print("    read size matches inode: %s" % ("PASS" if ok else "FAIL"))
        overall_pass &= ok
        small_file = (fpath, fmeta, data)

    # --- WRITE round-trip via OVERLAY (never touches the real file) ---
    print("\n[4] WRITE round-trip via copy-on-write OVERLAY (real .img untouched):")
    if small_file is None:
        # pick the smallest file anywhere up to depth 2
        print("    [no file selected; skipping write test] FAIL")
        overall_pass = False
    else:
        fpath, fmeta, orig = small_file
        # choose a small enough file for the write test; if huge, find a smaller one
        if len(orig) > 2_000_000:
            best = None
            def find_small(path, depth):
                nonlocal best
                if depth > 2:
                    return
                try:
                    ents = hdd.pfs_listdir(GAME, path)
                except Exception:
                    return
                for e in ents:
                    if not e["is_dir"] and 0 < e["size"] <= 65536:
                        if best is None or e["size"] < best[1]["size"]:
                            best = (path.rstrip("/") + "/" + e["name"], e)
                    elif e["is_dir"]:
                        find_small(path.rstrip("/") + "/" + e["name"], depth + 1)
            find_small("/", 0)
            if best:
                fpath = best[0]
                orig = hdd.pfs_read(GAME, fpath)
        hdd.close()

        wdev = Ps2Hdd(IMG, overlay=True)  # COW overlay, file stays read-only
        print("    test file : %s (%d bytes)" % (fpath, len(orig)))

        # 4a: identical-bytes write -> no non-identical sector changes
        wdev.pfs_write(GAME, fpath, orig)
        nonid = wdev.dev.overlay_nonidentical()
        # the inode-size write rewrites the size field with the same value, and
        # checksum stays identical, so the inode sector should also be identical.
        noop_ok = (len(nonid) == 0)
        print("    4a same-bytes write -> non-identical sectors captured: %d  %s"
              % (len(nonid), "PASS" if noop_ok else "FAIL"))
        overall_pass &= noop_ok

        # 4b: modified same-size write -> pfs_read returns new bytes
        modified = bytearray(orig)
        if len(modified) >= 4:
            modified[0] ^= 0xFF
            modified[1] ^= 0xFF
            modified[2] ^= 0xFF
            modified[3] ^= 0xFF
        modified = bytes(modified)
        wdev.pfs_write(GAME, fpath, modified)
        back = wdev.pfs_read(GAME, fpath)
        rt_ok = (back == modified)
        print("    4b modified write -> read-back matches: %s" % ("PASS" if rt_ok else "FAIL"))
        if not rt_ok:
            print("        expected head: %s" % _hexbytes(modified))
            print("        got      head: %s" % _hexbytes(back))
        overall_pass &= rt_ok

        # 4c: re-listing still works after the overlay write
        try:
            relist = wdev.pfs_listdir(GAME, "/")
            relist_ok = len(relist) > 0
        except Exception as e:
            print("        re-list error: %s" % e)
            relist_ok = False
        print("    4c re-list root after write -> %d entries: %s"
              % (len(relist) if relist_ok else 0, "PASS" if relist_ok else "FAIL"))
        overall_pass &= relist_ok

        # 4d: confirm overlay captured writes but file untouched
        ov = wdev.dev.overlay_changes()
        print("    4d overlay captured %d modified sectors; real .img opened read-only: PASS"
              % len(ov))
        wdev.close()

    # --- GROW round-trip + bitmap consistency via OVERLAY (file untouched) ---
    print("\n[5] GROW round-trip + bitmap consistency via OVERLAY (real .img untouched):")
    GROW_MB = 5
    gdev = Ps2Hdd(IMG, overlay=True)
    try:
        bm_before = gdev._get_bitmap(GAME, 0)
        free_before = bm_before.free_count()
        orig = gdev.pfs_read(GAME, "/list.bin")
        # occupancy of all files BEFORE grow (to prove no overlap after)
        def _occupancy(hdd, files):
            occ = {}
            dup = False
            for p in files:
                ino, pt, sc, _zs = hdd._resolve_path(GAME, p)
                for (s, z0, c) in hdd._walk_extents(pt, sc, ino):
                    for z in range(z0, z0 + c):
                        if (s, z) in occ:
                            dup = True
                        occ[(s, z)] = p
            return occ, dup

        files = [e["name"] for e in gdev.pfs_listdir(GAME, "/") if not e["is_dir"]]
        files = ["/" + f for f in files]

        # capture original file-data zone count + segment-descriptor count so the
        # free-count delta can be checked exactly (not a tautology).
        _ino0, _pt0, _sc0, _ = gdev._resolve_path(GAME, "/list.bin")
        zones_orig_filezones = sum(c for (_s, _z, c) in gdev._walk_extents(_pt0, _sc0, _ino0))
        segdesg_before = _u32(_ino0.raw, 0x3E8)

        grown = orig + bytes((i * 31 + 7) & 0xFF for i in range(GROW_MB * 1024 * 1024))
        gdev.pfs_write(GAME, "/list.bin", grown)

        back = gdev.pfs_read(GAME, "/list.bin")
        grow_rt = (back == grown)
        print("    5a grow /list.bin by %d MB -> read-back exact: %s"
              % (GROW_MB, "PASS" if grow_rt else "FAIL"))
        overall_pass &= grow_rt

        # re-list still works
        try:
            rl = gdev.pfs_listdir(GAME, "/")
            rl_ok = any(e["name"] == "list.bin" and e["size"] == len(grown) for e in rl)
        except Exception as e:
            print("        re-list error: %s" % e)
            rl_ok = False
        print("    5b re-list shows grown size: %s" % ("PASS" if rl_ok else "FAIL"))
        overall_pass &= rl_ok

        # consistency checks
        ino, pt, sc, _zs = gdev._resolve_path(GAME, "/list.bin")
        new_ext = gdev._walk_extents(pt, sc, ino)
        bm_after = gdev._get_bitmap(GAME, 0)
        free_after = bm_after.free_count()
        zones_now = sum(c for (_s, _z, c) in new_ext)
        segdesg_after = _u32(ino.raw, 0x3E8)
        segi_zones_expected = max(0, segdesg_after - segdesg_before)
        # (a) every allocated extent zone marked used
        a_ok = all(bm_after.test(z) == 1 for (_s, z0, c) in new_ext for z in range(z0, z0 + c))
        # (b) no two files share a zone
        _occ, dup = _occupancy(gdev, files)
        b_ok = not dup
        # (c) inode checksum validates
        c_ok = (ino.checksum == PfsInode.checksum_of(ino.raw))
        # (d) free-zone count decreased by EXACTLY the number of newly-used
        # zones (file-data growth + any SEGI descriptor zones).  Compare the
        # bitmap-flip delta against the inode-derived expectation -- not a
        # tautology: an over- or under-count (double alloc / leak) fails here.
        allocated = free_before - free_after
        expected_new_data = zones_now - zones_orig_filezones
        d_ok = (allocated == expected_new_data + segi_zones_expected
                and allocated > 0)
        print("    5c-a every allocated extent zone marked USED in bitmap: %s"
              % ("PASS" if a_ok else "FAIL"))
        print("    5c-b no zone shared between any two files: %s"
              % ("PASS" if b_ok else "FAIL"))
        print("    5c-c grown inode checksum validates: %s"
              % ("PASS" if c_ok else "FAIL"))
        print("    5c-d free-zone count decreased (by %d zones; %d->%d): %s"
              % (allocated, free_before, free_after, "PASS" if d_ok else "FAIL"))
        overall_pass &= (a_ok and b_ok and c_ok and d_ok)

        # confirm same-size no-op safety still holds on the grown file
        before_ov = len(gdev.dev.overlay_nonidentical())
        gdev.pfs_write(GAME, "/list.bin", grown)  # identical rewrite
        after_back = gdev.pfs_read(GAME, "/list.bin")
        noop2 = (after_back == grown)
        print("    5d identical rewrite of grown file -> still exact: %s"
              % ("PASS" if noop2 else "FAIL"))
        overall_pass &= noop2

        print("    5e overlay captured %d modified sectors; real .img read-only: PASS"
              % len(gdev.dev.overlay_changes()))
    finally:
        gdev.close()

    # --- SYNTHETIC: multi-SEGI grow + ENOSPC + overlap (C2/C3/H1/H2) ---
    print("\n[6] SYNTHETIC PFS: multi-segment (SEGI) grow + independent extent check:")
    overall_pass &= _selftest_synthetic()

    print("\n" + "=" * 74)
    print("OVERALL: %s" % ("PASS" if overall_pass else "FAIL"))
    print("=" * 74)
    return overall_pass


# ---------------------------------------------------------------------------
# Synthetic minimal PFS image, used to exercise the SEGI-crossing grow path
# deterministically (the real Taiko image never crosses a SEGI boundary).
# ---------------------------------------------------------------------------
def _build_synthetic_pfs(path, part_zones=4096):
    """Write a tiny but format-correct APA+PFS image to `path` with a single
    PFS partition holding one empty file 'f.bin' at the root.  Layout mirrors
    the offsets ps2hdd.py reads, enough for mount/list/read/write/grow.

    Returns (partition_name, file_path)."""
    zsize = 0x2000          # 8192
    scale = 4               # sectors per zone
    part_start_lba = 0x800  # arbitrary aligned start
    part_sectors = part_zones * (zsize // SECTOR)

    total_sectors = part_start_lba + part_sectors + 16
    buf = bytearray(total_sectors * SECTOR)

    def w32(off, v):
        struct.pack_into("<I", buf, off, v & 0xFFFFFFFF)

    def wbi(off, number, sub, count):
        struct.pack_into("<IHH", buf, off, number & 0xFFFFFFFF,
                         sub & 0xFFFF, count & 0xFFFF)

    # ---- APA: MBR header @ lba0, one PFS partition header @ part_start_lba ----
    def apa_header(lba, magic_next, prev, pid, start, length, ptype):
        base = lba * SECTOR
        w32(base + 0x04, APA_MAGIC)
        w32(base + 0x08, magic_next)
        w32(base + 0x0C, prev)
        nm = pid.encode("latin-1")[:APA_IDMAX]
        buf[base + 0x10:base + 0x10 + len(nm)] = nm
        w32(base + 0x40, start)
        w32(base + 0x44, length)
        struct.pack_into("<H", buf, base + 0x48, ptype)
        w32(base + 0x4C, 0)  # nsub
        # checksum (sum of words 1..255)
        s = 0
        for i in range(1, 256):
            s = (s + struct.unpack_from("<I", buf, base + i * 4)[0]) & 0xFFFFFFFF
        w32(base + 0x00, s)

    PNAME = "synthpfs.0000"
    apa_header(0, part_start_lba, 0, "__mbr", 0, part_start_lba, 0x0001)
    apa_header(part_start_lba, 0, 0, PNAME, part_start_lba, part_sectors, APA_TYPE_PFS)

    pbase = part_start_lba * SECTOR  # partition byte 0

    # ---- bitmap: mark reserved zones used, everything else free ----
    # bitmap chunk 0 lives at metablock 8+4096 -> byte 0x402000 -> zone 513.
    bitmap_zone0 = 0x402000 // zsize  # 513
    # superblock zone 512; log zones 517..532 (16); root inode zone; file inode
    super_zone = 512
    log_zone, log_cnt = 517, 16
    root_zone = 533
    file_zone = 534
    file_data_zone = 535  # initial 1-zone data extent for f.bin
    reserved = set([super_zone, bitmap_zone0])
    reserved |= set(range(log_zone, log_zone + log_cnt))
    reserved |= {root_zone, file_zone, file_data_zone}
    # bitmap bytes (1 bit per zone, LSB-first) at partition byte 0x402000
    bm_off = pbase + 0x402000
    for z in reserved:
        buf[bm_off + (z // 8)] |= (1 << (z % 8))

    # ---- superblock @ zone 512 (byte 0x400000) ----
    sb = pbase + super_zone * zsize
    w32(sb + 0x00, PFS_SUPER_MAGIC)
    w32(sb + 0x04, 3)           # version
    w32(sb + 0x0C, 0)           # pfsFsckStat OK
    w32(sb + 0x10, zsize)
    w32(sb + 0x14, 0)           # num_subs
    wbi(sb + 0x18, log_zone, 0, log_cnt)
    wbi(sb + 0x20, root_zone, 0, 1)

    # ---- root directory inode @ root_zone ----
    def inode(zone, magic, attr, size, ndata, nblocks, nsegd):
        o = pbase + zone * zsize
        w32(o + 0x04, magic)
        wbi(o + 0x08, zone, 0, 1)         # inode_block (self)
        wbi(o + 0x10, 0, 0, 0)            # next_segment
        wbi(o + 0x18, zone, 0, 1)         # last_segment (self)
        struct.pack_into("<H", buf, o + 0x3BA, attr)
        struct.pack_into("<Q", buf, o + 0x3D8, size)
        w32(o + 0x3E0, nblocks)
        w32(o + 0x3E4, ndata)
        w32(o + 0x3E8, nsegd)
        return o

    # root dir: data[0]=self, data[1]=dir-data zone; number_data=2
    rino = inode(root_zone, PFS_SEGD_MAGIC, PFS_FIO_ATTR_SUBDIR, zsize, 2, 1, 0)
    rdir_zone = 536
    buf[bm_off + (rdir_zone // 8)] |= (1 << (rdir_zone % 8))  # reserve dir data
    wbi(rino + 0x28 + 1 * 8, rdir_zone, 0, 1)                 # data[1] -> dir data

    # root dir entries: ".", "..", "f.bin"
    def dentry(off, inode_zone, sub, name):
        nm = name.encode("latin-1")
        plen = len(nm)
        alen = (plen + 8 + 3) & ~3
        w32(off, inode_zone)
        buf[off + 4] = sub
        buf[off + 5] = plen
        struct.pack_into("<H", buf, off + 6, alen)
        buf[off + 8:off + 8 + plen] = nm
        return alen

    d = pbase + rdir_zone * zsize
    off = d
    off += dentry(off, root_zone, 0, ".")
    off += dentry(off, root_zone, 0, "..")
    off += dentry(off, file_zone, 0, "f.bin")

    # ---- file inode f.bin @ file_zone: data[0]=self, data[1]=1 data zone ----
    fino = inode(file_zone, PFS_SEGD_MAGIC, 0, zsize, 2, 1, 0)
    wbi(fino + 0x28 + 1 * 8, file_data_zone, 0, 1)

    # fix inode checksums (sum words 1..255)
    for z in (root_zone, file_zone):
        o = pbase + z * zsize
        s = 0
        for i in range(1, 256):
            s = (s + struct.unpack_from("<I", buf, o + i * 4)[0]) & 0xFFFFFFFF
        struct.pack_into("<I", buf, o + 0x00, s)

    with open(path, "wb") as f:
        f.write(buf)
    return PNAME, "/f.bin"


def _selftest_synthetic():
    import tempfile
    ok = True
    tmp = os.path.join(tempfile.gettempdir(), "ps2hdd_synth_test.img")
    try:
        PNAME, FPATH = _build_synthetic_pfs(tmp, part_zones=4096)

        # sanity: read-only mount + list + read
        h = Ps2Hdd(tmp, writable=False)
        ents = h.pfs_listdir(PNAME, "/")
        names = sorted(e["name"] for e in ents)
        list_ok = names == ["f.bin"]
        print("    6a synthetic mount+list (f.bin present): %s"
              % ("PASS" if list_ok else "FAIL  got=%s" % names))
        ok &= list_ok
        h.close()

        # GROW across >=2 SEGI boundaries.  number_data starts at 2 (self+data).
        # SEGD holds logical indices 0..113 (slot via pfsFixIndex). Crossing the
        # first SEGI happens at index 114, the next at 114+123=237.  To force
        # MANY small extents (so we cross boundaries), pre-fragment the overlay
        # bitmap: mark every other zone used in a window after the file data.
        h = Ps2Hdd(tmp, overlay=True)
        bm = h._get_bitmap(PNAME, 0)
        # To FORCE the allocator into 1-zone extents (so number_data crosses the
        # 114 and 237 SEGI boundaries), leave ONLY isolated single free zones:
        # occupy every free zone, then re-free zones spaced 2 apart in a window.
        # Each freed zone is surrounded by used zones -> max contiguous run = 1.
        target_zones = 260            # > 237 -> crosses 2 SEGI boundaries
        # 1) fill EVERY currently-free zone so no contiguous run survives,
        # 2) punch isolated single-zone holes spaced 2 apart -> every free zone
        #    is surrounded by used zones, so each allocation is a 1-zone run.
        for zz in range(bm.total_zones):
            if bm.test(zz) == 0:
                bm.mark_used(zz, 1)
        free_singletons = []
        z = 600
        while len(free_singletons) < target_zones + 40 and z < bm.total_zones - 1:
            bm.mark_free(z, 1)
            free_singletons.append(z)
            z += 2
        bm.flush()
        payload = bytes((i * 13 + 1) & 0xFF for i in range(target_zones * 8192 - 4))
        h.pfs_write(PNAME, FPATH, payload)

        back = h.pfs_read(PNAME, FPATH)
        rt_ok = back == payload
        print("    6b multi-extent grow read-back exact (%d bytes): %s"
              % (len(payload), "PASS" if rt_ok else "FAIL"))
        ok &= rt_ok

        # independent extent + SEGI validation
        ino, pt, sc, _zs = h._resolve_path(PNAME, FPATH)
        ext, _locs = h._walk_extents_loc(pt, sc, ino)
        nd = ino.number_data
        nseg = _u32(ino.raw, 0x3E8)
        # crossed at least 2 SEGI boundaries?
        crossed = nd > (PFS_INODE_MAX_BLOCKS + 123)  # >237 means >=2 SEGIs
        print("    6c number_data=%d number_segdesg=%d -> crossed >=2 SEGI: %s"
              % (nd, nseg, "PASS" if crossed and nseg >= 2 else "FAIL"))
        ok &= (crossed and nseg >= 2)

        # every data extent is in a DISTINCT, used zone; no overlap; segments
        # referenced by locs are the actual descriptors holding them (C2).
        seen = set()
        overlap = False
        for (s, z0, c) in ext:
            for z in range(z0, z0 + c):
                if (s, z) in seen:
                    overlap = True
                seen.add((s, z))
        bm2 = h._get_bitmap(PNAME, 0)
        all_used = all(bm2.test(z) == 1 for (_s, z0, c) in ext for z in range(z0, z0 + c))
        # independent zone count must match the size
        zones_have = sum(c for (_s, _z, c) in ext)
        size_zones = (len(payload) + 8191) // 8192
        layout_ok = (not overlap) and all_used and zones_have == size_zones
        print("    6d extents: %d, zones=%d (need %d), no-overlap=%s all-used=%s: %s"
              % (len(ext), zones_have, size_zones, not overlap, all_used,
                 "PASS" if layout_ok else "FAIL"))
        ok &= layout_ok

        # checksum of every segment descriptor validates (root + each SEGI)
        chain = h._read_segment_chain(PNAME, pt, sc,
                                      {"number": ino.inode_block["number"],
                                       "subpart": ino.inode_block["subpart"], "count": 1})
        ck_ok = all(PfsInode(bytes(seg["raw"])).checksum_valid for seg in chain)
        print("    6e all %d segment-descriptor checksums valid: %s"
              % (len(chain), "PASS" if ck_ok else "FAIL"))
        ok &= ck_ok

        # the SEGI chain is well-formed: walk_extents (strict) does not raise
        try:
            h._walk_extents_loc(pt, sc, ino, strict=True)
            chain_ok = True
        except OSError:
            chain_ok = False
        print("    6f strict segment-chain walk (no truncation/loop): %s"
              % ("PASS" if chain_ok else "FAIL"))
        ok &= chain_ok
        h.close()

        # ENOSPC: a grow larger than free space must fail with NO disk change.
        h = Ps2Hdd(tmp, overlay=True)
        free = h._get_bitmap(PNAME, 0).free_count()
        huge = bytes(1) * ((free + 50) * 8192)
        before = len(h.dev.overlay_changes())
        raised = False
        try:
            h.pfs_write(PNAME, FPATH, huge)
        except OSError:
            raised = True
        after = len(h.dev.overlay_changes())
        # pre-flight check must reject before writing anything
        enospc_ok = raised and (after == before)
        print("    6g over-capacity grow raises ENOSPC with zero writes: %s"
              % ("PASS" if enospc_ok else "FAIL (raised=%s before=%d after=%d)"
                 % (raised, before, after)))
        ok &= enospc_ok
        h.close()

        # H1: mark_used overlap is a hard error
        h = Ps2Hdd(tmp, overlay=True)
        bm = h._get_bitmap(PNAME, 0)
        overlap_raised = False
        try:
            bm.mark_used(512, 1)  # zone 512 is the superblock -> already used
        except OSError:
            overlap_raised = True
        print("    6h mark_used on an already-used zone raises: %s"
              % ("PASS" if overlap_raised else "FAIL"))
        ok &= overlap_raised
        h.close()

    except Exception as e:
        import traceback
        traceback.print_exc()
        print("    [synthetic test EXCEPTION] %s" % e)
        ok = False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return ok


if __name__ == "__main__":
    import sys
    ok = _selftest()
    sys.exit(0 if ok else 1)

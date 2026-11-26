"""The batched HDD read/write paths must be byte-identical to the original
per-sector / per-extent implementations (kept here as reference copies)."""
import hashlib
import os
import shutil

import pytest

from taiko2x6 import safeio
from taiko2x6.ps2 import ps2hdd
from taiko2x6.ps2.ps2hdd import SECTOR, BlockDevice, Ps2Hdd


# ---- reference (pre-optimisation) implementations --------------------------
def old_read_sectors(self, lba, count):
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


def old_write_bytes(self, byte_off, data):
    length = len(data)
    first = byte_off // SECTOR
    last = (byte_off + length - 1) // SECTOR
    nsec = last - first + 1
    buf = bytearray(self.read_sectors(first, nsec))
    start = byte_off - first * SECTOR
    buf[start:start + length] = data
    self.write_sectors(first, bytes(buf))


def old_read_file_data(self, part, scale, root_inode, zone_size):
    extents = self._walk_extents(part, scale, root_inode)
    buf = bytearray()
    remaining = root_inode.size
    for (sub, zone, count) in extents:
        nbytes = count * zone_size
        lba = self._zone_to_lba(part, scale, sub, zone)
        buf += self.dev.read_sectors(lba, nbytes // SECTOR)
        if len(buf) >= remaining:
            break
    return bytes(buf[:remaining]), extents


def old_write_payload(self, part, scale, zs, extents, data, capacity):
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


@pytest.fixture
def use_old(monkeypatch):
    def apply():
        monkeypatch.setattr(BlockDevice, "read_sectors", old_read_sectors)
        monkeypatch.setattr(BlockDevice, "write_bytes", old_write_bytes)
        monkeypatch.setattr(Ps2Hdd, "_read_file_data", old_read_file_data)
        monkeypatch.setattr(Ps2Hdd, "_write_payload", old_write_payload)

    def undo():
        monkeypatch.undo()
    return apply, undo


def _sha(p):
    return hashlib.sha1(open(p, "rb").read()).hexdigest()


def _pattern(n, seed):
    return bytes(((i * seed + (i >> 8)) & 0xFF) for i in range(n))


# payload sizes: in-place shrink, exact multiples, odd sizes, multi-extent grow
SIZES = [1, 8191, 8192, 20_000, 70_001, 300_000, 1_000_003]


def _scenario(img, pname, fpath):
    """Series of writes + reads; returns list of read-backs."""
    outs = []
    for k, n in enumerate(SIZES):
        with ps2hdd.write_session(str(img)) as h:
            h.pfs_write(pname, fpath, _pattern(n, 3 + k))
        h = Ps2Hdd(str(img))
        outs.append(h.pfs_read(pname, fpath))
        h.close()
    return outs


def _journals(img):
    return sorted(img.parent.glob(img.name + ".*.undo"))


def test_writes_reads_identical_to_old(tmp_path, use_old):
    apply_old, undo_old = use_old
    base = tmp_path / "base.img"
    pname, fpath = ps2hdd._build_synthetic_pfs(str(base), part_zones=4096)

    new_img = tmp_path / "new.img"
    old_img = tmp_path / "old.img"
    shutil.copy(base, new_img)
    shutil.copy(base, old_img)

    new_reads = _scenario(new_img, pname, fpath)
    apply_old()
    old_reads = _scenario(old_img, pname, fpath)
    undo_old()

    assert new_reads == old_reads
    for k, n in enumerate(SIZES):
        assert new_reads[k] == _pattern(n, 3 + k)
    # metadata carries timestamps -> compare everything except those fields by
    # comparing data zones through reads (above) and image length + payload.
    assert os.path.getsize(new_img) == os.path.getsize(old_img)


def test_write_bytes_identical_including_unaligned(tmp_path, use_old):
    apply_old, undo_old = use_old
    img_a = tmp_path / "a.img"
    img_b = tmp_path / "b.img"
    with open(img_a, "wb") as f:
        f.write(os.urandom(SECTOR * 64))
    shutil.copy(img_a, img_b)
    cases = [(0, 512), (512, 1024), (3, 10), (500, 40), (1000, 3000),
             (5 * 512 + 7, 1), (8192, 4096)]

    def run(path):
        dev = BlockDevice(str(path), writable=True, undo=True)
        for off, n in cases:
            dev.write_bytes(off, bytes((off + i) & 0xFF for i in range(n)))
        dev.write_bytes(70 * SECTOR, b"\x11" * SECTOR)   # past EOF
        dev.close()
        return dev.undo.path

    ja = run(img_a)
    apply_old()
    jb = run(img_b)
    undo_old()
    assert _sha(img_a) == _sha(img_b)
    safeio.rollback(ja)
    safeio.rollback(jb)
    assert _sha(img_a) == _sha(img_b)


def test_overlay_read_matches_old(tmp_path, use_old):
    apply_old, undo_old = use_old
    img = tmp_path / "o.img"
    img.write_bytes(os.urandom(SECTOR * 40 + 100))      # ragged tail
    dev = BlockDevice(str(img), overlay=True)
    dev.write_sectors(3, b"\xAA" * SECTOR * 2)
    dev.write_sectors(39, b"\xBB" * SECTOR)
    dev.write_sectors(60, b"\xCC" * SECTOR)
    for lba, cnt in [(0, 50), (2, 3), (3, 1), (38, 4), (41, 3), (59, 3), (0, 1), (10, 0)]:
        new = dev.read_sectors(lba, cnt)
        apply_old()
        old = dev.read_sectors(lba, cnt)
        undo_old()
        assert new == old and len(new) == cnt * SECTOR
    dev.close()


def test_rollback_after_multi_extent_write(tmp_path):
    """Fragmented (many small extents) write must roll back exactly."""
    img = tmp_path / "f.img"
    pname, fpath = ps2hdd._build_synthetic_pfs(str(img), part_zones=4096)
    # fragment the free bitmap for real (undo disabled)
    with ps2hdd.write_session(str(img), undo=False) as h:
        bm = h._get_bitmap(pname, 0)
        for z in range(bm.total_zones):
            if bm.test(z) == 0:
                bm.mark_used(z, 1)
        z = 600
        for _ in range(80):
            bm.mark_free(z, 1)
            z += 2
        bm.flush()
    before = _sha(img)
    payload = _pattern(60 * 8192 - 5, 7)
    with ps2hdd.write_session(str(img)) as h:
        h.pfs_write(pname, fpath, payload)
    assert h.undo_path is not None
    h2 = Ps2Hdd(str(img))
    assert h2.pfs_read(pname, fpath) == payload
    h2.close()
    safeio.rollback(h.undo_path)
    assert _sha(img) == before

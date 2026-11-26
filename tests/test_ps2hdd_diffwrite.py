"""In-place PFS writes only write + journal the sectors that differ."""
import hashlib
import random
import time

from taiko2x6 import safeio
from taiko2x6.ps2 import ps2hdd
from taiko2x6.ps2.ps2hdd import SECTOR


def _sha(p):
    return hashlib.sha1(p.read_bytes()).hexdigest()


def _ref_write_payload(self, part, scale, zs, extents, data, capacity):
    """The pre-optimisation _write_payload (reference): writes everything."""
    mv = memoryview(data)
    dlen = len(data)
    total = capacity if capacity > dlen else dlen
    runs = []
    pos = 0
    for (sub, zone, count) in extents:
        nbytes = count * zs
        lba = self._zone_to_lba(part, scale, sub, zone)
        if (runs and runs[-1][0] + runs[-1][1] // SECTOR == lba
                and runs[-1][1] + nbytes <= self._WRITE_RUN_MAX):
            runs[-1][1] += nbytes
        else:
            runs.append([lba, nbytes])
        pos += nbytes
        if pos >= total:
            break
    pos = 0
    for lba, nbytes in runs:
        end = pos + nbytes
        if end <= dlen:
            chunk = mv[pos:end]
        else:
            chunk = bytearray(nbytes)
            if pos < dlen:
                chunk[:dlen - pos] = mv[pos:dlen]
        self.dev.write_bytes(lba * SECTOR, chunk)
        pos = end


def _big(tmp_path, zones=2048):
    img = tmp_path / "big.img"
    pname, fpath = ps2hdd._build_synthetic_pfs(str(img), part_zones=zones)
    return img, pname, fpath


def _prep(img, part, fpath, size):
    base = random.Random(1).randbytes(size)
    with ps2hdd.write_session(str(img), undo=False) as h:
        h.pfs_write(part, fpath, base)
    return base


def _scatter(base, offs):
    b = bytearray(base)
    for o in offs:
        b[o] ^= 0xFF
    return bytes(b)


def _nsec_diff(a, b):
    assert len(a) == len(b)
    return sum(a[i:i + SECTOR] != b[i:i + SECTOR]
               for i in range(0, len(a), SECTOR))


def _run(img, part, fpath, payload, ref=False):
    orig = ps2hdd.Ps2Hdd._write_payload
    counts = {"w": 0}
    real_ws = ps2hdd.BlockDevice.write_sectors

    def ws(self, lba, data):
        counts["w"] += len(data)
        return real_ws(self, lba, data)
    ps2hdd.BlockDevice.write_sectors = ws
    if ref:
        ps2hdd.Ps2Hdd._write_payload = (
            lambda self, part, scale, zs, ex, d, cap:
            _ref_write_payload(self, part, scale, zs, ex, d, cap))
    try:
        t = time.perf_counter()
        with ps2hdd.write_session(str(img)) as h:
            h.pfs_write(part, fpath, payload)
        dt = time.perf_counter() - t
    finally:
        ps2hdd.BlockDevice.write_sectors = real_ws
        ps2hdd.Ps2Hdd._write_payload = orig
    return h, counts["w"], dt


def test_scattered_changes_match_full_write_and_roll_back(tmp_path):
    img, part, fpath = _big(tmp_path)
    base = _prep(img, part, fpath, 3 * 1024 * 1024)
    pristine = _sha(img)
    payload = _scatter(base, [10, 700_000, 700_001, 2_500_000])
    copy = tmp_path / "copy.img"
    copy.write_bytes(img.read_bytes())

    h, wrote, _ = _run(img, part, fpath, payload)
    assert wrote < 4 * 64 * 1024
    # 3 changed sectors -> at most 3 journal blocks + inode sector's block
    assert 0 < h.dev.undo.bytes_saved <= 4 * 64 * 1024

    _run(copy, part, fpath, payload, ref=True)
    # identical image except the inode timestamps (root inode sector)
    assert _nsec_diff(img.read_bytes(), copy.read_bytes()) <= 1

    h2 = ps2hdd.Ps2Hdd(str(img))
    assert h2.pfs_read(part, fpath) == payload       # read-back verification
    h2.close()
    safeio.rollback(h.undo_path)
    assert _sha(img) == pristine


def test_unchanged_payload_writes_and_journals_nothing(tmp_path):
    img, part, fpath = _big(tmp_path)
    base = _prep(img, part, fpath, 1024 * 1024)
    before = _sha(img)
    h, wrote, _ = _run(img, part, fpath, base)
    assert wrote == 0 and h.undo_path is None
    assert not list(tmp_path.glob("*.undo"))
    assert _sha(img) == before


def test_unchanged_content_smaller_size_matches_full_write(tmp_path):
    """Shrink bypasses the no-op shortcut: diff path must equal full write."""
    img, part, fpath = _big(tmp_path)
    base = _prep(img, part, fpath, 600_000)
    payload = _scatter(base, [5, 300_000])[:500_000]
    copy = tmp_path / "c.img"
    copy.write_bytes(img.read_bytes())
    pristine = _sha(img)
    h, wrote, _ = _run(img, part, fpath, payload)
    _run(copy, part, fpath, payload, ref=True)
    assert _nsec_diff(img.read_bytes(), copy.read_bytes()) <= 1
    assert wrote < 600_000
    safeio.rollback(h.undo_path)
    assert _sha(img) == pristine


def test_growing_write_unchanged(tmp_path):
    img, part, fpath = _big(tmp_path)
    base = _prep(img, part, fpath, 100_000)
    payload = base + random.Random(2).randbytes(400_000)
    copy = tmp_path / "c.img"
    copy.write_bytes(img.read_bytes())
    _run(img, part, fpath, payload)
    _run(copy, part, fpath, payload, ref=True)
    assert _nsec_diff(img.read_bytes(), copy.read_bytes()) <= 1
    h2 = ps2hdd.Ps2Hdd(str(img))
    assert h2.pfs_read(part, fpath) == payload
    h2.close()


def test_measure(tmp_path, capsys):
    img, part, fpath = _big(tmp_path, zones=4096)
    base = _prep(img, part, fpath, 24 * 1024 * 1024)
    payload = _scatter(base, [1000, 9_000_000, 20_000_000])
    copy = tmp_path / "c.img"
    copy.write_bytes(img.read_bytes())
    h1, w1, t1 = _run(img, part, fpath, payload)
    h0, w0, t0 = _run(copy, part, fpath, payload, ref=True)
    with capsys.disabled():
        print(f"\nBEFORE written={w0} journal={h0.dev.undo.bytes_saved} t={t0:.3f}s"
              f"\nAFTER  written={w1} journal={h1.dev.undo.bytes_saved} t={t1:.3f}s")
    assert w1 < w0 and h1.dev.undo.bytes_saved < h0.dev.undo.bytes_saved

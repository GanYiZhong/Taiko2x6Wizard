import hashlib

import pytest

from taiko2x6.ps2 import ps2hdd
from taiko2x6 import safeio


def _sha(p):
    return hashlib.sha1(p.read_bytes()).hexdigest()


def _undo_files(img):
    return sorted(img.parent.glob(img.name + ".*.undo"))


def test_read_only_open_makes_no_journal(pfs_image):
    img, part, fpath = pfs_image
    h = ps2hdd.Ps2Hdd(str(img))
    assert h.pfs_read(part, fpath) is not None
    h.close()
    assert _undo_files(img) == []


def test_write_session_keeps_journal_that_restores(pfs_image):
    img, part, fpath = pfs_image
    before = _sha(img)
    payload = bytes((i * 5) & 0xFF for i in range(20_000))   # needs new zones
    logs = []
    with ps2hdd.write_session(str(img), log=logs.append) as h:
        h.pfs_write(part, fpath, payload)
    assert h.undo_path is not None and h.undo_path.exists()
    assert any("undo journal" in m for m in logs)
    h2 = ps2hdd.Ps2Hdd(str(img))
    assert h2.pfs_read(part, fpath) == payload
    h2.close()
    safeio.rollback(h.undo_path)
    assert _sha(img) == before


def test_write_session_rolls_back_on_error(pfs_image):
    img, part, fpath = pfs_image
    before = _sha(img)
    with pytest.raises(RuntimeError):
        with ps2hdd.write_session(str(img)) as h:
            h.pfs_write(part, fpath, b"\xAB" * 30_000)
            raise RuntimeError("second write failed")
    assert _sha(img) == before
    assert _undo_files(img) == []                 # used journal is removed


def test_noop_session_leaves_no_journal(pfs_image):
    img, _part, _f = pfs_image
    with ps2hdd.write_session(str(img)) as h:
        pass
    assert h.undo_path is None and _undo_files(img) == []


def test_undo_false_skips_journal(pfs_image):
    img, part, fpath = pfs_image
    with ps2hdd.write_session(str(img), undo=False) as h:
        h.pfs_write(part, fpath, b"x" * 100)
    assert _undo_files(img) == []


def test_grow_partition_refuses_oversize_without_writing(pfs_image):
    img, part, _f = pfs_image
    before = _sha(img)
    h = ps2hdd.Ps2Hdd(str(img), writable=True)
    try:
        with pytest.raises(ValueError):
            h.grow_partition(part, 3 * 1024 ** 3)
    finally:
        h.close()
    assert _sha(img) == before


def test_memory_card_backup_on_writable_open(tmp_path):
    from taiko2x6.ps2 import ps2mc_card
    card = tmp_path / "card.ps2"
    card.write_bytes(b"\0" * 1024)               # not a real card: init fails
    with pytest.raises(Exception):
        ps2mc_card.Ps2mcCard(card, writable=True)
    # the backup is taken before the card is parsed, so it exists anyway
    assert (tmp_path / "card.ps2.bak").read_bytes() == b"\0" * 1024

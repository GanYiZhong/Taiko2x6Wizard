"""Fault-injection proof of the crash-safety promises in taiko2x6.safeio.

Every interruption point of a DATA.000 + list.bin save/rebuild
(``write_pair``) and of an in-place image write (``write_session`` +
``UndoJournal``) is hit one at a time. Two failure models are used:

* KILL   - a ``BaseException`` raised at op N which then *keeps failing* for
           every later op, so no cleanup/rollback code can run: this freezes the
           disk exactly as a power cut / ``kill -9`` would. Recovery (open the
           archive / ``safeio.rollback``) must then give the complete OLD state
           or the complete NEW state, never a mix, and leave no ``*.tmp`` /
           ``*.txn`` / ``*.txn-old`` behind.
* ENOSPC - ``OSError(ENOSPC)`` once at op N, the process survives and its own
           error handling must leave the OLD state with nothing stray.

Everything is synthetic and tiny (<10 s for the whole file).
"""
from __future__ import annotations

import errno
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from taiko2x6 import safeio
from taiko2x6.archive.archive_model import Archive
from taiko2x6.ps2 import ps2hdd

from conftest import SAMPLE_GROUPS, build_archive


class Killed(BaseException):
    """Simulated power cut (BaseException so no ``except Exception`` eats it)."""


class _FileProxy:
    """Wraps a file from os.fdopen so each ``write`` is an interruption point."""

    def __init__(self, f, inj):
        self._f, self._inj = f, inj

    def write(self, data):
        if self._inj.step("write"):
            half = len(data) // 2          # torn write: half lands, then death
            if half and self._inj.mode == "kill":
                self._f.write(bytes(data[:half]))
                self._f.flush()
            self._inj.fire()
        return self._f.write(data)

    def __getattr__(self, name):
        return getattr(self._f, name)

    def __enter__(self):
        self._f.__enter__()
        return self

    def __exit__(self, *a):
        return self._f.__exit__(*a)


class Injector:
    """Counts interruption points; fails at number ``at`` (1-based)."""

    def __init__(self, at=None, mode="kill", sticky=True):
        self.at, self.mode, self.sticky = at, mode, sticky
        self.count = 0
        self.dead = False
        self.on_fire = None

    def step(self, label) -> bool:
        if self.dead and self.sticky and self.mode == "kill":
            self.fire()
        self.count += 1
        return self.at == self.count

    def fire(self):
        if self.on_fire and not self.dead:
            self.on_fire()
        self.dead = True
        if self.mode == "kill":
            raise Killed()
        raise OSError(errno.ENOSPC, "No space left on device")

    def hook(self, label, fn):
        def wrapper(*a, **k):
            if self.step(label):
                self.fire()
            return fn(*a, **k)
        return wrapper


class patched_fs:
    """Context manager that makes every durable filesystem op of safeio an
    interruption point (replace, fsync, unlink, mkstemp, copy2, file write)."""

    def __init__(self, inj):
        self.inj = inj

    def __enter__(self):
        i = self.inj
        self.saved = (os.replace, os.fsync, os.unlink, os.fdopen,
                      tempfile.mkstemp, shutil.copy2)
        os.replace = i.hook("replace", os.replace)
        os.fsync = i.hook("fsync", os.fsync)
        os.unlink = i.hook("unlink", os.unlink)
        tempfile.mkstemp = i.hook("mkstemp", tempfile.mkstemp)
        shutil.copy2 = i.hook("copy2", shutil.copy2)
        # safeio writes temp files through its module-level ``open``
        safeio.open = lambda f, mode="r", *a, **k: (
            _FileProxy(open(f, mode, *a, **k), i) if "w" in mode
            else open(f, mode, *a, **k))
        return self

    def __exit__(self, *a):
        (os.replace, os.fsync, os.unlink, os.fdopen,
         tempfile.mkstemp, shutil.copy2) = self.saved
        del safeio.open


def _sha(p):
    return hashlib.sha1(Path(p).read_bytes()).hexdigest()


def _state(d: Path, names):
    return tuple(None if not (d / n).exists() else (d / n).read_bytes()
                 for n in names)


def _stray(d: Path):
    return sorted(p.name for p in d.iterdir()
                  if p.name.endswith((".tmp", ".txn", ".txn-old")))


def _fresh(d: Path, with_old=True):
    d.mkdir(parents=True, exist_ok=True)
    if with_old:
        lb, db = build_archive(SAMPLE_GROUPS)
        (d / "list.bin").write_bytes(lb)
        (d / "DATA.000").write_bytes(db)
    return d / "list.bin", d / "DATA.000"


def _find(a, gname, fname):
    for g in a.layout.groups:
        if g["name"] == gname:
            for e in a.layout.files_for_group(g):
                if e["name"] == fname:
                    return g, e
    raise KeyError


BIG = bytes((i * 31) & 0xFF for i in range(9000))


def _op_save(d, backup):
    lp, dp = d / "list.bin", d / "DATA.000"
    a = Archive(lp, dp)
    g, e = _find(a, "song.abc", "abc_m.sht")
    a.stage_replace(g["index"], e["index"], b"N" * 290)
    a.close()                       # Windows: no mmap may pin the target
    a.save(lp, dp, allow_relayout=False, backup=backup)


def _op_rebuild(d, backup):
    """Rebuild into NEW output files (targets did not exist before)."""
    a = Archive(d / "list.bin", d / "DATA.000")
    g, e = _find(a, "song.xyz", "xyz_m.sht")
    a.stage_replace(g["index"], e["index"], BIG)
    try:
        a.rebuild(d / "out_list.bin", d / "out_DATA.000", backup=backup)
    finally:
        a.close()


def _op_rebuild_over(d, backup):
    """Rebuild over the existing pair (the common GUI path)."""
    a = Archive(d / "list.bin", d / "DATA.000")
    g, e = _find(a, "song.xyz", "xyz_m.sht")
    a.stage_replace(g["index"], e["index"], BIG)
    a.close()                       # Windows: no mmap may pin the target
    a.rebuild(d / "list.bin", d / "DATA.000", backup=backup)


SCENARIOS = {
    # name: (operation, files in the transaction, lead (txn) file)
    "save_inplace": (_op_save, ("list.bin", "DATA.000"), "DATA.000"),
    "rebuild_new": (_op_rebuild, ("out_list.bin", "out_DATA.000"), "out_DATA.000"),
    "rebuild_over": (_op_rebuild_over, ("list.bin", "DATA.000"), "DATA.000"),
}


def _reference(tmp_path, name):
    op, files, _ = SCENARIOS[name]
    d = tmp_path / f"ref_{name}"
    _fresh(d)
    old = _state(d, files)
    op(d, False)
    new = _state(d, files)
    assert old != new
    return old, new


def _interruption_points(tmp_path, name, backup):
    d = tmp_path / f"count_{name}_{backup}"
    _fresh(d)
    inj = Injector(None)
    with patched_fs(inj):
        SCENARIOS[name][0](d, backup)
    assert inj.count > 8
    return inj.count


@pytest.mark.parametrize("backup", [False, True])
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_kill_at_every_step_recovers_to_old_or_new(tmp_path, name, backup):
    op, files, lead = SCENARIOS[name]
    old, new = _reference(tmp_path, name)
    n = _interruption_points(tmp_path, name, backup)
    outcomes = set()
    for k in range(1, n + 1):
        d = tmp_path / f"{name}_{backup}_k{k}"
        _fresh(d)
        inj = Injector(k, "kill")
        with patched_fs(inj):
            with pytest.raises(Killed):
                op(d, backup)
        # ---- next "program start": opening the archive / recovery ----
        safeio.recover_pair(safeio.txn_path(d / lead))
        st = _state(d, files)
        assert st in (old, new), f"{name} kill@{k}/{n}: mixed or corrupt state"
        outcomes.add("new" if st == new else "old")
        assert _stray(d) == [], f"{name} kill@{k}/{n}: stray {_stray(d)}"
        if st == old:
            # the interrupted file pair must also be re-saveable afterwards
            op(d, backup)
            assert _state(d, files) == new, f"{name} kill@{k}/{n}: re-save differs"
            assert _stray(d) == []
    assert outcomes == {"old", "new"}      # both sides of the commit point seen


def test_archive_open_recovers_every_kill_point(tmp_path):
    """Same as above but recovery is done by Archive.__init__ (what users hit)."""
    old, new = _reference(tmp_path, "save_inplace")
    n = _interruption_points(tmp_path, "save_inplace", False)
    for k in range(1, n + 1):
        d = tmp_path / f"open_k{k}"
        _fresh(d)
        with patched_fs(Injector(k, "kill")):
            with pytest.raises(Killed):
                _op_save(d, False)
        a = Archive(d / "list.bin", d / "DATA.000")      # must open cleanly
        a.close()
        assert _state(d, ("list.bin", "DATA.000")) in (old, new)
        assert _stray(d) == []


def test_crash_during_recovery_is_itself_recoverable(tmp_path):
    """Kill the save at every step, then kill the RECOVERY at every step, then
    recover again: still old-or-new, still nothing stray."""
    op, files, lead = SCENARIOS["rebuild_over"]
    old, new = _reference(tmp_path, "rebuild_over")
    n = _interruption_points(tmp_path, "rebuild_over", False)
    seen = 0
    for k in range(1, n + 1):
        d0 = tmp_path / f"r{k}"
        _fresh(d0)
        with patched_fs(Injector(k, "kill")):
            with pytest.raises(Killed):
                op(d0, False)
        jr = safeio.txn_path(d0 / lead)
        probe = tmp_path / f"probe{k}"
        shutil.copytree(d0, probe)
        inj = Injector(None)
        with patched_fs(inj):
            safeio.recover_pair(safeio.txn_path(probe / lead))
        for k2 in range(1, inj.count + 1):
            seen += 1
            d = tmp_path / f"r{k}_{k2}"
            shutil.copytree(d0, d)
            with patched_fs(Injector(k2, "kill")):
                try:
                    safeio.recover_pair(safeio.txn_path(d / lead))
                except Killed:
                    pass
            safeio.recover_pair(safeio.txn_path(d / lead))
            assert _state(d, files) in (old, new), f"k={k} k2={k2}"
            assert _stray(d) == [], f"k={k} k2={k2}: {_stray(d)}"
    assert seen > 10


@pytest.mark.parametrize("backup", [False, True])
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_disk_full_at_every_step_leaves_old_state(tmp_path, name, backup):
    op, files, lead = SCENARIOS[name]
    old, new = _reference(tmp_path, name)
    n = _interruption_points(tmp_path, name, backup)
    for k in range(1, n + 1):
        d = tmp_path / f"{name}_{backup}_e{k}"
        _fresh(d)
        inj = Injector(k, "enospc")
        with patched_fs(inj):
            try:
                op(d, backup)
            except OSError as e:
                assert e.errno == errno.ENOSPC
        # whatever happened in-process, recovery finds a consistent pair
        safeio.recover_pair(safeio.txn_path(d / lead))
        st = _state(d, files)
        assert st in (old, new), f"{name} enospc@{k}/{n}: mixed state"
        assert _stray(d) == [], f"{name} enospc@{k}/{n}: stray {_stray(d)}"


def test_torn_backup_is_not_left_as_known_good_original(tmp_path):
    """A kill while copying the .bak must not leave a truncated DATA.000.bak
    that later passes for the original."""
    d = tmp_path / "bak"
    _fresh(d)
    want = (d / "DATA.000").read_bytes()
    real = shutil.copy2

    def torn(src, dst, *a, **k):
        with open(src, "rb") as s, open(dst, "wb") as o:
            o.write(s.read(len(want) // 2))
        raise Killed()

    shutil.copy2 = torn
    try:
        with pytest.raises(Killed):
            safeio.make_backup(d / "DATA.000")
    finally:
        shutil.copy2 = real
    bak = d / "DATA.000.bak"
    assert not bak.exists() or bak.read_bytes() == want


# --------------------------------------------------------------------------- #
#  in-place image writes: write_session + UndoJournal
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def base_image(tmp_path_factory):
    d = tmp_path_factory.mktemp("img")
    p = d / "base.img"
    pname, fpath = ps2hdd._build_synthetic_pfs(str(p), part_zones=1024)
    return p, pname, fpath


PAYLOAD = bytes((i * 5) & 0xFF for i in range(40_000))


def _img_session(img, part, fpath):
    with ps2hdd.write_session(str(img), log=lambda m: None) as h:
        h.pfs_write(part, fpath, PAYLOAD)


class _ImgHooks:
    """Interruption points of an image session: journal saves, device writes
    (including torn ones), fsyncs. Snapshots the disk at the moment of death."""

    def __init__(self, inj, img, snap):
        self.inj, self.img, self.snap = inj, Path(img), Path(snap)
        inj.on_fire = self.snapshot

    def snapshot(self):
        if self.inj.mode != "kill":
            return
        self.snap.mkdir(exist_ok=True)
        shutil.copyfile(self.img, self.snap / self.img.name)
        for j in self.img.parent.glob(self.img.name + ".*.undo"):
            shutil.copyfile(j, self.snap / j.name)

    def __enter__(self):
        i = self.inj
        self.saved = (os.fsync, ps2hdd.BlockDevice.write_sectors,
                      safeio.UndoJournal.save)
        os.fsync = i.hook("fsync", os.fsync)
        real_ws, real_save = self.saved[1], self.saved[2]

        def write_sectors(dev, lba, data):
            if i.step("write_sectors"):
                if i.mode == "kill" and len(data) > 512:
                    real_ws(dev, lba, data[:(len(data) // 1024) * 512])  # torn
                i.fire()
            return real_ws(dev, lba, data)

        def save(j, f, off, ln):
            if i.step("journal.save"):
                i.fire()
            return real_save(j, f, off, ln)

        ps2hdd.BlockDevice.write_sectors = write_sectors
        safeio.UndoJournal.save = save
        return self

    def __exit__(self, *a):
        (os.fsync, ps2hdd.BlockDevice.write_sectors,
         safeio.UndoJournal.save) = self.saved


def _copy_img(base, d):
    d.mkdir(parents=True, exist_ok=True)
    img = d / "t.img"
    shutil.copyfile(base, img)
    return img


def test_image_kill_at_every_step_rolls_back_exactly(tmp_path, base_image):
    base, part, fpath = base_image
    before = _sha(base)
    # count interruption points
    img = _copy_img(base, tmp_path / "count")
    inj = Injector(None)
    with _ImgHooks(inj, img, tmp_path / "x"):
        _img_session(img, part, fpath)
    n = inj.count
    assert n >= 6
    new_sha = _sha(img)
    assert new_sha != before
    for k in range(1, n + 1):
        d = tmp_path / f"k{k}"
        img = _copy_img(base, d)
        inj = Injector(k, "kill", sticky=False)
        snap = tmp_path / f"snap{k}"
        with _ImgHooks(inj, img, snap):
            with pytest.raises(Killed):
                _img_session(img, part, fpath)
        # the live files went through the in-process handler; the snapshot is
        # the frozen post-crash disk: only the journal can bring it back.
        simg = snap / img.name
        journals = list(snap.glob("*.undo"))
        if journals:
            assert len(journals) == 1
            safeio.rollback(journals[0], simg)
        assert _sha(simg) == before, f"kill@{k}/{n}: not restored exactly"
        # and the in-process handler also restored the live image + cleaned up
        assert _sha(img) == before
        assert list(d.glob("*.undo")) == []


def test_image_disk_full_at_every_step_rolls_back(tmp_path, base_image):
    base, part, fpath = base_image
    before = _sha(base)
    img = _copy_img(base, tmp_path / "count")
    inj = Injector(None)
    with _ImgHooks(inj, img, tmp_path / "x"):
        _img_session(img, part, fpath)
    n = inj.count
    for k in range(1, n + 1):
        d = tmp_path / f"e{k}"
        img = _copy_img(base, d)
        inj = Injector(k, "enospc")
        with _ImgHooks(inj, img, tmp_path / f"s{k}"):
            try:
                _img_session(img, part, fpath)
                failed = False
            except OSError as e:
                assert e.errno == errno.ENOSPC
                failed = True
        undo = list(d.glob("*.undo"))
        if failed:
            assert _sha(img) == before, f"enospc@{k}/{n}: not rolled back"
            assert undo == []
        else:                       # fsync failure is tolerated by design
            assert len(undo) == 1
            safeio.rollback(undo[0], img)
            assert _sha(img) == before


# --------------------------------------------------------------------------- #
#  undo journal truncated at every byte offset
# --------------------------------------------------------------------------- #
def _journal_fixture(tmp_path):
    p = tmp_path / "small.img"
    orig = bytes((i * 13 + 5) & 0xFF for i in range(1000)) + bytes(200)
    p.write_bytes(orig)
    j = safeio.UndoJournal(p, block=64)
    with open(p, "r+b") as f:
        for off, data in [(10, b"A" * 100), (500, b"B" * 70),
                          (1050, b"C" * 90),               # zero region
                          (len(orig) - 40, b"D" * 100)]:    # grows the file
            j.save(f, off, len(data))
            f.seek(off)
            f.write(data)
    j.close()
    return p, j.path, orig, p.read_bytes()


def test_truncated_journal_never_writes_garbage(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda fd: None)     # speed only
    p, jpath, orig, modified = _journal_fixture(tmp_path)
    jbytes = jpath.read_bytes()
    assert len(jbytes) > 20
    hdr = safeio._HDR.size
    diff = [i for i in range(len(orig)) if orig[i] != (modified[i] if i < len(modified) else None)]
    for cut in range(len(jbytes) + 1):
        work = tmp_path / "w"
        work.mkdir(exist_ok=True)
        t = work / "small.img"
        t.write_bytes(modified)
        jt = work / "small.img.20260101-000000.undo"
        jt.write_bytes(jbytes[:cut])
        if cut < hdr:
            with pytest.raises(ValueError):          # refuses, touches nothing
                safeio.rollback(jt, t)
            assert t.read_bytes() == modified
            continue
        safeio.rollback(jt, t)
        got = t.read_bytes()
        assert len(got) == len(orig), f"cut {cut}: size {len(got)}"
        masked = bytearray(got)
        for i in diff:                  # each touched byte: old OR new value
            assert got[i] in (orig[i], modified[i]), f"cut {cut}: garbage @{i}"
            masked[i] = orig[i]
        assert bytes(masked) == orig, f"cut {cut}: untouched byte changed"
        if cut == len(jbytes):
            assert got == orig
        # rolling back again is harmless (a crash during rollback can be retried)
        if cut % 16 == 0:
            safeio.rollback(jt, t)
            assert t.read_bytes() == got
    # also: every info/ranges listing copes with any truncation
    for cut in range(hdr, len(jbytes) + 1, 7):
        jt = tmp_path / "w" / "i.undo"
        jt.write_bytes(jbytes[:cut])
        safeio.journal_info(jt)
        safeio.journal_ranges(jt)


def test_rollback_rejects_foreign_or_corrupt_header(tmp_path):
    t = tmp_path / "x.img"
    t.write_bytes(b"\x07" * 100)
    j = tmp_path / "x.img.20260101-000000.undo"
    j.write_bytes(b"NOTAJOURNAL" + b"\0" * 40)
    with pytest.raises(ValueError):
        safeio.rollback(j, t)
    assert t.read_bytes() == b"\x07" * 100


def test_rollback_interrupted_midway_can_be_rerun(tmp_path):
    p, jpath, orig, modified = _journal_fixture(tmp_path)
    jbytes = jpath.read_bytes()
    n = safeio.journal_info(jpath)["records"]
    real = os.fsync
    # die inside rollback right before the final fsync: data was written, the
    # truncate already ran; a re-run must still finish the job.
    os.fsync = lambda fd: (_ for _ in ()).throw(Killed())
    try:
        with pytest.raises(Killed):
            safeio.rollback(jpath, p)
    finally:
        os.fsync = real
    safeio.rollback(jpath, p)
    assert p.read_bytes() == orig
    assert n >= 3 and jbytes

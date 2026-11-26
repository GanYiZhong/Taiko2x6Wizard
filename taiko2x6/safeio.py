"""Crash-safe file writing shared by every tool that touches game data.

Three layers, from cheapest to most general:

* ``atomic_write(path, data)`` - write a sibling temp file, fsync, then
  ``os.replace`` it over the target. A crash leaves either the old file or the
  new one, never a truncated mix.
* ``write_pair(...)`` / ``recover_pair(...)`` - commit SEVERAL files (DATA.000 +
  list.bin) as one transaction. A journal next to the first file records the
  plan; if the process dies half way, ``recover_pair`` (run automatically when
  an archive is opened) rolls back to the old pair, so DATA.000 and list.bin can
  never disagree with each other.
* ``UndoJournal`` - a write-ahead undo log for files modified IN PLACE (PS2 HDD
  images, ISOs, memory cards). Before any byte range is overwritten for the
  first time, its original contents are appended to ``<file>.<stamp>.undo`` and
  fsynced. ``rollback(journal)`` restores the file exactly, including its
  original length. This costs only as much disk as the data actually
  overwritten (about 1 GB for a DATA.000 rewrite) instead of a full 6-80 GB
  copy of the image.

CLI::

    python -m taiko2x6.safeio info     TK14.img.20261118-101500.undo
    python -m taiko2x6.safeio rollback TK14.img.20261118-101500.undo
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path

__all__ = [
    "atomic_write", "make_backup", "write_pair", "recover_pair", "txn_path",
    "UndoJournal", "rollback", "journal_info", "undo_path_for",
]


# --------------------------------------------------------------------------- #
#  small helpers
# --------------------------------------------------------------------------- #
def _fsync_dir(d: Path):
    """Best-effort directory fsync so renames are durable (no-op on Windows)."""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _replace(src, dst, attempts: int = 8):
    """os.replace with retries: on Windows the target can be briefly locked by
    a just-closed mmap section or a viewer/emulator holding the file."""
    for attempt in range(attempts):
        try:
            os.replace(str(src), str(dst))
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            gc.collect()
            time.sleep(0.25)


def _write_temp(path: Path, data) -> Path:
    """``data`` is a bytes-like object or a callable ``writer(fileobj)`` that
    streams the content (so a multi-GB file never has to sit in memory)."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            if callable(data):
                data(f)
            else:
                f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return Path(tmp)


# --------------------------------------------------------------------------- #
#  single-file atomic write + rolling backups
# --------------------------------------------------------------------------- #
def atomic_write(path, data) -> None:
    """Write ``data`` to ``path`` atomically (temp file + fsync + os.replace)."""
    path = Path(path)
    tmp = _write_temp(path, data)
    try:
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def make_backup(path) -> Path | None:
    """Copy ``path`` to a backup before it is overwritten; never clobbers one.

    The first backup is ``<file>.bak`` (the known-good original); later ones
    are ``<file>.bak.1``, ``.bak.2``, ... Returns the backup path, or None if
    ``path`` does not exist."""
    path = Path(path)
    if not path.exists():
        return None
    bak = path.with_name(path.name + ".bak")
    n = 0
    while bak.exists():
        n += 1
        bak = path.with_name(path.name + f".bak.{n}")
    shutil.copy2(str(path), str(bak))
    return bak


# --------------------------------------------------------------------------- #
#  multi-file transaction (DATA.000 + list.bin)
# --------------------------------------------------------------------------- #
def txn_path(first_target) -> Path:
    """Journal file used by write_pair for a transaction led by this file."""
    p = Path(first_target)
    return p.with_name(p.name + ".txn")


def write_pair(files, backup: bool = True, log=None) -> list[Path]:
    """Replace several files as one all-or-nothing transaction.

    ``files`` is a sequence of ``(path, data)``; ``data`` is bytes-like or a
    callable ``writer(fileobj)`` that streams the content. Steps:

    1. optional rolling ``.bak`` of each existing target;
    2. every new file is written to a temp sibling and fsynced;
    3. a journal (``<first>.txn``) records targets, temps and parked originals;
    4. each original is renamed aside, each temp renamed into place;
    5. originals and journal are deleted.

    Any error in 2-4 rolls everything back. A crash in 4 is rolled back by
    ``recover_pair`` the next time the files are opened. All files must be on
    one volume (renames). Returns the list of backup paths made.
    """
    files = [(Path(p), d) for p, d in files]
    if not files:
        return []
    journal = txn_path(files[0][0])
    if journal.exists():
        recover_pair(journal, log=log)

    backups = []
    if backup:
        for p, _ in files:
            b = make_backup(p)
            if b is not None:
                backups.append(b)
                if log:
                    log(f"backup {p.name} -> {b.name}")

    entries = []
    try:
        for p, data in files:
            tmp = _write_temp(p, data)
            old = p.with_name(p.name + ".txn-old")
            entries.append({"target": str(p), "tmp": str(tmp), "old": str(old),
                            "had_old": p.exists()})
        atomic_write(journal, json.dumps({"version": 1, "files": entries},
                                         indent=1).encode())
        for e in entries:
            if e["had_old"]:
                _replace(e["target"], e["old"])
        for e in entries:
            _replace(e["tmp"], e["target"])
    except BaseException:
        _rollback_entries(entries)
        try:
            journal.unlink()
        except OSError:
            pass
        raise

    # committed: drop the parked originals, then the journal
    for e in entries:
        try:
            os.unlink(e["old"])
        except OSError:
            pass
    try:
        journal.unlink()
    except OSError:
        pass
    _fsync_dir(files[0][0].parent)
    return backups


def _rollback_entries(entries):
    for e in entries:
        tgt, tmp, old = Path(e["target"]), Path(e["tmp"]), Path(e["old"])
        if old.exists():
            # an original was parked: put it back (over a new file if present)
            _replace(old, tgt)
        elif not e.get("had_old") and tgt.exists() and not tmp.exists():
            # target did not exist before and the new one was renamed in
            try:
                tgt.unlink()
            except OSError:
                pass
        try:
            tmp.unlink()
        except OSError:
            pass


def recover_pair(journal, log=None) -> str | None:
    """Finish or undo an interrupted ``write_pair``.

    If every temp file was already renamed into place the transaction had
    committed, so only the parked originals are cleaned up ("committed").
    Otherwise the originals are put back ("rolled back"). Returns None when
    there is no journal."""
    journal = Path(journal)
    if not journal.exists():
        return None
    try:
        entries = json.loads(journal.read_bytes())["files"]
    except (ValueError, KeyError):
        # torn journal: it is written atomically, so this means it was never
        # completed -> nothing was renamed yet; just remove stray temps.
        journal.unlink()
        return "discarded"
    committed = all(not Path(e["tmp"]).exists() for e in entries) and \
        all(Path(e["target"]).exists() for e in entries)
    if committed:
        for e in entries:
            try:
                os.unlink(e["old"])
            except OSError:
                pass
        state = "committed"
    else:
        _rollback_entries(entries)
        state = "rolled back"
    journal.unlink()
    if log:
        log(f"recovered interrupted write ({state}): "
            + ", ".join(Path(e['target']).name for e in entries))
    return state


# --------------------------------------------------------------------------- #
#  in-place undo journal
# --------------------------------------------------------------------------- #
_MAGIC = b"T2X6UNDO"
_HDR = struct.Struct("<8sIQ")        # magic, version, original file size
_REC = struct.Struct("<QIB")          # offset, length, flags
_FLAG_ZERO = 1                        # original range was all zero (no payload)


def undo_path_for(target) -> Path:
    """Default journal name: ``<file>.<YYYYmmdd-HHMMSS>.undo`` (unique)."""
    t = Path(target)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    p = t.with_name(f"{t.name}.{stamp}.undo")
    n = 1
    while p.exists():
        p = t.with_name(f"{t.name}.{stamp}-{n}.undo")
        n += 1
    return p


class UndoJournal:
    """Write-ahead undo log for a file that is about to be modified in place.

    Call ``save(f, offset, length)`` BEFORE writing that range through ``f``.
    The first time a block is seen its current bytes are appended to the
    journal and fsynced; blocks already saved are skipped, so the journal
    always holds the state from before this session. Ranges beyond the
    original end of file are not saved: rollback truncates them away.
    """

    def __init__(self, target, journal=None, block: int = 65536):
        self.target = Path(target)
        self.path = Path(journal) if journal else undo_path_for(self.target)
        self.block = block
        self.orig_size = os.path.getsize(self.target)
        self._saved: set[int] = set()
        self._f = open(self.path, "xb")
        self._f.write(_HDR.pack(_MAGIC, 1, self.orig_size))
        self._f.flush()
        os.fsync(self._f.fileno())
        self.bytes_saved = 0

    def save(self, f, offset: int, length: int):
        """Record the current contents of ``[offset, offset+length)`` of the
        open target file ``f`` (seek position is not preserved)."""
        if length <= 0:
            return
        b = self.block
        first = offset // b
        last = min((offset + length - 1) // b, (self.orig_size - 1) // b)
        runs = []
        run_start = None
        for blk in range(first, last + 1):
            if blk in self._saved:
                if run_start is not None:
                    runs.append((run_start, blk))
                    run_start = None
                continue
            if run_start is None:
                run_start = blk
        if run_start is not None:
            runs.append((run_start, last + 1))
        if not runs:
            return
        for s, e in runs:
            off = s * b
            ln = min(e * b, self.orig_size) - off
            f.seek(off)
            data = f.read(ln)
            if data.count(0) == len(data):
                self._f.write(_REC.pack(off, len(data), _FLAG_ZERO))
            else:
                self._f.write(_REC.pack(off, len(data), 0))
                self._f.write(data)
            self.bytes_saved += len(data)
            self._saved.update(range(s, e))
        self._f.flush()
        os.fsync(self._f.fileno())

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass

    def discard(self):
        """Delete the journal (only when the caller knows nothing was written
        or the result has been verified and the undo is not wanted)."""
        self.close()
        try:
            self.path.unlink()
        except OSError:
            pass


def _iter_records(journal: Path):
    with open(journal, "rb") as j:
        hdr = j.read(_HDR.size)
        if len(hdr) < _HDR.size:
            raise ValueError(f"{journal}: not an undo journal (too short)")
        magic, ver, orig_size = _HDR.unpack(hdr)
        if magic != _MAGIC or ver != 1:
            raise ValueError(f"{journal}: not an undo journal")
        yield ("header", orig_size)
        while True:
            rh = j.read(_REC.size)
            if len(rh) < _REC.size:
                return                       # clean end or torn header
            off, ln, flags = _REC.unpack(rh)
            if flags & _FLAG_ZERO:
                yield ("rec", off, ln, None)
                continue
            data = j.read(ln)
            if len(data) < ln:
                return                       # torn tail: its data write never ran
            yield ("rec", off, ln, data)


def journal_info(journal) -> dict:
    it = _iter_records(Path(journal))
    _, orig = next(it)
    n = total = 0
    for _, off, ln, _d in it:
        n += 1
        total += ln
    return {"original_size": orig, "records": n, "bytes": total}


def rollback(journal, target=None, log=None) -> dict:
    """Restore ``target`` (default: journal name minus ``.<stamp>.undo``) to
    the state it had before the journaled session."""
    journal = Path(journal)
    if target is None:
        name = journal.name
        if not name.endswith(".undo"):
            raise ValueError("pass the target file explicitly")
        base = name[:-len(".undo")]
        base = base.rsplit(".", 1)[0]       # strip the timestamp
        target = journal.with_name(base)
    target = Path(target)
    it = _iter_records(journal)
    _, orig_size = next(it)
    n = 0
    with open(target, "r+b") as f:
        for _, off, ln, data in it:
            f.seek(off)
            f.write(data if data is not None else b"\0" * ln)
            n += 1
        f.truncate(orig_size)
        f.flush()
        os.fsync(f.fileno())
    if log:
        log(f"rolled back {n} range(s) in {target.name}; size {orig_size:,} B")
    return {"target": str(target), "records": n, "size": orig_size}


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m taiko2x6.safeio",
        description="Inspect or apply an in-place undo journal (*.undo).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("info", help="show what a journal holds")
    a.add_argument("journal")
    b = sub.add_parser("rollback", help="restore the file to its pre-write state")
    b.add_argument("journal")
    b.add_argument("--target", help="file to restore (default: from journal name)")
    args = ap.parse_args(argv)
    if args.cmd == "info":
        info = journal_info(args.journal)
        print(f"original size {info['original_size']:,} B, "
              f"{info['records']} range(s), {info['bytes']:,} B saved")
    else:
        rollback(args.journal, args.target, log=print)
    return 0


if __name__ == "__main__":
    sys.exit(_main())

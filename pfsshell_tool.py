#!/usr/bin/env python3
"""Drive the native ``pfsshell.exe`` to browse / extract / inject files on a PS2
APA+PFS HDD image.

This is an alternative to the pure-Python :mod:`ps2hdd` browser. pfsshell is the
reference C implementation (uyjulian/pfsshell) and is more tolerant of unusual
APA/PFS layouts; it is also the tool the game's own HDD was authored with.

pfsshell is an INTERACTIVE REPL. We drive it by piping a newline-separated
command script to stdin and parsing stdout. Its command set (no partition
mounted vs mounted):

    device <path>       select the disk image / block device
    ls                  no mount -> list partitions; mounted -> list dir
    mount <partition>   mount a PFS partition (prompt becomes "<part>:/#")
    cd <dir>            change directory inside the mounted partition
    get <file>          copy a PFS file to the PROCESS cwd
    put <file>          copy a host file (from the PROCESS cwd) into PFS
    umount / exit

``get``/``put`` are relative to the *process* working directory, so we launch
pfsshell with ``cwd`` set to the host folder we want to read/write.

NOTE: the image must not be locked by another process. PCSX2 (or any emulator)
holding the .img open makes both pfsshell and ps2hdd fail with
"Permission denied" — close it first.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


class PfsShellError(RuntimeError):
    pass


def _short_path_win(path: str) -> str:
    """Windows 8.3 short path (space-free) for an existing file, or '' if
    unavailable (non-Windows, 8.3 disabled, or file missing)."""
    try:
        import ctypes
        from ctypes import wintypes
        _get = ctypes.windll.kernel32.GetShortPathNameW
        _get.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        _get.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(512)
        n = _get(path, buf, 512)
        return buf.value if 0 < n < 512 else ""
    except Exception:
        return ""


def find_pfsshell(hint: str | Path | None = None) -> str | None:
    """Locate pfsshell.exe. Checks an explicit hint, an env var, the toolkit dir,
    the image's own folder (passed as hint), and PATH."""
    cands: list[Path] = []
    if hint:
        h = Path(hint)
        cands += [h, h / "pfsshell.exe", h.parent / "pfsshell.exe"]
    env = os.environ.get("PFSSHELL_EXE")
    if env:
        cands.append(Path(env))
    import apppaths
    here = apppaths.resource_dir()
    cands += [here / "pfsshell.exe", here / "pfsshell" / "pfsshell.exe",
              Path(__file__).resolve().parent / "pfsshell.exe"]
    for c in cands:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            continue
    onpath = shutil.which("pfsshell") or shutil.which("pfsshell.exe")
    return onpath


class PfsShell:
    """Thin scripted wrapper over pfsshell.exe for one disk image."""

    def __init__(self, image: str | Path, exe: str | None = None,
                 timeout: int = 600):
        img = Path(image).resolve()
        # auto-recovery: if a previous session crashed mid temp-rename, the
        # image is still named _pfsshell_active.img — restore it first.
        if not img.exists():
            tmp = img.parent / "_pfsshell_active.img"
            marker = img.parent / "_pfsshell_active.orig.txt"
            try:
                if (tmp.exists() and marker.exists()
                        and marker.read_text(encoding="utf-8").strip() == img.name):
                    os.rename(tmp, img)
                    marker.unlink()
            except OSError:
                pass
        if not img.exists():
            raise PfsShellError(f"image not found: {img}")
        self.image = str(img)
        self.exe = exe or find_pfsshell(img.parent)
        if not self.exe:
            raise PfsShellError(
                "pfsshell.exe not found. Put it next to this toolkit, set the "
                "PFSSHELL_EXE environment variable, or install it on PATH.")
        # pfsshell's command parser splits args on whitespace and has no quoting,
        # so a path with spaces / brackets (e.g. "NM00057 ... [Ver.B02] (HDD).img")
        # breaks the `device` command. Run pfsshell with cwd = the image's folder
        # and pass only the basename to `device`; get/put also resolve against
        # this cwd. If the basename still has spaces, fall back to the Windows
        # 8.3 short path (space-free) for the whole image path.
        self.imgdir = str(img.parent)
        self.imgbase = img.name
        self._link: str | None = None
        self._renamed: tuple | None = None   # (tmp_path, orig_path, marker_path)
        # Directory spaces are harmless (we pass the dir via subprocess cwd, not
        # through pfsshell's parser) — only the BASENAME must be space-free.
        if " " in self.imgbase:
            short = _short_path_win(self.image)
            if short and " " not in Path(short).name:
                self.imgdir = str(Path(short).parent)
                self.imgbase = Path(short).name
        if " " in self.imgbase:
            # 8.3 short names disabled. Try a space-free HARDLINK next to the
            # image (instant, same inode — NTFS only).
            link = img.parent / "_pfsshell_link.img"
            try:
                if link.exists():
                    link.unlink()
                os.link(self.image, link)
                self._link = str(link)
                self.imgbase = link.name
            except OSError:
                # Last resort (exFAT: no 8.3, no hardlinks): temporarily RENAME
                # the image to a space-free name — instant on the same volume,
                # no copy — and restore the original name in close(). A marker
                # file records the original name so a crashed session can be
                # auto-recovered on the next open.
                tmp = img.parent / "_pfsshell_active.img"
                marker = img.parent / "_pfsshell_active.orig.txt"
                try:
                    if tmp.exists() or marker.exists():
                        raise OSError(
                            "leftover _pfsshell_active.* from a previous run "
                            "in the image folder — restore or delete it first")
                    os.rename(self.image, tmp)
                    marker.write_text(img.name, encoding="utf-8")
                    self._renamed = (str(tmp), self.image, str(marker))
                    self.imgbase = tmp.name
                except OSError as exc:
                    raise PfsShellError(
                        f"the image filename has spaces ({img.name!r}) and no "
                        f"space-free alias could be made ({exc}). Close any "
                        f"program using the image and retry, or rename the .img "
                        f"manually.") from exc
        self.timeout = timeout

    def close(self):
        if self._link:
            try:
                os.unlink(self._link)
            except OSError:
                pass
            self._link = None
        if self._renamed:
            tmp, orig, marker = self._renamed
            try:
                os.rename(tmp, orig)
            except OSError:
                return   # locked: keep the marker so the next open can recover
            try:
                os.unlink(marker)
            except OSError:
                pass
            self._renamed = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # -- low level ------------------------------------------------------------
    def _run(self, script_lines: list[str], cwd: str | None = None) -> str:
        """Feed ``script_lines`` (each a pfsshell command) to a fresh pfsshell
        process and return its combined stdout. ``exit`` is always appended.
        Defaults cwd to the image folder so `device <basename>` resolves."""
        if cwd is None:
            cwd = self.imgdir
        script = "".join(l.rstrip("\n") + "\n" for l in script_lines) + "exit\n"
        try:
            proc = subprocess.run(
                [self.exe],
                input=script,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PfsShellError(f"pfsshell timed out after {self.timeout}s") from exc
        out = (proc.stdout or "") + (proc.stderr or "")
        low = out.lower()
        if "permission denied" in low:
            raise PfsShellError(
                "Permission denied opening the image — close any program using "
                "it (e.g. PCSX2) and try again.")
        if "no such file" in low and "device" in low:
            raise PfsShellError(f"pfsshell could not open the image: {self.image}")
        return out

    # -- parsing helpers ------------------------------------------------------
    # pfsshell interleaves three things on stdout/stderr and we concatenate the
    # streams, so line ORDER is not reliable (the banner can land after the
    # data).  Never scrape "every line that isn't obviously noise" -- the noise
    # is unbounded prose.  Both listings have a strict column format, so match
    # that instead; banner/driver/help text cannot satisfy either pattern.
    #
    #   partitions (ls with nothing mounted):  "0x0100  2048MB t14jp1400.0001"
    #   files      (ls with a partition on):   "-rwxrwxrwx  2792052 2026-07-16 15:18 taiko"
    _PART_ROW = re.compile(
        r"^\s*0x([0-9a-fA-F]{4})\s+(\d+)MB\s+(\S+)\s*$")
    _FILE_ROW = re.compile(
        r"^\s*([-dl])([rwxst-]{9})\s+(\d+)\s+"
        r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+(.+?)\s*$")
    # APA partition type 0x0100 == PFS filesystem; 0x0001 == __mbr, etc.
    _PFS_TYPE = 0x0100

    @staticmethod
    def _strip_prompts(text: str) -> list[str]:
        """Split output into lines, dropping blanks and leading prompt echoes.

        A single line can carry several prompts ("> # t14jp1400.0001:/# ..."),
        so strip repeatedly rather than once.
        """
        lines = []
        for raw in text.splitlines():
            s = raw.rstrip()
            while True:
                s2 = re.sub(r"^(?:>|#|[A-Za-z0-9_+\-.]+:[^#]*#)\s*", "", s)
                if s2 == s:
                    break
                s = s2
            if s:
                lines.append(s)
        return lines

    # pfsshell marks every error with a "(!) " prefix, e.g.
    #   (!) hdd0:0x0100: No such file or directory.
    #   (!) Exit code is -2.
    #   (!) No partition mounted; use `mount' command.
    _ERR_ROW = re.compile(r"^\(!\)\s*(.+?)\s*$")

    def _check_errors(self, out: str, what: str) -> None:
        """Raise if pfsshell reported a failure.

        Errors used to be ignored entirely, which let a failed `mount` fall
        through to the following `ls` -- pfsshell then listed the DEVICE
        (i.e. the partition table) and the caller happily showed partitions as
        if they were files.
        """
        msgs = [m.group(1) for m in
                (self._ERR_ROW.match(s) for s in self._strip_prompts(out)) if m]
        # "Exit code is -N" only restates the line before it; drop it if we
        # already have the real message.
        detail = [m for m in msgs if not m.lower().startswith("exit code is")]
        if msgs:
            raise PfsShellError(f"{what}: {(detail or msgs)[0]}")

    # -- operations -----------------------------------------------------------
    def list_partitions(self, pfs_only: bool = True) -> list[str]:
        """Return partition names on the image (via ``ls`` before mount).

        Only rows matching the "0xTTTT  <n>MB <name>" column format count, so
        the banner ("pfsshell for POSIX systems" -> "systems") can't sneak in.
        With ``pfs_only`` (default) non-PFS partitions such as __mbr are hidden;
        they have no filesystem to browse.
        """
        out = self._run([f"device {self.imgbase}", "ls"])
        self._check_errors(out, "listing partitions")
        parts = []
        for s in self._strip_prompts(out):
            m = self._PART_ROW.match(s)
            if not m:
                continue
            ptype, name = int(m.group(1), 16), m.group(3)
            if pfs_only and ptype != self._PFS_TYPE:
                continue
            parts.append(name)
        seen, uniq = set(), []
        for p in parts:
            if p not in seen:
                seen.add(p); uniq.append(p)
        return uniq

    def listdir(self, partition: str, path: str = "/") -> list[dict]:
        """List entries in ``path`` of a mounted partition."""
        cmds = [f"device {self.imgbase}", f"mount {partition}"]
        if path and path != "/":
            cmds.append(f"cd {path.strip('/')}")
        cmds += ["ls", "umount"]
        out = self._run(cmds)
        self._check_errors(out, f"listing {partition}:{path}")
        entries = []
        for s in self._strip_prompts(out):
            m = self._FILE_ROW.match(s)
            if not m:
                continue
            kind, _perm, size, date, tm, name = m.groups()
            if name in {".", ".."}:
                continue
            entries.append({"name": name, "size": int(size),
                            "is_dir": kind == "d", "mtime": f"{date} {tm}",
                            "raw": s})
        return entries

    def extract(self, partition: str, pfs_path: str, dest_file: str | Path) -> int:
        """Extract ``/pfs_path`` (e.g. '/DATA.000') from the image to ``dest_file``."""
        dest = Path(dest_file)
        dest.parent.mkdir(parents=True, exist_ok=True)
        pfs_path = pfs_path.lstrip("/")
        d, base = os.path.split(pfs_path)
        cmds = [f"device {self.imgbase}", f"mount {partition}"]
        if d:
            cmds.append(f"cd {d}")
        cmds += [f"get {base}", "umount"]
        # cwd is the image folder (for `device <basename>`), so `get` drops the
        # file there; move it to the requested destination.
        out = self._run(cmds)
        self._check_errors(out, f"extracting {partition}:/{pfs_path}")
        got = Path(self.imgdir) / base
        if not got.exists():
            raise PfsShellError(
                f"pfsshell get did not produce {base}. Check the partition/path "
                f"and that the image is not locked.")
        if got.resolve() != dest.resolve():
            shutil.move(str(got), str(dest))
        return dest.stat().st_size

    def _size_of(self, partition: str, d: str, base: str) -> int | None:
        """Size of ``base`` inside ``partition``:/``d``, or None if absent."""
        for e in self.listdir(partition, d or "/"):
            if e["name"] == base:
                return e["size"]
        return None

    def inject(self, partition: str, pfs_path: str, src_file: str | Path) -> int:
        """Write ``src_file`` into the image at ``/pfs_path``.

        pfsshell's ``put`` opens an existing file WITHOUT truncating it, so
        writing a SMALLER file leaves the tail of the old one behind: an 8-byte
        put over a 32-byte file yields 8 new bytes + 24 stale ones, and `ls`
        still reports 32 B.  Silent corruption, invisible to a size check.
        Writing an equal/larger file is fine (every old byte gets overwritten),
        so only `rm` first when shrinking -- that keeps the original intact if
        the `put` fails, which matters when the file is most of the partition
        (DATA.000 is 1.3G of a 2G partition, so there is no room to stage a
        temp copy and rename).
        """
        src = Path(src_file)
        if not src.is_file():
            raise PfsShellError(f"source file not found: {src}")
        new_size = src.stat().st_size
        pfs_path = pfs_path.lstrip("/")
        d, base = os.path.split(pfs_path)
        old_size = self._size_of(partition, d, base)
        # cwd is the image folder; pfsshell `put` reads ``base`` from there, so
        # stage a copy named exactly ``base`` next to the image.
        staged = Path(self.imgdir) / base
        cleanup = staged.resolve() != src.resolve()
        if cleanup:
            shutil.copyfile(src, staged)
        try:
            cmds = [f"device {self.imgbase}", f"mount {partition}"]
            if d:
                cmds.append(f"cd {d}")
            if old_size is not None and new_size < old_size:
                cmds.append(f"rm {base}")
            cmds += [f"put {base}", "umount"]
            out = self._run(cmds)
            # without this a failed `put` still returned src size, i.e. reported
            # a write that never happened.
            self._check_errors(out, f"injecting {partition}:/{pfs_path}")
        finally:
            if cleanup:
                try:
                    staged.unlink()
                except OSError:
                    pass
        wrote = self._size_of(partition, d, base)
        if wrote != new_size:
            raise PfsShellError(
                f"injecting {partition}:/{pfs_path}: wrote {new_size} B but the "
                f"file is {wrote} B on the image — it may be corrupt.")
        return new_size


# --------------------------------------------------------------------------- #
#  GUI dialog
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtCore import Qt, QThread, Signal
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QComboBox, QListWidget,
        QLabel, QMessageBox, QProgressDialog, QListWidgetItem,
    )
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False
    QDialog = object  # type: ignore


if _HAVE_QT:
    import appconfig

    class _Worker(QThread):
        done = Signal(object)

        def __init__(self, fn):
            super().__init__()
            self._fn = fn

        def run(self):
            try:
                self.done.emit(self._fn())
            except Exception as exc:
                import traceback
                self.done.emit(("ERROR", exc, traceback.format_exc()))

    class PfsShellDialog(QDialog):
        """Browse a PS2 HDD image with the native pfsshell.exe backend."""

        def __init__(self, parent=None, default_img=""):
            super().__init__(parent)
            self.setWindowTitle("PS2 HDD via pfsshell (APA + PFS)")
            self.resize(640, 480)
            self.sh: PfsShell | None = None
            self.path = ""
            self._workers: list[_Worker] = []
            self._build_ui()
            # Don't gate on exists(): if a previous session died mid temp-rename
            # the image is sitting there as _pfsshell_active.img, so exists() is
            # False and this would skip _open() -- which is the only thing that
            # constructs PfsShell, i.e. the only thing that can auto-recover it.
            # PfsShell recovers first and raises a clear error if it's really
            # gone.
            if default_img:
                self._open(default_img)

        def _build_ui(self):
            lay = QVBoxLayout(self)
            top = QHBoxLayout()
            self.lbl_img = QLabel("(no image open)")
            b_open = QPushButton("Open .img…"); b_open.clicked.connect(lambda: self._open())
            top.addWidget(b_open); top.addWidget(self.lbl_img, 1)
            lay.addLayout(top)

            prow = QHBoxLayout()
            prow.addWidget(QLabel("partition:"))
            self.cb_part = QComboBox()
            self.cb_part.currentIndexChanged.connect(self._refresh_files)
            prow.addWidget(self.cb_part, 1)
            lay.addLayout(prow)

            self.lst = QListWidget()
            lay.addWidget(self.lst, 1)

            btns = QHBoxLayout()
            self.b_extract = QPushButton("Extract selected…"); self.b_extract.clicked.connect(self._extract)
            self.b_replace = QPushButton("Replace selected…"); self.b_replace.clicked.connect(self._replace)
            for b in (self.b_extract, self.b_replace):
                b.setEnabled(False)
            b_close = QPushButton("Close"); b_close.clicked.connect(self.accept)
            btns.addWidget(self.b_extract); btns.addWidget(self.b_replace)
            btns.addStretch(1); btns.addWidget(b_close)
            lay.addLayout(btns)

            self.status = QLabel("pfsshell backend. Close any emulator holding the image first.")
            self.status.setStyleSheet("color:#999;")
            lay.addWidget(self.status)

        def _open(self, path=""):
            if not path:
                path = appconfig.pick_open(self, "hddimg", "Open PS2 HDD image",
                                           "HDD image (*.img *.raw *.bin);;All files (*)")
            if not path:
                return
            try:
                if self.sh:
                    self.sh.close()
                    self.sh = None
                self.sh = PfsShell(path)
            except PfsShellError as exc:
                QMessageBox.critical(self, "pfsshell", str(exc))
                return
            self.path = path
            self.lbl_img.setText(path)
            if self.sh._renamed:
                self.status.setText(
                    "image temporarily renamed to _pfsshell_active.img (name has "
                    "spaces; pfsshell can't parse them) — restored automatically "
                    "when this dialog closes. Don't launch the game meanwhile.")
            self._run(lambda: self.sh.list_partitions(),
                      "Reading partitions…", self._got_parts)

        def _got_parts(self, parts):
            # Keep signals blocked through setCurrentIndex too, otherwise
            # currentIndexChanged kicks off a _refresh_files of its own and the
            # explicit call below starts a second, racing worker.
            self.cb_part.blockSignals(True)
            self.cb_part.clear()
            self.cb_part.addItems(parts or [])
            if parts:
                default = next((i for i, p in enumerate(parts)
                                if not p.startswith("__")), 0)
                self.cb_part.setCurrentIndex(default)
            self.cb_part.blockSignals(False)
            if parts:
                self._refresh_files()
            else:
                self.status.setText("no PFS partitions found")

        def _cur_part(self):
            return self.cb_part.currentText() or None

        def _refresh_files(self):
            part = self._cur_part()
            if not part:
                return
            self._run(lambda: self.sh.listdir(part, "/"),
                      f"Listing {part}…", self._got_files)

        def _got_files(self, entries):
            self.lst.clear()
            for e in entries:
                tag = "[dir] " if e.get("is_dir") else ""
                sz = f"   {e['size']:,} B" if e.get("size") is not None else ""
                it = QListWidgetItem(f"{tag}{e['name']}{sz}")
                it.setData(Qt.UserRole, e)
                self.lst.addItem(it)
            self.b_extract.setEnabled(True); self.b_replace.setEnabled(True)
            self.status.setText(f"{len(entries)} entries")

        def _selected(self):
            it = self.lst.currentItem()
            return it.data(Qt.UserRole) if it else None

        def _extract(self):
            e = self._selected(); part = self._cur_part()
            if not e or e.get("is_dir"):
                QMessageBox.information(self, "Extract", "Select a file."); return
            dest = appconfig.pick_save(self, "hdd_extract", "Extract to", e["name"])
            if not dest:
                return
            self._run(lambda: self.sh.extract(part, "/" + e["name"], dest),
                      f"Extracting {e['name']}…",
                      lambda r: self.status.setText(f"extracted → {dest} ({r:,} B)"))

        def _replace(self):
            e = self._selected(); part = self._cur_part()
            if not e or e.get("is_dir"):
                QMessageBox.information(self, "Replace", "Select a file."); return
            src = appconfig.pick_open(self, "hdd_replace", f"Replace {e['name']} with…")
            if not src:
                return
            if QMessageBox.warning(
                    self, "Replace in HDD image",
                    f"Write {Path(src).name} into the image as {e['name']}?\n\n"
                    f"The image is modified in place by pfsshell. Make a backup "
                    f"first. Continue?",
                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            def ok(r):
                self.status.setText(f"replaced {e['name']} ✓ ({r:,} B)")
                self._refresh_files()   # else the list keeps showing the old size

            self._run(lambda: self.sh.inject(part, "/" + e["name"], src),
                      f"Writing {e['name']}…", ok)

        # -- worker plumbing --
        def _run(self, fn, msg, on_ok):
            prog = QProgressDialog(msg, None, 0, 0, self)
            prog.setWindowModality(Qt.WindowModal); prog.setCancelButton(None)
            prog.setMinimumDuration(0); prog.show()
            w = _Worker(fn)
            # Hold a strong ref until the thread has ACTUALLY finished, not just
            # emitted its result: `done` is delivered from run(), so the QThread
            # is still winding down. A single `self._worker` slot used to be
            # overwritten by the next _run() started from inside the previous
            # worker's callback -- that dropped the last ref to a live QThread
            # and aborted with "QThread: Destroyed while thread is still
            # running".
            self._workers.append(w)

            def finished():
                w.deleteLater()
                try:
                    self._workers.remove(w)
                except ValueError:
                    pass

            def done(r):
                prog.close()
                if isinstance(r, tuple) and r and r[0] == "ERROR":
                    QMessageBox.critical(self, "pfsshell failed", str(r[1]))
                    return
                on_ok(r)

            w.done.connect(done)
            w.finished.connect(finished)
            w.start()

        def done(self, r):
            # Runs for OK/Cancel/the window X alike — wait out any in-flight
            # worker, then close the shell so a temp-renamed image is restored.
            for w in list(self._workers):
                if w.isRunning():
                    w.wait()
            if self.sh:
                self.sh.close()
                self.sh = None
            super().done(r)


if __name__ == "__main__":
    import sys
    exe = find_pfsshell()
    print("pfsshell.exe:", exe or "NOT FOUND")
    if len(sys.argv) > 1:
        sh = PfsShell(sys.argv[1])
        print("partitions:", sh.list_partitions())

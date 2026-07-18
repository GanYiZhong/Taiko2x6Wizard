#!/usr/bin/env python3
"""PS2 memory-card image reader/writer (the format mymc handles), with support
for the MagicGate Arcade / COH cards used by SYSTEM246/256 Taiko boards.

Why our own instead of mymc: mymc ships as Python-2.7 .pyc inside library.zip
(py2exe) and can't be imported from Python 3. The on-card filesystem is public
and identical for arcade cards -- MagicGate only guards the physical card's
authentication, not the layout -- so a clean Py3 implementation is simpler and
maintainable. (mymc itself is public domain, by Ross Ridge.)

Format
------
Page   = 512 data bytes (+ ``spare`` bytes of ECC on "raw"/arcade dumps).
         An 8 MB arcade card is 16384 pages x 528 = 8,650,752 bytes.
         A no-ECC .ps2 dump is 16384 x 512 = 8,388,608 bytes.
Cluster= pages_per_cluster (2) pages = 1024 data bytes.
Superblock (page 0):
    0x00 char magic[28]    "Sony PS2 Memory Card Format "
    0x1C char version[12]
    0x28 u16 page_len, u16 pages_per_cluster, u16 pages_per_block, u16 -
    0x30 u32 clusters_per_card
    0x34 u32 alloc_offset      first data cluster
    0x38 u32 alloc_end
    0x3C u32 rootdir_cluster   (relative to alloc_offset)
    0x50 u32 ifc_list[32]      indirect-FAT cluster numbers
    0xD0 u32 bad_block_list[32]
    0x150 u8 card_type, u8 card_flags   (flags bit0 = ECC/spare present)
FAT: two levels. ifc_list[i] -> an indirect cluster of 256 u32 -> a FAT cluster
     of 256 u32. Entry bit31 = allocated; low 31 bits = next cluster (0x7FFFFFFF
     = end of chain). Cluster numbers are relative to alloc_offset.
Dirent: 512 bytes, 2 per cluster:
    0x00 u16 mode, 0x04 u32 length, 0x08 created[8], 0x10 u32 cluster,
    0x14 u32 dir_entry, 0x18 modified[8], 0x20 u32 attr, 0x40 char name[32]
"""
from __future__ import annotations

import struct
from pathlib import Path

PAGE_DATA = 512

# dirent mode bits
DF_READ = 0x0001
DF_WRITE = 0x0002
DF_EXECUTE = 0x0004
DF_PROTECTED = 0x0008
DF_FILE = 0x0010
DF_DIRECTORY = 0x0020
DF_EXISTS = 0x8000


class Ps2mcError(Exception):
    pass


class Ps2mcCard:
    """Read (and optionally write) a PS2 memory-card image."""

    def __init__(self, path, writable: bool = False):
        self.path = str(path)
        self.writable = writable
        self.f = open(self.path, "r+b" if writable else "rb")
        try:
            self._read_superblock()
        except Exception:
            self.f.close()
            raise

    # -- lifecycle ---------------------------------------------------------
    def close(self):
        try:
            self.f.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # -- superblock --------------------------------------------------------
    def _read_superblock(self):
        self.f.seek(0)
        sb = self.f.read(0x160)
        if sb[:28] != b"Sony PS2 Memory Card Format ":
            raise Ps2mcError(f"not a PS2 memory card image: {self.path}")
        self.version = sb[0x1C:0x28].split(b"\x00")[0].decode("latin1")
        (self.page_len, self.pages_per_cluster,
         self.pages_per_block, _u) = struct.unpack_from("<4H", sb, 0x28)
        (self.clusters_per_card, self.alloc_offset, self.alloc_end,
         self.rootdir_cluster) = struct.unpack_from("<4I", sb, 0x30)
        self.ifc_list = list(struct.unpack_from("<32I", sb, 0x50))
        self.card_type = sb[0x150]
        self.card_flags = sb[0x151]

        self.cluster_size = self.page_len * self.pages_per_cluster
        self.entries_per_cluster = self.cluster_size // 4

        # Derive the spare (ECC) size from the file size rather than trusting
        # card_flags: raw arcade dumps carry 16 spare bytes per page.
        size = Path(self.path).stat().st_size
        total_pages = self.clusters_per_card * self.pages_per_cluster
        raw = size // total_pages if total_pages else 0
        if raw == self.page_len:
            self.spare = 0
        elif raw > self.page_len:
            self.spare = raw - self.page_len
        else:
            raise Ps2mcError(
                f"image too small: {size} bytes for {total_pages} pages")
        self.raw_page = self.page_len + self.spare
        self._fat_cache: dict[int, list] = {}

    @property
    def has_ecc(self) -> bool:
        return self.spare > 0

    def info(self) -> dict:
        return {
            "version": self.version, "page_len": self.page_len,
            "spare": self.spare, "raw_page": self.raw_page,
            "pages_per_cluster": self.pages_per_cluster,
            "clusters_per_card": self.clusters_per_card,
            "alloc_offset": self.alloc_offset, "alloc_end": self.alloc_end,
            "rootdir_cluster": self.rootdir_cluster,
            "card_type": self.card_type, "card_flags": self.card_flags,
            "size_mb": self.clusters_per_card * self.cluster_size / 1e6,
        }

    # -- raw page / cluster IO --------------------------------------------
    def read_page(self, n: int) -> bytes:
        self.f.seek(n * self.raw_page)
        d = self.f.read(self.page_len)
        if len(d) != self.page_len:
            raise Ps2mcError(f"short read at page {n}")
        return d

    def write_page(self, n: int, data: bytes):
        if not self.writable:
            raise Ps2mcError("card opened read-only")
        if len(data) != self.page_len:
            raise Ps2mcError("page write must be exactly one page")
        self.f.seek(n * self.raw_page)
        self.f.write(data)
        if self.spare:
            self.f.write(_ecc_spare(data, self.spare))

    def read_cluster(self, n: int) -> bytes:
        base = n * self.pages_per_cluster
        return b"".join(self.read_page(base + i)
                        for i in range(self.pages_per_cluster))

    def write_cluster(self, n: int, data: bytes):
        if len(data) != self.cluster_size:
            raise Ps2mcError("cluster write must be exactly one cluster")
        base = n * self.pages_per_cluster
        for i in range(self.pages_per_cluster):
            self.write_page(base + i, data[i * self.page_len:(i + 1) * self.page_len])

    # -- FAT ---------------------------------------------------------------
    def _fat_cluster(self, n: int) -> list:
        c = self._fat_cache.get(n)
        if c is None:
            c = list(struct.unpack("<%dI" % self.entries_per_cluster,
                                   self.read_cluster(n)))
            self._fat_cache[n] = c
        return c

    def lookup_fat(self, n: int) -> int:
        epc = self.entries_per_cluster
        indirect_offset = n // epc
        fat_offset = n % epc
        ifc = self.ifc_list[indirect_offset // epc]
        indirect = self._fat_cluster(ifc)
        fat_cluster_num = indirect[indirect_offset % epc]
        return self._fat_cluster(fat_cluster_num)[fat_offset]

    def set_fat(self, n: int, value: int):
        """Write a FAT entry back to the card."""
        epc = self.entries_per_cluster
        indirect_offset = n // epc
        fat_offset = n % epc
        ifc = self.ifc_list[indirect_offset // epc]
        indirect = self._fat_cluster(ifc)
        fat_cluster_num = indirect[indirect_offset % epc]
        fat = self._fat_cluster(fat_cluster_num)
        fat[fat_offset] = value & 0xFFFFFFFF
        self.write_cluster(fat_cluster_num,
                           struct.pack("<%dI" % epc, *fat))

    def chain(self, first: int, limit: int | None = None) -> list:
        """Cluster chain starting at data-cluster ``first``."""
        out = []
        n = first
        seen = set()
        while n != 0xFFFFFFFF and (n & 0x7FFFFFFF) != 0x7FFFFFFF:
            if n in seen:
                raise Ps2mcError("cyclic FAT chain")
            seen.add(n)
            out.append(n)
            if limit and len(out) >= limit:
                break
            e = self.lookup_fat(n)
            if not (e & 0x80000000):
                break                      # not allocated -> chain ends
            n = e & 0x7FFFFFFF
        return out

    def read_data_cluster(self, n: int) -> bytes:
        return self.read_cluster(self.alloc_offset + n)

    def write_data_cluster(self, n: int, data: bytes):
        self.write_cluster(self.alloc_offset + n, data)

    # -- directory ---------------------------------------------------------
    def _parse_dirent(self, buf: bytes, off: int) -> dict:
        mode, = struct.unpack_from("<H", buf, off + 0x00)
        length, = struct.unpack_from("<I", buf, off + 0x04)
        cluster, = struct.unpack_from("<I", buf, off + 0x10)
        name = buf[off + 0x40:off + 0x60].split(b"\x00")[0].decode("latin1")
        return {"mode": mode, "length": length, "cluster": cluster,
                "name": name,
                "is_dir": bool(mode & DF_DIRECTORY),
                "exists": bool(mode & DF_EXISTS)}

    def _read_dir(self, first_cluster: int, count: int) -> list:
        ents = []
        per = self.cluster_size // 512
        for i, dc in enumerate(self.chain(first_cluster)):
            buf = self.read_data_cluster(dc)
            for j in range(per):
                if len(ents) >= count:
                    return ents
                e = self._parse_dirent(buf, j * 512)
                e["_dir_cluster"] = dc
                e["_dir_index"] = i * per + j
                ents.append(e)
        return ents

    def root(self) -> list:
        """Entries of the root directory (including '.' and '..')."""
        head = self._read_dir(self.rootdir_cluster, 1)
        if not head:
            raise Ps2mcError("cannot read root directory")
        return self._read_dir(self.rootdir_cluster, head[0]["length"])

    def listdir(self, path: str = "/") -> list:
        ents = [e for e in self.root()
                if e["exists"] and e["name"] not in (".", "..")]
        parts = [p for p in path.strip("/").split("/") if p]
        for p in parts:
            match = next((e for e in ents if e["name"] == p and e["is_dir"]), None)
            if match is None:
                raise Ps2mcError(f"no such directory: {path}")
            sub = self._read_dir(match["cluster"], match["length"])
            ents = [e for e in sub
                    if e["exists"] and e["name"] not in (".", "..")]
        return ents

    def _find(self, path: str) -> dict:
        parts = [p for p in path.strip("/").split("/") if p]
        if not parts:
            raise Ps2mcError("empty path")
        parent = "/".join(parts[:-1])
        for e in self.listdir("/" + parent):
            if e["name"] == parts[-1]:
                return e
        raise Ps2mcError(f"no such file: {path}")

    # -- file IO -----------------------------------------------------------
    def read_file(self, path: str) -> bytes:
        e = self._find(path)
        if e["is_dir"]:
            raise Ps2mcError(f"is a directory: {path}")
        out = bytearray()
        for dc in self.chain(e["cluster"]):
            out += self.read_data_cluster(dc)
            if len(out) >= e["length"]:
                break
        return bytes(out[:e["length"]])

    # -- allocation --------------------------------------------------------
    # FAT entry encoding, read off a real arcade card (NM00057) -- all 5678 free
    # entries carry the identical value, so this is not a guess:
    #   0x7FFFFFFF  free            (bit31 clear = unallocated)
    #   0xFFFFFFFF  allocated, last (bit31 set, "next" field saturated)
    #   0x80000000 | next           allocated, points at the next cluster
    FAT_FREE = 0x7FFFFFFF
    FAT_EOC = 0xFFFFFFFF

    def free_clusters(self) -> list:
        """Data clusters currently unallocated."""
        return [n for n in range(self.alloc_end)
                if not (self.lookup_fat(n) & 0x80000000)]

    def _alloc_chain(self, count: int) -> list:
        """Allocate and link ``count`` free clusters. Raises if the card is full."""
        picked = []
        for n in range(self.alloc_end):
            if not (self.lookup_fat(n) & 0x80000000):
                picked.append(n)
                if len(picked) == count:
                    break
        if len(picked) < count:
            raise Ps2mcError(
                f"card full: need {count} clusters, only {len(picked)} free "
                f"({len(picked) * self.cluster_size} B)")
        for i, n in enumerate(picked):
            self.set_fat(n, self.FAT_EOC if i == count - 1
                         else (0x80000000 | picked[i + 1]))
        return picked

    def _free_chain(self, clusters) -> None:
        for n in clusters:
            self.set_fat(n, self.FAT_FREE)

    def write_file(self, path: str, data: bytes):
        """Overwrite an existing file, growing or shrinking it as needed.

        Reallocates rather than writing into the file's existing clusters, so
        the new data is NOT capped by whatever the old file happened to occupy
        (T14GAME, for instance, has only 92 bytes of slack in its chain while
        the card has ~5.8 MB free). Shrinking releases the surplus clusters
        instead of leaking them.

        Ordering is deliberate -- allocate, write, repoint the dirent, and only
        then release the old chain -- so the file always references complete
        data. An interrupted write leaks clusters; it does not lose the file.
        """
        if not self.writable:
            raise Ps2mcError("card opened read-only")
        e = self._find(path)
        if e["is_dir"]:
            raise Ps2mcError(f"is a directory: {path}")
        cs = self.cluster_size
        old = self.chain(e["cluster"])
        need = max(1, (len(data) + cs - 1) // cs)
        # Same cluster count -> write in place; needs no free space at all.
        chain = old if need == len(old) else self._alloc_chain(need)

        pad = data + b"\x00" * (-len(data) % cs)
        for i, dc in enumerate(chain):
            self.write_data_cluster(dc, pad[i * cs:(i + 1) * cs])

        buf = bytearray(self.read_data_cluster(e["_dir_cluster"]))
        off = (e["_dir_index"] % (cs // 512)) * 512
        struct.pack_into("<I", buf, off + 0x04, len(data))
        struct.pack_into("<I", buf, off + 0x10, chain[0])
        self.write_data_cluster(e["_dir_cluster"], bytes(buf))

        if chain is not old:
            self._free_chain(old)
        self.f.flush()
        return len(data)


# --------------------------------------------------------------------------- #
#  ECC (spare bytes) — the Hamming code Sony uses on raw card dumps
# --------------------------------------------------------------------------- #
# Column-parity masks. Index 3 is a 0x00 placeholder, so that bit is always 0 —
# which is why the complemented column byte is masked with 0x77 and not 0x7F.
_CPMASKS = [0x55, 0x33, 0x0F, 0x00, 0xAA, 0xCC, 0xF0]


def _parity(x: int) -> int:
    p = 0
    while x:
        p ^= x & 1
        x >>= 1
    return p


_PARITY = [_parity(v) for v in range(256)]
_COLUMN = [sum(_parity(v & m) << i for i, m in enumerate(_CPMASKS))
           for v in range(256)]


def ecc_calculate(chunk: bytes) -> bytes:
    """3-byte ECC over a 128-byte chunk (Sony PS2 memory-card scheme).

    Verified against a real MagicGate arcade card (NM00057): 718 written pages
    sampled across the whole image reproduce their stored spare exactly, 0
    mismatches. Anchor: an all-zero chunk yields 77 7f 7f.
    """
    a = b = c = 0
    for i, v in enumerate(chunk):
        a ^= _COLUMN[v]
        if _PARITY[v]:
            b ^= ~i & 0x7F
            c ^= i & 0x7F
    return bytes([(~a) & 0x77, (~b) & 0x7F, (~c) & 0x7F])


def _ecc_spare(page: bytes, spare_len: int) -> bytes:
    """Spare area for a 512-byte page: 4 x 3-byte ECC (one per 128-byte chunk),
    zero-padded out to ``spare_len`` (16 on these cards)."""
    out = bytearray()
    for i in range(0, len(page), 128):
        out += ecc_calculate(page[i:i + 128])
    if len(out) > spare_len:
        raise Ps2mcError("spare too small for ECC")
    out += b"\x00" * (spare_len - len(out))
    return bytes(out)


def verify_ecc(path, step: int = 7) -> dict:
    """Sample the card and check stored spares against ecc_calculate().
    Erased pages (spare all 0xFF) are reported separately, not as errors."""
    with Ps2mcCard(path) as c:
        if not c.has_ecc:
            return {"ecc": False}
        ok = erased = bad = 0
        for p in range(0, c.clusters_per_card * c.pages_per_cluster, step):
            c.f.seek(p * c.raw_page)
            data = c.f.read(c.page_len)
            spare = c.f.read(c.spare)
            if len(spare) < c.spare:
                break
            if spare == b"\xff" * c.spare:
                erased += 1
                continue
            if _ecc_spare(data, c.spare) == spare:
                ok += 1
            else:
                bad += 1
        return {"ecc": True, "ok": ok, "erased": erased, "bad": bad}


# --------------------------------------------------------------------------- #
#  GUI dialog
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtCore import Qt, QThread, Signal
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QListWidget, QLabel,
        QMessageBox, QProgressDialog, QListWidgetItem,
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

    class Ps2mcDialog(QDialog):
        """Browse a PS2 / MagicGate-Arcade (COH) memory-card image: extract and
        replace files such as T14GAME, boot.bin, IRXARC.BIN."""

        def __init__(self, parent=None, default_img=""):
            super().__init__(parent)
            self.setWindowTitle("PS2 Memory Card (MagicGate Arcade / COH)")
            self.resize(640, 480)
            self.card: Ps2mcCard | None = None
            self.path = ""
            self._build_ui()
            if default_img and Path(default_img).exists():
                self._open(default_img)

        def _build_ui(self):
            lay = QVBoxLayout(self)
            top = QHBoxLayout()
            self.lbl = QLabel("(no card open)")
            b_open = QPushButton("Open card…"); b_open.clicked.connect(lambda: self._open())
            top.addWidget(b_open); top.addWidget(self.lbl, 1)
            lay.addLayout(top)

            self.info = QLabel(""); self.info.setStyleSheet("color:#aaa;font-family:Consolas;")
            lay.addWidget(self.info)

            self.lst = QListWidget()
            lay.addWidget(self.lst, 1)

            btns = QHBoxLayout()
            self.b_extract = QPushButton("Extract selected…"); self.b_extract.clicked.connect(self._extract)
            self.b_replace = QPushButton("Replace selected…"); self.b_replace.clicked.connect(self._replace)
            self.b_ecc = QPushButton("Verify ECC"); self.b_ecc.clicked.connect(self._verify)
            for b in (self.b_extract, self.b_replace, self.b_ecc):
                b.setEnabled(False)
            b_close = QPushButton("Close"); b_close.clicked.connect(self.accept)
            btns.addWidget(self.b_extract); btns.addWidget(self.b_replace)
            btns.addWidget(self.b_ecc)
            btns.addStretch(1); btns.addWidget(b_close)
            lay.addLayout(btns)

            self.status = QLabel(""); self.status.setStyleSheet("color:#999;")
            lay.addWidget(self.status)

        def _open(self, path=""):
            if not path:
                path = appconfig.pick_open(
                    self, "ps2mc", "Open PS2 memory card image",
                    "Memory card (*.ps2 *.bin *.mcd *.mc2 *.ic002);;All files (*)")
            if not path:
                return
            try:
                if self.card:
                    self.card.close()
                self.card = Ps2mcCard(path)
            except Exception as exc:
                QMessageBox.critical(self, "Open failed", str(exc))
                return
            self.path = path
            self.lbl.setText(path)
            i = self.card.info()
            self.info.setText(
                f"{i['size_mb']:.1f} MB  page={i['page_len']}+{i['spare']} spare  "
                f"{'ECC/raw' if self.card.has_ecc else 'no-ECC'}  "
                f"clusters={i['clusters_per_card']}  ver {i['version']}")
            self._refresh()

        def _refresh(self):
            self.lst.clear()
            try:
                ents = self.card.listdir("/")
            except Exception as exc:
                self.status.setText(f"list failed: {exc}"); return
            for e in ents:
                tag = "[dir] " if e["is_dir"] else ""
                it = QListWidgetItem(f"{tag}{e['name']:20s} {e['length']:>12,} B")
                it.setData(Qt.UserRole, e)
                self.lst.addItem(it)
            for b in (self.b_extract, self.b_replace, self.b_ecc):
                b.setEnabled(True)
            self.status.setText(f"{len(ents)} entries")

        def _sel(self):
            it = self.lst.currentItem()
            return it.data(Qt.UserRole) if it else None

        def _extract(self):
            e = self._sel()
            if not e or e["is_dir"]:
                QMessageBox.information(self, "Extract", "Select a file."); return
            dest = appconfig.pick_save(self, "ps2mc_extract", "Extract to", e["name"])
            if not dest:
                return
            try:
                data = self.card.read_file("/" + e["name"])
                Path(dest).write_bytes(data)
                self.status.setText(f"extracted {e['name']} → {dest} ({len(data):,} B)")
            except Exception as exc:
                QMessageBox.critical(self, "Extract failed", str(exc))

        def _replace(self):
            e = self._sel()
            if not e or e["is_dir"]:
                QMessageBox.information(self, "Replace", "Select a file."); return
            src = appconfig.pick_open(self, "ps2mc_replace", f"Replace {e['name']} with…")
            if not src:
                return
            data = Path(src).read_bytes()
            # The limit is the CARD's free space, not the slot the old file
            # happened to occupy: write_file reallocates, so a bigger file just
            # takes fresh clusters (T14GAME's chain has 92 B of slack while the
            # card has megabytes free).
            try:
                with Ps2mcCard(self.path) as probe:
                    ent = probe._find("/" + e["name"])
                    cs = probe.cluster_size
                    have = len(probe.chain(ent["cluster"])) + len(probe.free_clusters())
                    room = have * cs
            except Exception as exc:
                QMessageBox.critical(self, "Replace", str(exc)); return
            if len(data) > room:
                QMessageBox.warning(
                    self, "Too big",
                    f"{Path(src).name} is {len(data):,} B but the card only has "
                    f"{room:,} B available for {e['name']} (its current clusters "
                    f"plus all free space). Make the replacement smaller.")
                return
            if QMessageBox.warning(
                    self, "Replace in memory card",
                    f"Write {Path(src).name} ({len(data):,} B) over {e['name']} "
                    f"in:\n{self.path}\n\nThe card image is modified IN PLACE "
                    f"(ECC is regenerated). Back it up first. Continue?",
                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            try:
                self.card.close(); self.card = None
                with Ps2mcCard(self.path, writable=True) as w:
                    n = w.write_file("/" + e["name"], data)
                self.card = Ps2mcCard(self.path)
                self.status.setText(f"replaced {e['name']} ✓ ({n:,} B)")
                self._refresh()
            except Exception as exc:
                QMessageBox.critical(self, "Replace failed", str(exc))
                if self.card is None:
                    try:
                        self.card = Ps2mcCard(self.path)
                    except Exception:
                        self.card = None

        def _verify(self):
            prog = QProgressDialog("Verifying ECC…", None, 0, 0, self)
            prog.setWindowModality(Qt.WindowModal); prog.setCancelButton(None)
            prog.setMinimumDuration(0); prog.show()
            self._w = _Worker(lambda: verify_ecc(self.path))

            def done(r):
                prog.close()
                if isinstance(r, tuple) and r and r[0] == "ERROR":
                    QMessageBox.critical(self, "ECC", str(r[1])); return
                if not r.get("ecc"):
                    QMessageBox.information(self, "ECC", "This image has no ECC/spare area.")
                    return
                QMessageBox.information(
                    self, "ECC verify",
                    f"written pages OK : {r['ok']}\n"
                    f"erased pages     : {r['erased']}\n"
                    f"MISMATCHES       : {r['bad']}")

            self._w.done.connect(done)
            self._w.start()

        def done(self, r):
            w = getattr(self, "_w", None)
            if w is not None and w.isRunning():
                w.wait()
            if self.card:
                self.card.close(); self.card = None
            super().done(r)


# --------------------------------------------------------------------------- #
#  self-test / CLI
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    img = sys.argv[1] if len(sys.argv) > 1 else r"E:/mymc-alpha-2.7/NM00057.bin"
    with Ps2mcCard(img) as c:
        for k, v in c.info().items():
            print(f"  {k:20s} {v}")
        print("\nroot:")
        for e in c.listdir("/"):
            tag = "[dir]" if e["is_dir"] else "     "
            print(f"  {tag} {e['name']:16s} {e['length']:>10,} B  cluster={e['cluster']}")

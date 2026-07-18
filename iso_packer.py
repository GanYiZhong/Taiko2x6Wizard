#!/usr/bin/env python3
"""
ISO packer — inject modified DATA.000 + LIST.BIN back into the game ISO.

The Taiko SYSTEM256 disc (E:\\Taiko No Tatsujin 8.iso) is a minimal ISO 9660 /
UDF-bridge image whose root contains exactly two files: DATA.000 (first, at
LBA 268) and LIST.BIN (immediately after). The PS2/arcade CDVD reader resolves
files by walking the ISO 9660 directory, so we only need to:

  * keep the original header region [0, 268) verbatim (system area, PVD, UDF
    descriptors, path tables, root directory),
  * write the new DATA.000 at LBA 268 and LIST.BIN right after it,
  * patch the root directory records (both-endian extent LBA + data length) for
    DATA.000 and LIST.BIN,
  * patch the PVD volume-space-size (both-endian).

This works even when the files grow (our DATA.000 grows when songs are added).
The UDF descriptors are left as-is; the PS2 reads ISO 9660, not UDF.
"""
from __future__ import annotations

import logging
import struct
import time
from pathlib import Path

# Child of the app's "taiko" logger — inherits its console handler under the GUI.
log = logging.getLogger("taiko.isopacker")

SEC = 2048
DATA_LBA = 268                      # original start LBA of DATA.000 (kept)


def _find_root_records(header: bytes, root_lba: int, names: set) -> dict:
    """Find directory-record byte offsets (within header) for the given names.

    Assumes a single-extent root directory contiguous from ``root_lba`` and
    fully contained in ``header``. Malformed records (rec_len too small, or a
    name running past the extent / buffer) terminate the walk rather than
    looping forever or reading across boundaries.
    """
    base = root_lba * SEC
    if base + 10 + 4 > len(header):
        raise ValueError(
            "root directory LBA %d lies outside the buffered header" % root_lba)
    length = struct.unpack_from("<I", header, base + 10)[0]   # root '.' data length
    if base + length > len(header):
        raise ValueError(
            "root directory extent (lba %d, %d bytes) exceeds the buffered "
            "header; multi-extent or relocated roots are not supported"
            % (root_lba, length))
    out = {}
    pos = 0
    while pos < length:
        rec_len = header[base + pos]
        if rec_len == 0:
            # padding to next logical sector
            pos = ((pos // SEC) + 1) * SEC
            continue
        # A directory record is >= 33 bytes (fixed part) + name; reject runts
        # and records whose name would spill past this extent.
        if rec_len < 33:
            break
        nlen = header[base + pos + 32]
        if pos + 33 + nlen > length or base + pos + 33 + nlen > len(header):
            break
        name = header[base + pos + 33: base + pos + 33 + nlen]
        nm = name.split(b";")[0].decode("ascii", "replace").upper()
        if nm in names:
            out[nm] = base + pos
        pos += rec_len
    return out


def _patch_record(buf: bytearray, off: int, lba: int, size: int):
    struct.pack_into("<I", buf, off + 2, lba)     # extent LBA, little-endian
    struct.pack_into(">I", buf, off + 6, lba)     # extent LBA, big-endian
    struct.pack_into("<I", buf, off + 10, size)   # data length, little-endian
    struct.pack_into(">I", buf, off + 14, size)   # data length, big-endian


# --------------------------------------------------------------------------- #
#  UDF (ECMA-167) patching
#
#  The Taiko disc is a UDF-bridge image: the same DATA.000 / LIST.BIN are
#  described by BOTH ISO 9660 directory records AND a UDF filesystem. Tools like
#  7-Zip (and some loaders) read the UDF view, so if we only patch ISO 9660 the
#  UDF File Entries keep the OLD sizes / extents — the disc looks unchanged and a
#  UDF reader gets stale (truncated) data. This module patches the UDF metadata
#  to match, recomputing each descriptor's tag checksum + CRC.
#
#  All UDF structures we touch (AVDP@256, VDS partition descriptors, LVID, and
#  the file entries just below LBA 268) live inside the header region the packer
#  already buffers, so this is a cheap in-place edit.
# --------------------------------------------------------------------------- #
_UDF_CRC_TABLE = []
for _n in range(256):
    _c = _n << 8
    for _ in range(8):
        _c = ((_c << 1) ^ 0x1021) if (_c & 0x8000) else (_c << 1)
        _c &= 0xFFFF
    _UDF_CRC_TABLE.append(_c)


def _udf_crc(data) -> int:
    """ECMA-167 / ITU-T CRC-16 (poly 0x1021, init 0)."""
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _UDF_CRC_TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


def _udf_fix_tag(buf: bytearray, off: int):
    """Recompute a descriptor tag's DescriptorCRC then TagChecksum in place.

    Uses the tag's own DescriptorCRCLength (bytes 10-11) — the body length is
    unchanged by our edits, so the stored length stays valid. CRC covers the
    bytes right after the 16-byte tag; the checksum is the mod-256 sum of the
    16 tag bytes excluding the checksum byte itself (byte 4).
    """
    crclen = struct.unpack_from("<H", buf, off + 10)[0]
    crc = _udf_crc(buf[off + 16: off + 16 + crclen])
    struct.pack_into("<H", buf, off + 8, crc)
    ck = (sum(buf[off:off + 16]) - buf[off + 4]) & 0xFF
    buf[off + 4] = ck


def _udf_tag_id(buf: bytes, sec: int) -> int:
    o = sec * SEC
    return struct.unpack_from("<H", buf, o)[0] if o + 2 <= len(buf) else -1


def _patch_udf(header: bytearray, data_lba: int, data_size: int,
               list_lba: int, list_size: int, delta: int = 0) -> dict:
    """Patch the UDF File Entries / partition size to match the new layout.

    `delta` is how many sectors everything after DATA.000 shifted (0 when file
    sizes are unchanged). Returns a report dict; on any unmet assumption it
    records a warning and leaves that structure untouched rather than risk
    corrupting the disc. A non-UDF (plain ISO 9660) image is a no-op.
    """
    rep: dict = {"udf": False, "warnings": [], "patched": []}
    # UDF Volume Recognition Sequence lives at sectors 16.. — require an NSR0x.
    vrs = header[16 * SEC: 24 * SEC]
    if b"NSR0" not in vrs:
        return rep                      # plain ISO 9660, nothing to do
    if _udf_tag_id(header, 256) != 2:   # Anchor Volume Descriptor Pointer
        rep["warnings"].append("no AVDP at sector 256; UDF left unpatched")
        return rep
    rep["udf"] = True

    def blocks(nbytes: int) -> int:
        return (nbytes + SEC - 1) // SEC

    # Single short_ad extents only; a >1 GiB file would need multiple ADs.
    if data_size >= 0x40000000 or list_size >= 0x40000000:
        rep["warnings"].append(
            "file exceeds 1 GiB single-extent limit; UDF left unpatched")
        return rep

    # -- locate VDS, partition descriptors, LVD/LVID --------------------------
    mvds_loc = struct.unpack_from("<I", header, 256 * SEC + 20)[0]
    mvds_len = struct.unpack_from("<I", header, 256 * SEC + 16)[0]
    rvds_loc = struct.unpack_from("<I", header, 256 * SEC + 28)[0]
    rvds_len = struct.unpack_from("<I", header, 256 * SEC + 24)[0]
    part_offs, lvid_off, part_start = [], None, None
    for base_loc, base_len in ((mvds_loc, mvds_len), (rvds_loc, rvds_len)):
        for s in range(base_loc, base_loc + base_len // SEC + 1):
            if (s + 1) * SEC > len(header):
                break
            tid = _udf_tag_id(header, s)
            if tid == 5:                                    # Partition Descriptor
                part_offs.append(s * SEC)
                if part_start is None:
                    part_start = struct.unpack_from("<I", header, s * SEC + 188)[0]
            elif tid == 6:                                  # Logical Volume Desc
                iloc = struct.unpack_from("<I", header, s * SEC + 436)[0]
                if lvid_off is None and iloc and (iloc + 1) * SEC <= len(header) \
                        and _udf_tag_id(header, iloc) == 9:
                    lvid_off = iloc * SEC
    if part_start is None:
        rep["warnings"].append("no UDF partition descriptor found; unpatched")
        return rep

    # -- find the two regular-file File Entries (fileType 5) ------------------
    data_lb = data_lba - part_start
    list_lb = list_lba - part_start
    fe = {}
    for s in range(part_start, data_lba):
        if (s + 1) * SEC > len(header):
            break
        o = s * SEC
        if struct.unpack_from("<H", header, o)[0] != 261:   # File Entry
            continue
        if header[o + 27] != 5:                             # not a regular file
            continue
        if (struct.unpack_from("<H", header, o + 34)[0] & 7) != 0:
            continue                                        # not short_ad
        l_ea = struct.unpack_from("<I", header, o + 168)[0]
        l_ad = struct.unpack_from("<I", header, o + 172)[0]
        if l_ad != 8:                                       # single short_ad only
            continue
        ad = o + 176 + l_ea
        lb = struct.unpack_from("<I", header, ad + 4)[0]
        fe[lb] = (o, ad)

    def patch_fe(o, ad, size, new_lb):
        struct.pack_into("<Q", header, o + 56, size)                 # InformationLength
        struct.pack_into("<Q", header, o + 64, blocks(size))         # LogicalBlocksRecorded
        struct.pack_into("<I", header, ad, size)                     # short_ad extent length (type 0)
        struct.pack_into("<I", header, ad + 4, new_lb)               # short_ad extent location
        _udf_fix_tag(header, o)

    # DATA.000 keeps its start block; identify it by that lb, LIST.BIN is the other.
    if data_lb in fe:
        o, ad = fe.pop(data_lb)
        patch_fe(o, ad, data_size, data_lb)
        rep["patched"].append("DATA.000 FE")
    else:
        rep["warnings"].append("DATA.000 UDF File Entry not found; unpatched")
    if fe:
        o, ad = next(iter(fe.values()))                     # the remaining file FE
        patch_fe(o, ad, list_size, list_lb)
        rep["patched"].append("LIST.BIN FE")
    else:
        rep["warnings"].append("LIST.BIN UDF File Entry not found; unpatched")

    # -- grow the partition by the layout shift -------------------------------
    # Add `delta` (the number of sectors everything after DATA.000 moved by) to
    # the ORIGINAL partition length rather than recomputing it from LIST.BIN's
    # end. Recomputing dropped the disc's trailing slack (the anchor run sits
    # inside the partition), shrinking it by a block; growing by delta preserves
    # the original relationship exactly and is a true no-op when delta == 0.
    for po in part_offs:
        old_len = struct.unpack_from("<I", header, po + 192)[0]
        struct.pack_into("<I", header, po + 192, old_len + delta)
        _udf_fix_tag(header, po)
    if part_offs:
        rep["patched"].append(f"partition length += {delta}")

    # -- keep the LVID size table consistent with the grown partition ---------
    if lvid_off is not None:
        npart = struct.unpack_from("<I", header, lvid_off + 72)[0]
        if npart >= 1:
            so = lvid_off + 80 + 4 * npart
            struct.pack_into("<I", header, so,
                             struct.unpack_from("<I", header, so)[0] + delta)
            _udf_fix_tag(header, lvid_off)
            rep["patched"].append("LVID size table")
    rep["part_start"] = part_start
    return rep


def pack_iso(orig_iso, data_path, list_path, out_iso,
             progress=lambda done, total, msg: None) -> dict:
    """Build `out_iso` from the original ISO header + new DATA.000/LIST.BIN."""
    orig_iso, data_path = Path(orig_iso), Path(data_path)
    list_path, out_iso = Path(list_path), Path(out_iso)

    data_size = data_path.stat().st_size
    list_size = list_path.stat().st_size
    data_sectors = (data_size + SEC - 1) // SEC
    list_sectors = (list_size + SEC - 1) // SEC

    orig_total = orig_iso.stat().st_size // SEC
    _t0 = time.perf_counter()
    log.info("pack_iso: %s (DATA %.1f MB, LIST %d B) -> %s",
             orig_iso.name, data_size / 1e6, list_size, out_iso.name)

    with open(orig_iso, "rb") as f:
        header = bytearray(f.read(DATA_LBA * SEC))
    if len(header) < DATA_LBA * SEC:
        raise ValueError(
            "original ISO is smaller than the %d-sector header region"
            % DATA_LBA)
    if header[16 * SEC:16 * SEC + 6] != b"\x01CD001":
        raise ValueError("ISO 9660 PVD not found at sector 16")

    pvd = 16 * SEC

    root_lba = struct.unpack_from("<I", header, pvd + 156 + 2)[0]
    # The root directory must live inside the buffered header region for the
    # single-extent walk below to be valid.
    if not 0 < root_lba < DATA_LBA:
        raise ValueError(
            "root directory LBA %d is outside the buffered header [1, %d)"
            % (root_lba, DATA_LBA))
    recs = _find_root_records(header, root_lba, {"DATA.000", "LIST.BIN"})
    if "DATA.000" not in recs or "LIST.BIN" not in recs:
        raise ValueError("DATA.000 / LIST.BIN not found in ISO root directory")

    # Original layout, so we know where the ORIGINAL trailing region (the UDF
    # backup-anchor run) starts and can carry it over. A UDF reader looks for a
    # backup Anchor at the last sector of the disc; dropping it (as a plain
    # header+data+list rebuild does) makes some loaders reject the disc.
    orig_data_lba = struct.unpack_from("<I", header, recs["DATA.000"] + 2)[0]
    orig_list_lba = struct.unpack_from("<I", header, recs["LIST.BIN"] + 2)[0]
    orig_list_size = struct.unpack_from("<I", header, recs["LIST.BIN"] + 10)[0]
    orig_list_end = orig_list_lba + (orig_list_size + SEC - 1) // SEC
    tail_len = max(0, orig_total - orig_list_end)   # trailing anchor run, sectors

    data_lba = orig_data_lba if orig_data_lba > 0 else DATA_LBA
    if data_lba > DATA_LBA:
        raise ValueError(
            "DATA.000 starts at LBA %d, past the %d-sector header buffer; "
            "cannot place payload without overwriting header"
            % (data_lba, DATA_LBA))
    list_lba = data_lba + data_sectors
    new_list_end = list_lba + list_sectors
    new_total = new_list_end + tail_len
    delta = new_total - orig_total                  # how far the tail moved
    log.info("layout: DATA@%d LIST@%d tail=%d sectors, delta=%+d, total=%d sectors",
             data_lba, list_lba, tail_len, delta, new_total)

    # Read the original trailing anchor run so we can re-emit it at the new end.
    tail = b""
    if tail_len:
        with open(orig_iso, "rb") as f:
            f.seek(orig_list_end * SEC)
            tail = bytearray(f.read(tail_len * SEC))
        # If the layout shifted, relocate each Anchor Volume Descriptor Pointer:
        # rewrite its self TagLocation and fix the tag checksum/CRC.
        if delta:
            for k in range(tail_len):
                o = k * SEC
                if struct.unpack_from("<H", tail, o)[0] == 2:      # AVDP
                    struct.pack_into("<I", tail, o + 12, new_list_end + k)
                    _udf_fix_tag(tail, o)

    # Patch the PVD volume space size (both-endian) to the new total (incl tail).
    struct.pack_into("<I", header, pvd + 80, new_total)
    struct.pack_into(">I", header, pvd + 84, new_total)

    _patch_record(header, recs["DATA.000"], data_lba, data_size)
    _patch_record(header, recs["LIST.BIN"], list_lba, list_size)

    # Patch the UDF filesystem too, so a UDF reader (7-Zip, some loaders) sees
    # the same grown DATA.000 / moved LIST.BIN as ISO 9660 does.
    udf = _patch_udf(header, data_lba, data_size, list_lba, list_size, delta)
    if udf.get("udf"):
        log.info("UDF patched: %s%s", ", ".join(udf["patched"]) or "nothing",
                 "  warnings: " + "; ".join(udf["warnings"]) if udf["warnings"] else "")

    total_bytes = data_size + list_size
    done = 0
    next_log = 0.2                                   # console progress every ~20%
    with open(out_iso, "wb") as o:
        # Write exactly data_lba sectors of header so DATA.000 lands on its
        # declared start LBA even if we buffered more than we emit.
        o.write(header[:data_lba * SEC])
        for src, nsec, sz in ((data_path, data_sectors, data_size),
                              (list_path, list_sectors, list_size)):
            with open(src, "rb") as f:
                while True:
                    chunk = f.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    o.write(chunk)
                    done += len(chunk)
                    progress(done, total_bytes, src.name)
                    if done / total_bytes >= next_log:
                        log.info("writing… %.0f%% (%.0f/%.0f MB)",
                                 done / total_bytes * 100, done / 1e6, total_bytes / 1e6)
                        next_log += 0.2
            pad = nsec * SEC - sz
            if pad:
                o.write(b"\0" * pad)
        if tail:
            o.write(tail)                            # UDF backup-anchor run

    log.info("pack_iso done: %.1f MB (%d sectors) in %.1fs",
             new_total * SEC / 1e6, new_total, time.perf_counter() - _t0)
    return {"out": str(out_iso), "data_lba": data_lba, "list_lba": list_lba,
            "total_sectors": new_total, "size": new_total * SEC,
            "data_size": data_size, "list_size": list_size,
            "tail_sectors": tail_len, "udf": udf}


def repair_udf(iso_path) -> dict:
    """Retrofit the UDF filesystem of an already-built ISO to match ISO 9660.

    Reads DATA.000 / LIST.BIN extent + size from the ISO 9660 root directory,
    patches the (stale) UDF File Entries / partition to match, and rewrites only
    the header sectors — no full-image rewrite. Fixes discs produced before UDF
    patching existed.
    """
    iso_path = Path(iso_path)
    with open(iso_path, "rb") as f:
        header = bytearray(f.read(DATA_LBA * SEC))
    if header[16 * SEC:16 * SEC + 6] != b"\x01CD001":
        raise ValueError("ISO 9660 PVD not found at sector 16")
    pvd = 16 * SEC
    root_lba = struct.unpack_from("<I", header, pvd + 156 + 2)[0]
    recs = _find_root_records(header, root_lba, {"DATA.000", "LIST.BIN"})
    if "DATA.000" not in recs or "LIST.BIN" not in recs:
        raise ValueError("DATA.000 / LIST.BIN not found in ISO root directory")
    data_lba = struct.unpack_from("<I", header, recs["DATA.000"] + 2)[0]
    data_size = struct.unpack_from("<I", header, recs["DATA.000"] + 10)[0]
    list_lba = struct.unpack_from("<I", header, recs["LIST.BIN"] + 2)[0]
    list_size = struct.unpack_from("<I", header, recs["LIST.BIN"] + 10)[0]
    if data_lba > DATA_LBA:
        raise ValueError("DATA.000 starts past the buffered header; cannot repair")
    rep = _patch_udf(header, data_lba, data_size, list_lba, list_size)
    if rep.get("patched"):
        with open(iso_path, "r+b") as f:
            f.write(header[:data_lba * SEC])
        log.info("repair_udf %s: %s", iso_path.name, ", ".join(rep["patched"]))
    rep.update(data_lba=data_lba, data_size=data_size,
               list_lba=list_lba, list_size=list_size)
    return rep


# --------------------------------------------------------------------------- #
#  Qt dialog
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtCore import Qt, QThread, Signal
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QPushButton,
        QFileDialog, QLabel, QProgressBar, QMessageBox, QWidget,
    )

    class _PackWorker(QThread):
        prog = Signal(int, int, str)
        done = Signal(object)

        def __init__(self, orig, data, lst, out):
            super().__init__()
            self.args = (orig, data, lst, out)

        def run(self):
            try:
                r = pack_iso(*self.args, progress=lambda d, t, m: self.prog.emit(d, t, m))
                self.done.emit(r)
            except Exception as exc:
                import traceback
                self.done.emit(("ERROR", exc, traceback.format_exc()))

    class PackIsoDialog(QDialog):
        def __init__(self, data_path, list_path, default_iso="", parent=None):
            super().__init__(parent)
            self.setWindowTitle("Pack to ISO")
            self.resize(640, 240)
            lay = QVBoxLayout(self)
            form = QFormLayout()
            self.ed_orig = QLineEdit(default_iso)
            b_o = QPushButton("…"); b_o.clicked.connect(lambda: self._pick(self.ed_orig, "ISO (*.iso)"))
            self.ed_data = QLineEdit(str(data_path))
            self.ed_list = QLineEdit(str(list_path))
            self.ed_out = QLineEdit(str(Path(default_iso).with_name(
                Path(default_iso).stem + ".patched.iso")) if default_iso else "")
            b_out = QPushButton("…"); b_out.clicked.connect(lambda: self._save(self.ed_out))
            form.addRow("original ISO:", self._row(self.ed_orig, b_o))
            form.addRow("DATA.000:", self.ed_data)
            form.addRow("LIST.BIN:", self.ed_list)
            form.addRow("output ISO:", self._row(self.ed_out, b_out))
            lay.addLayout(form)
            self.bar = QProgressBar()
            lay.addWidget(self.bar)
            self.lbl = QLabel(""); self.lbl.setStyleSheet("color:#999;")
            lay.addWidget(self.lbl)
            btns = QHBoxLayout(); btns.addStretch(1)
            self.b_go = QPushButton("Build ISO"); self.b_go.clicked.connect(self._go)
            b_c = QPushButton("Close"); b_c.clicked.connect(self.reject)
            btns.addWidget(self.b_go); btns.addWidget(b_c)
            lay.addLayout(btns)

        def _row(self, e, b):
            w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(e, 1); h.addWidget(b)
            return w

        def _pick(self, e, filt):
            import appconfig                       # last-used-path memory
            p = appconfig.pick_open(self, appconfig.key_for_filter(filt),
                                    "Choose", filt)
            if p:
                e.setText(p)

        def _save(self, e):
            import appconfig
            p = appconfig.pick_save(self, "iso_out", "Output ISO", "",
                                    "ISO (*.iso)")
            if p:
                e.setText(p)

        def _go(self):
            for e, what in ((self.ed_orig, "original ISO"), (self.ed_data, "DATA.000"),
                            (self.ed_list, "LIST.BIN")):
                if not e.text() or not Path(e.text()).exists():
                    QMessageBox.warning(self, "Pack to ISO", f"Missing {what}.")
                    return
            if not self.ed_out.text():
                QMessageBox.warning(self, "Pack to ISO", "Choose an output ISO path.")
                return
            self.b_go.setEnabled(False)
            self._worker = _PackWorker(self.ed_orig.text(), self.ed_data.text(),
                                       self.ed_list.text(), self.ed_out.text())
            self._worker.prog.connect(self._on_prog)
            self._worker.done.connect(self._on_done)
            self._worker.start()

        def _on_prog(self, done, total, msg):
            self.bar.setMaximum(total); self.bar.setValue(done)
            self.lbl.setText(f"writing {msg}… {done/1e6:.0f}/{total/1e6:.0f} MB")

        def _on_done(self, r):
            self.b_go.setEnabled(True)
            if isinstance(r, tuple) and r and r[0] == "ERROR":
                QMessageBox.critical(self, "Pack failed", str(r[1]))
                return
            self.lbl.setText(f"done: {r['size']/1e6:.0f} MB, {r['total_sectors']} sectors")
            QMessageBox.information(
                self, "ISO built",
                f"Wrote {r['out']}\n{r['size']/1e6:.1f} MB ({r['total_sectors']} sectors)\n"
                f"DATA.000 @ LBA {r['data_lba']} ({r['data_size']:,} B)\n"
                f"LIST.BIN @ LBA {r['list_lba']} ({r['list_size']:,} B)")

except ImportError:
    PackIsoDialog = None  # type: ignore


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 5:
        print(pack_iso(*sys.argv[1:5], progress=lambda d, t, m: None))
    else:
        print("usage: python iso_packer.py <orig.iso> <DATA.000> <LIST.BIN> <out.iso>")

"""Taiko 14 USB keyboard (+ dummy JVS) patch.

Ported from https://github.com/GanYiZhong/Taiko14-USB-Keyboard-Patch (MIT).
The IOP module ``KBD2EE.irx`` (bundled in ``exe/data/``, the hardware-
validated "full" build: keyboard + boot without an I/O board) reads a USB HID
keyboard and writes the drum/button state straight into the game's JVS input
structure. It has to be inside an ``IRXARC.BIN`` module archive the game loads.

Two ways to get it loaded:

* **HDD (no card change)** - ``taiko`` (``Game::LoadModules``) opens
  ``pfs0:IRXARC.BIN;1`` FIRST and only falls back to ``mc0:IRXARC.BIN`` on the
  dongle card; pfs0 is the partition ``T14LOAD`` booted ``taiko`` from
  (``t14jp1400.0001``). It then loads EVERY module in the archive, in order.
  So writing "stock card IRXARC + KBD2EE" into that partition is enough; the
  card is untouched. Written as both ``IRXARC.BIN;1`` and ``IRXARC.BIN``
  because whether the PFS driver strips the ``;1`` is unverified.
  *Not yet tested on hardware.*
* **Card (the original, hardware-tested method)** - append KBD2EE to the
  card's ``IRXARC.BIN`` and add its name to ``T14LOAD``'s module list.

Keys: 1P D F J K, 2P Z X C V, COIN 5, SERVICE 9, TEST F1 (toggle), SELECT
up/down/Enter. The keyboard must be unplugged and replugged once after boot.

CLI::

    python -m taiko2x6.exe.usb_keyboard status  TK14.img
    python -m taiko2x6.exe.usb_keyboard install TK14.img --card NM00057.bin
    python -m taiko2x6.exe.usb_keyboard remove  TK14.img
    python -m taiko2x6.exe.usb_keyboard install NM00057.ps2      # card method
    python -m taiko2x6.exe.usb_keyboard files --in dumped/ --out out/
"""
from __future__ import annotations

import hashlib
import struct
import sys
import zlib
from pathlib import Path

__all__ = [
    "KeyboardPatchError", "bundled_irx", "patch_irxarc", "unpatch_irxarc",
    "irxarc_modules", "patch_t14load", "unpatch_t14load", "t14load_has_kbd",
    "hdd_status", "hdd_install", "hdd_remove", "card_status", "card_install",
    "card_remove", "KEYMAP",
]

MODULE = "KBD2EE"
IRX_SHA256_PREFIX = "34F1CC379FB27157"          # the hardware-validated build
HDD_NAMES = ("IRXARC.BIN;1", "IRXARC.BIN")      # see module docstring
# T14LOAD module-name list: one free slot + a zero run used as a string cave
PTR_OFF, STR_OFF, STR_VA = 0x32664, 0x34AC4, 0x01033AC4

KEYMAP = [
    ("1P drum: Ka L / Don L / Don R / Ka R", "D  F  J  K"),
    ("2P drum: Ka L / Don L / Don R / Ka R", "Z  X  C  V"),
    ("COIN", "5"),
    ("SERVICE", "9"),
    ("TEST (toggles like the real switch)", "F1"),
    ("SELECT up / down / enter", "↑  ↓  Enter"),
]


class KeyboardPatchError(RuntimeError):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16].upper()


def bundled_irx() -> bytes:
    """The bundled KBD2EE.irx (checked against the validated build)."""
    p = Path(__file__).resolve().parent / "data" / "KBD2EE.irx"
    if not p.is_file():
        raise KeyboardPatchError(f"bundled module missing: {p}")
    data = p.read_bytes()
    if _sha(data) != IRX_SHA256_PREFIX:
        raise KeyboardPatchError(f"{p.name} is not the validated build "
                                 f"(sha256 {_sha(data)})")
    return data


# --------------------------------------------------------------------------- #
#  IRXARC.BIN container (format from TAIKO DRUM TOOLS, MIT)
#    file = scramble(zlib(blob)); blob[0] = module count; 0x40 header, then per
#    module: u32 elf size, u32 arg size, name @0x10, args @0x40, ELF @0x140
# --------------------------------------------------------------------------- #
def _scramble(d: bytes) -> bytes:
    """Involution: the same call scrambles and unscrambles."""
    return bytes(b ^ (~(i & 0xA5) & 0xFF) for i, b in enumerate(d))


def _align16(x: int) -> int:
    return (x + 0xF) & ~0xF


def _parse(blob) -> tuple[int, list]:
    """-> (count, [(name, argsz, elf_off, size, hdr_off, end_off), ...])"""
    n = blob[0]
    cur = 0x40
    ents = []
    for _ in range(n):
        hdr = cur
        size, argsz = struct.unpack_from("<II", blob, cur)
        name = bytes(blob[cur + 0x10:blob.index(b"\0", cur + 0x10)]).decode("latin1")
        elf_off = cur + 0x140
        cur = _align16(elf_off + size)
        ents.append((name, argsz, elf_off, size, hdr, cur))
    return n, ents


def _make_entry(name: str, elf: bytes) -> bytes:
    hdr = bytearray(0x140)
    struct.pack_into("<I", hdr, 0, _align16(len(elf)))
    hdr[0x10:0x10 + len(name)] = name.encode("ascii")
    return bytes(hdr) + elf + b"\0" * ((-len(elf)) % 16)


def _decode(raw: bytes) -> bytearray:
    try:
        return bytearray(zlib.decompress(_scramble(raw)))
    except zlib.error as exc:
        raise KeyboardPatchError(f"not an IRXARC.BIN archive ({exc})") from None


def _encode(blob) -> bytes:
    return _scramble(zlib.compress(bytes(blob), 9))


def irxarc_modules(raw: bytes) -> list[str]:
    return [e[0] for e in _parse(_decode(raw))[1]]


def _strip(blob: bytearray) -> bytearray:
    while True:
        n, ents = _parse(blob)
        hit = [e for e in ents if e[0] == MODULE]
        if not hit:
            return blob
        e = hit[0]
        del blob[e[4]:e[5]]
        blob[0] = n - 1


def unpatch_irxarc(raw: bytes) -> bytes:
    """Remove every KBD2EE entry (returns the input unchanged if none)."""
    blob = _decode(raw)
    if MODULE not in [e[0] for e in _parse(blob)[1]]:
        return raw
    return _encode(_strip(blob))


def patch_irxarc(raw: bytes, irx: bytes | None = None, log=None) -> bytes:
    """Append KBD2EE as the last module (a stale copy is removed first)."""
    irx = irx if irx is not None else bundled_irx()
    blob = _strip(_decode(raw))
    n, ents = _parse(blob)
    names = [e[0] for e in ents]
    if "usbd.irx" not in names:
        raise KeyboardPatchError("this IRXARC.BIN has no usbd.irx - not a "
                                 "Taiko 14 module archive?")
    if len(blob) % 16:
        raise KeyboardPatchError("archive length is not 16-byte aligned")
    blob += _make_entry(MODULE, irx)
    blob[0] = n + 1
    n2, ents2 = _parse(blob)
    k = [e[0] for e in ents2].index(MODULE)
    if n2 != n + 1 or bytes(blob[ents2[k][2]:ents2[k][2] + len(irx)]) != irx:
        raise KeyboardPatchError("self-check failed after appending KBD2EE")
    if log:
        log(f"IRXARC: {n} modules + {MODULE} ({len(irx):,} B) as module {n + 1}")
    return _encode(blob)


def _irxarc_info(raw: bytes) -> dict:
    blob = _decode(raw)
    n, ents = _parse(blob)
    names = [e[0] for e in ents]
    info = {"modules": n, "installed": MODULE in names, "last": names[-1] if names else None}
    if info["installed"]:
        e = ents[names.index(MODULE)]
        stored = bytes(blob[e[2]:e[2] + e[3]])      # size field is 16-aligned
        try:
            ref = bundled_irx()
            info["matches_bundled"] = stored[:len(ref)] == ref and \
                not any(stored[len(ref):])
        except KeyboardPatchError:
            pass
        info["count"] = names.count(MODULE)
    return info


# --------------------------------------------------------------------------- #
#  T14LOAD name list (card method only)
# --------------------------------------------------------------------------- #
def t14load_has_kbd(t: bytes) -> bool:
    return (struct.unpack_from("<I", t, PTR_OFF)[0] == STR_VA
            and t[STR_OFF:STR_OFF + 7] == b"KBD2EE\0")


def patch_t14load(t: bytes) -> bytes:
    t = bytearray(t)
    if len(t) < STR_OFF + 8:
        raise KeyboardPatchError("T14LOAD is too short - wrong file?")
    cur = struct.unpack_from("<I", t, PTR_OFF)[0]
    if cur not in (0, STR_VA):
        raise KeyboardPatchError(f"T14LOAD name-list slot holds {cur:#x}, "
                                 "expected 0 - not the supported Taiko 14 build")
    if t[STR_OFF:STR_OFF + 7] not in (b"\0" * 7, b"KBD2EE\0"):
        raise KeyboardPatchError("T14LOAD string cave is occupied")
    struct.pack_into("<I", t, PTR_OFF, STR_VA)
    t[STR_OFF:STR_OFF + 7] = b"KBD2EE\0"
    if struct.unpack_from("<I", t, PTR_OFF + 4)[0] != 0:
        raise KeyboardPatchError("T14LOAD name list would not stay NUL-terminated")
    return bytes(t)


def unpatch_t14load(t: bytes) -> bytes:
    if not t14load_has_kbd(t):
        return t
    t = bytearray(t)
    struct.pack_into("<I", t, PTR_OFF, 0)
    t[STR_OFF:STR_OFF + 7] = b"\0" * 7
    return bytes(t)


# --------------------------------------------------------------------------- #
#  sources
# --------------------------------------------------------------------------- #
def _is_card(path) -> bool:
    return Path(path).stat().st_size in (8650752, 8388608)


def read_card_files(card) -> tuple[bytes, bytes]:
    """(IRXARC.BIN, T14LOAD) from a dongle memory-card image."""
    from taiko2x6.ps2.ps2mc_card import Ps2mcCard
    with Ps2mcCard(card) as c:
        names = {e["name"] for e in c.listdir("/")}
        if "IRXARC.BIN" not in names or "T14LOAD" not in names:
            raise KeyboardPatchError(f"{Path(card).name} is not a Taiko 14 card "
                                     "(needs IRXARC.BIN and T14LOAD)")
        return c.read_file("/IRXARC.BIN"), c.read_file("/T14LOAD")


def stock_irxarc(source) -> bytes:
    """Stock IRXARC.BIN from a card image or an IRXARC.BIN file (any KBD2EE
    already in it is removed)."""
    p = Path(source)
    raw = read_card_files(p)[0] if _is_card(p) else p.read_bytes()
    return unpatch_irxarc(raw)


# --------------------------------------------------------------------------- #
#  HDD method
# --------------------------------------------------------------------------- #
def find_game_partition(h) -> str:
    """The PFS partition holding `taiko` (what T14LOAD mounts as pfs0)."""
    for p in h.partitions():
        if not p["is_pfs"]:
            continue
        try:
            names = {e["name"] for e in h.pfs_listdir(p["name"], "/")}
        except Exception:
            continue
        if "taiko" in names:
            return p["name"]
    raise KeyboardPatchError("no partition with `taiko` - not a Taiko 14+ HDD image")


def hdd_status(img, partition: str | None = None) -> dict:
    from taiko2x6.ps2 import ps2hdd
    h = ps2hdd.Ps2Hdd(str(img))
    try:
        part = partition or find_game_partition(h)
        names = {e["name"] for e in h.pfs_listdir(part, "/")}
        st = {"partition": part, "files": [n for n in HDD_NAMES if n in names]}
        if st["files"]:
            st.update(_irxarc_info(h.pfs_read(part, "/" + st["files"][0])))
        else:
            st["installed"] = False
        return st
    finally:
        h.close()


def hdd_install(img, source=None, partition: str | None = None, irx=None,
                log=print) -> dict:
    """Write stock IRXARC + KBD2EE into the game partition of a T14 image.

    ``source`` (card image or IRXARC.BIN) is only needed the first time; a
    re-install reuses the archive already on the HDD. Undo-journaled
    (ps2hdd.write_session): an error rolls the image back."""
    from taiko2x6.ps2 import ps2hdd
    st = hdd_status(img, partition)
    part = st["partition"]
    if source is not None:
        stock = stock_irxarc(source)
    elif st["files"]:
        h = ps2hdd.Ps2Hdd(str(img))
        try:
            stock = unpatch_irxarc(h.pfs_read(part, "/" + st["files"][0]))
        finally:
            h.close()
    else:
        raise KeyboardPatchError("first install needs the stock IRXARC.BIN: "
                                 "pass the dongle card image or a dumped IRXARC.BIN")
    new = patch_irxarc(stock, irx, log=log)
    with ps2hdd.write_session(str(img), log=log) as w:
        have = {e["name"] for e in w.pfs_listdir(part, "/")}
        for name in HDD_NAMES:
            if name in have:
                w.pfs_write(part, "/" + name, new)
                log(f"updated {part}:/{name} ({len(new):,} B)")
            else:
                w.pfs_create(part, "/" + name, new)
                log(f"created {part}:/{name} ({len(new):,} B)")
        for name in HDD_NAMES:
            if w.pfs_read(part, "/" + name) != new:
                raise KeyboardPatchError(f"read-back mismatch on {name}")
    return {"partition": part, "size": len(new), "undo": w.undo_path}


def hdd_remove(img, partition: str | None = None, log=print) -> dict:
    """Delete the IRXARC copies from the HDD (the game falls back to the card)."""
    from taiko2x6.ps2 import ps2hdd
    st = hdd_status(img, partition)
    if not st["files"]:
        log("nothing to remove")
        return {"partition": st["partition"], "removed": []}
    with ps2hdd.write_session(str(img), log=log) as w:
        for name in st["files"]:
            w.pfs_delete(st["partition"], "/" + name)
            log(f"deleted {st['partition']}:/{name}")
    return {"partition": st["partition"], "removed": st["files"], "undo": w.undo_path}


# --------------------------------------------------------------------------- #
#  card method (hardware-tested)
# --------------------------------------------------------------------------- #
def card_status(card) -> dict:
    arc, t = read_card_files(card)
    st = _irxarc_info(arc)
    st["t14load"] = t14load_has_kbd(t)
    return st


def _card_write(card, arc: bytes, t: bytes, log):
    from taiko2x6.ps2.ps2mc_card import Ps2mcCard
    with Ps2mcCard(card, writable=True) as w:       # makes <card>.bak first
        log(f"backup: {w.backup_path}")
        w.write_file("/IRXARC.BIN", arc)
        w.write_file("/T14LOAD", t)
    a2, t2 = read_card_files(card)
    if a2 != arc or t2 != t:
        raise KeyboardPatchError("card read-back mismatch - restore the .bak")


def card_install(card, irx=None, log=print) -> dict:
    arc, t = read_card_files(card)
    new_arc = patch_irxarc(arc, irx, log=log)
    new_t = patch_t14load(t)
    _card_write(card, new_arc, new_t, log)
    log(f"T14LOAD: name-list[{PTR_OFF:#x}] -> {STR_VA:#010x} (KBD2EE)")
    return {"irxarc": len(new_arc), "t14load": len(new_t)}


def card_remove(card, log=print) -> dict:
    arc, t = read_card_files(card)
    new_arc, new_t = unpatch_irxarc(arc), unpatch_t14load(t)
    if (new_arc, new_t) == (arc, t):
        log("card is not patched")
        return {"changed": False}
    _card_write(card, new_arc, new_t, log)
    log("removed KBD2EE from IRXARC.BIN and T14LOAD")
    return {"changed": True}


def patch_files(src_dir, out_dir, irx=None, log=print):
    """Original repo workflow: dumped IRXARC.BIN + T14LOAD -> out_dir (with
    *.ORIGINAL-BACKUP copies)."""
    src, out = Path(src_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in (("IRXARC.BIN", lambda d: patch_irxarc(d, irx, log=log)),
                     ("T14LOAD", patch_t14load)):
        raw = (src / name).read_bytes()
        (out / (name + ".ORIGINAL-BACKUP")).write_bytes(raw)
        (out / name).write_bytes(fn(raw))
        log(f"{name}: {len(raw):,} -> {(out / name).stat().st_size:,} B")


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m taiko2x6.exe.usb_keyboard",
        description="Taiko 14 USB keyboard (+ dummy JVS) patch. TARGET is a "
                    "T14 HDD .img (no card change) or a dongle card image.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "install", "remove"):
        sp = sub.add_parser(name)
        sp.add_argument("target", help="TK14 HDD .img, or dongle card .ps2/.bin")
        sp.add_argument("--partition", help="HDD partition (default: the one with `taiko`)")
        if name == "install":
            sp.add_argument("--card", help="HDD target: card image or IRXARC.BIN "
                                           "with the stock module archive")
    fp = sub.add_parser("files", help="patch dumped IRXARC.BIN + T14LOAD")
    fp.add_argument("--in", dest="src", required=True)
    fp.add_argument("--out", dest="dst", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "files":
            patch_files(a.src, a.dst)
            return 0
        card = _is_card(a.target)
        if a.cmd == "status":
            st = card_status(a.target) if card else hdd_status(a.target, a.partition)
            for k, v in st.items():
                print(f"{k:10s} {v}")
        elif a.cmd == "install":
            if card:
                card_install(a.target)
            else:
                hdd_install(a.target, a.card, a.partition)
            print("done. Unplug and replug the keyboard once after boot.")
        else:
            card_remove(a.target) if card else hdd_remove(a.target, a.partition)
    except (KeyboardPatchError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtCore import QThread, Signal
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
        QMessageBox, QPlainTextEdit, QGridLayout, QGroupBox, QRadioButton,
    )
except ImportError:                     # CLI-only environment
    QDialog = None
else:
    from taiko2x6.app import appconfig

    class _Worker(QThread):
        done = Signal(object)
        line = Signal(str)

        def __init__(self, fn):
            super().__init__()
            self._fn = fn

        def run(self):
            try:
                self.done.emit(self._fn(self.line.emit))
            except Exception as exc:
                self.done.emit(("ERROR", exc))

    class UsbKeyboardDialog(QDialog):
        """Install / remove the T14 USB keyboard patch on an HDD image or card."""

        def __init__(self, parent=None, default_img=""):
            super().__init__(parent)
            self.setWindowTitle("USB keyboard patch (Taiko 14)")
            self.resize(760, 560)
            self._worker = None
            lay = QVBoxLayout(self)

            mode = QGroupBox("Where to install")
            ml = QVBoxLayout(mode)
            self.rb_hdd = QRadioButton(
                "HDD image — write IRXARC.BIN into the game partition "
                "(card untouched; not yet tested on hardware)")
            self.rb_card = QRadioButton(
                "Dongle card image — patch IRXARC.BIN + T14LOAD on the card "
                "(the hardware-tested method)")
            self.rb_hdd.setChecked(True)
            ml.addWidget(self.rb_hdd); ml.addWidget(self.rb_card)
            lay.addWidget(mode)

            grid = QGridLayout()
            self.ed_target = QLineEdit(default_img)
            b1 = QPushButton("Browse…"); b1.clicked.connect(self._pick_target)
            self.ed_card = QLineEdit()
            self.ed_card.setPlaceholderText(
                "stock module archive: dongle card image or IRXARC.BIN "
                "(first HDD install only)")
            b2 = QPushButton("Browse…"); b2.clicked.connect(self._pick_card)
            self.lb_card = QLabel("Card / IRXARC.BIN:")
            grid.addWidget(QLabel("Target:"), 0, 0); grid.addWidget(self.ed_target, 0, 1)
            grid.addWidget(b1, 0, 2)
            grid.addWidget(self.lb_card, 1, 0); grid.addWidget(self.ed_card, 1, 1)
            grid.addWidget(b2, 1, 2)
            lay.addLayout(grid)
            self.rb_card.toggled.connect(self._mode_changed)

            keys = QLabel("<br>".join(f"<b>{k}</b>: {v}" for v, k in
                                       [(a, b) for a, b in KEYMAP])
                          + "<br><i>Unplug and replug the keyboard once after "
                            "boot. Close PCSX2 before writing.</i>")
            keys.setWordWrap(True)
            lay.addWidget(keys)

            row = QHBoxLayout()
            self.b_status = QPushButton("Status"); self.b_status.clicked.connect(self._status)
            self.b_install = QPushButton("Install"); self.b_install.clicked.connect(self._install)
            self.b_remove = QPushButton("Remove"); self.b_remove.clicked.connect(self._remove)
            for b in (self.b_status, self.b_install, self.b_remove):
                row.addWidget(b)
            row.addStretch(1)
            lay.addLayout(row)
            self.log = QPlainTextEdit(); self.log.setReadOnly(True)
            lay.addWidget(self.log, 1)

        def _mode_changed(self):
            hdd = self.rb_hdd.isChecked()
            self.ed_card.setEnabled(hdd); self.lb_card.setEnabled(hdd)

        def _pick_target(self):
            if self.rb_hdd.isChecked():
                p = appconfig.pick_open(self, "hddimg", "T14 HDD image",
                                        "HDD image (*.img);;All files (*)")
            else:
                p = appconfig.pick_open(self, "ps2mc", "Dongle card image",
                                        "Card image (*.ps2 *.bin);;All files (*)")
            if p:
                self.ed_target.setText(p)

        def _pick_card(self):
            p = appconfig.pick_open(self, "ps2mc", "Card image or IRXARC.BIN",
                                    "Card / IRXARC (*.ps2 *.bin *.BIN);;All files (*)")
            if p:
                self.ed_card.setText(p)

        def _target(self):
            t = self.ed_target.text().strip()
            if not t or not Path(t).is_file():
                QMessageBox.warning(self, "USB keyboard", "Choose a target file first.")
                return None
            return t

        def _run(self, fn):
            for b in (self.b_status, self.b_install, self.b_remove):
                b.setEnabled(False)
            self._worker = _Worker(fn)
            self._worker.line.connect(self.log.appendPlainText)
            self._worker.done.connect(self._done)
            self._worker.start()

        def _done(self, r):
            for b in (self.b_status, self.b_install, self.b_remove):
                b.setEnabled(True)
            if isinstance(r, tuple) and r and r[0] == "ERROR":
                self.log.appendPlainText(f"ERROR: {r[1]}")
                QMessageBox.critical(self, "USB keyboard", str(r[1]))
            elif isinstance(r, dict):
                for k, v in r.items():
                    self.log.appendPlainText(f"  {k}: {v}")

        def _status(self):
            t = self._target()
            if t:
                hdd = self.rb_hdd.isChecked()
                self._run(lambda log: hdd_status(t) if hdd else card_status(t))

        def _confirm(self, verb, t):
            what = ("The image is modified in place; everything overwritten is "
                    "saved to an undo journal first." if self.rb_hdd.isChecked()
                    else "A .bak copy of the card is made first.")
            return QMessageBox.question(
                self, "USB keyboard", f"{verb} the keyboard patch in\n{t}?\n\n"
                f"{what} Close PCSX2 first.") == QMessageBox.Yes

        def _install(self):
            t = self._target()
            if not t or not self._confirm("Install", t):
                return
            if self.rb_hdd.isChecked():
                src = self.ed_card.text().strip() or None
                self._run(lambda log: hdd_install(t, src, log=log))
            else:
                self._run(lambda log: card_install(t, log=log))

        def _remove(self):
            t = self._target()
            if not t or not self._confirm("Remove", t):
                return
            hdd = self.rb_hdd.isChecked()
            self._run(lambda log: hdd_remove(t, log=log) if hdd else card_remove(t, log=log))

        def closeEvent(self, ev):
            if self._worker is not None and self._worker.isRunning():
                self._worker.wait(5000)
            super().closeEvent(ev)


if __name__ == "__main__":
    sys.exit(_main())

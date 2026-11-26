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

Before installing, the game executable ``taiko`` is decrypted (needs the
``T14LOAD`` key material: taken from the card image, ``--t14load``, or
``T14LOAD.bin`` as for taiko_exe) and checked: KBD2EE rewrites seven fixed
16-byte code blocks and uses a fixed memory cave, so on any other ``taiko``
build it would overwrite live code. A mismatch refuses the install.

Keys: 1P D F J K, 2P Z X C V, COIN 5, SERVICE 9, TEST F1 (toggle), SELECT
up/down/Enter. The keyboard must be unplugged and replugged once after boot.

CLI::

    python -m taiko2x6.exe.usb_keyboard status  TK14.img
    python -m taiko2x6.exe.usb_keyboard install TK14.img --card NM00057.bin
    python -m taiko2x6.exe.usb_keyboard remove  TK14.img
    python -m taiko2x6.exe.usb_keyboard install NM00057.ps2 --img TK14.img  # card
    python -m taiko2x6.exe.usb_keyboard check   TK14.img --t14load T14LOAD.bin
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
    "card_remove", "KEYMAP", "check_taiko", "check_hdd_taiko",
]

MODULE = "KBD2EE"
# Two builds of the module (same source, gen_blob.py with/without --kbdonly):
#   full    keyboard + dummy JVS: boots and plays with NO I/O board attached.
#           The exact binary validated on hardware.
#   kbdonly keyboard only, for a machine that still has its real I/O board:
#           none of the dummy-JVS hooks (node-count gate, enumeration exit,
#           the four JVS wait loops) is applied, so the board keeps working
#           and the keyboard adds to it. Built with the toolchain that
#           reproduces the full build bit for bit; not yet tested on hardware.
VARIANTS = {
    "full": ("KBD2EE.irx", "34F1CC379FB27157"),
    "kbdonly": ("KBD2EE_kbdonly.irx", "719C4379EF7E31CA"),
}
IRX_SHA256_PREFIX = VARIANTS["full"][1]          # the hardware-validated build
HDD_NAMES = ("IRXARC.BIN;1", "IRXARC.BIN")      # see module docstring
# T14LOAD module-name list: one free slot + a zero run used as a string cave
PTR_OFF, STR_OFF, STR_VA = 0x32664, 0x34AC4, 0x01033AC4

# The seven 16-byte blocks KBD2EE DMAs over (VA -> the stock t14jp1400 words).
# KBD2EE carries the three untouched words of each block along, so the WHOLE
# block must match, not just the hooked instruction.
HOOK_BLOCKS = {
    0x00186A20: (0xDFB10008, 0xDFBF0010, 0x080629D0, 0x27BD0020),  # j 0x18A740 -> shim
    0x00187070: (0x24100007, 0x1000FFBD, 0xADE0B188, 0x3C0E003D),  # li $s0,7 -> 10
    0x001869D0: (0x00000000, 0x3C0F003D, 0x91EEE780, 0x51C00014),  # node-count gate
    0x00188A50: (0x0000202D, 0x0C06338A, 0x00000000, 0x962F0042),  # jvswait
    0x00188AC0: (0x0000202D, 0x0C06338A, 0x00000000, 0x962F0042),  # jvswait
    0x0018BAF0: (0x26510300, 0x0C06339A, 0x0000202D, 0x0C06338A),  # jvswait
    0x0018BB50: (0xA62F0010, 0x0C06339A, 0x0000202D, 0x0C06338A),  # jvswait
}
# mailbox 0x300000, shim code 0x300020 (2184 B), shim state 0x301000..0x301044
CAVE = (0x00300000, 0x00301100)

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


def bundled_irx(variant: str = "full") -> bytes:
    """A bundled KBD2EE build (see VARIANTS), checked against its hash."""
    if variant not in VARIANTS:
        raise KeyboardPatchError(f"unknown build {variant!r} (full / kbdonly)")
    name, sha = VARIANTS[variant]
    p = Path(__file__).resolve().parent / "data" / name
    if not p.is_file():
        raise KeyboardPatchError(f"bundled module missing: {p}")
    data = p.read_bytes()
    if _sha(data) != sha:
        raise KeyboardPatchError(f"{p.name} is not the expected {variant} build "
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
        for variant in VARIANTS:
            try:
                ref = bundled_irx(variant)
            except KeyboardPatchError:
                continue
            if stored[:len(ref)] == ref and not any(stored[len(ref):]):
                info["build"] = variant
        info["matches_bundled"] = "build" in info
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
#  taiko compatibility check
# --------------------------------------------------------------------------- #
def _va_reader(dec: bytes):
    """VA -> file offset through the ELF PT_LOAD headers."""
    if dec[:4] != b"\x7fELF":
        raise KeyboardPatchError("taiko is not a decrypted ELF")
    phoff, = struct.unpack_from("<I", dec, 0x1C)
    phnum, = struct.unpack_from("<H", dec, 0x2C)
    loads = []
    for i in range(phnum):
        typ, off, va, _pa, fsz, msz = struct.unpack_from("<6I", dec, phoff + 32 * i)
        if typ == 1:
            loads.append((va, off, fsz, msz))

    def read(va, n):
        for base, off, fsz, msz in loads:
            if base <= va and va + n <= base + msz:
                rel = va - base
                file_part = dec[off + rel:off + min(rel + n, fsz)] if rel < fsz else b""
                return file_part + b"\0" * (n - len(file_part))     # .bss is zero
        raise KeyboardPatchError(f"VA {va:#x} is outside taiko's loaded image")
    return read


def check_taiko(dec: bytes) -> list[str]:
    """Problems that make this decrypted ``taiko`` unsafe for KBD2EE
    (empty list = compatible)."""
    read = _va_reader(dec)
    problems = []
    for va, words in HOOK_BLOCKS.items():
        got = struct.unpack("<4I", read(va, 16))
        if got != words:
            bad = [f"{va + 4 * i:#x}: {g:#010x} (expected {w:#010x})"
                   for i, (g, w) in enumerate(zip(got, words)) if g != w]
            problems.append("hook block " + "; ".join(bad))
    cave = read(CAVE[0], CAVE[1] - CAVE[0])
    if any(cave):
        first = next(i for i, b in enumerate(cave) if b)
        problems.append(f"memory cave is not empty (data at {CAVE[0] + first:#x})")
    return problems


def _t14load_path(t14load=None, card=None) -> tuple[str, object]:
    """A T14LOAD file path for taiko_exe: explicit > the card's > T14LOAD.bin.
    Returns (path, cleanup) - cleanup deletes a temp copy taken from a card."""
    from taiko2x6.exe import taiko_exe
    if t14load:
        return taiko_exe.find_t14load(t14load), None
    if card and Path(card).is_file() and _is_card(card):
        import os
        import tempfile
        t = unpatch_t14load(read_card_files(card)[1])
        fd, tmp = tempfile.mkstemp(prefix="T14LOAD.", suffix=".bin")
        with os.fdopen(fd, "wb") as f:
            f.write(t)
        return tmp, lambda: os.unlink(tmp)
    try:
        return taiko_exe.find_t14load(), None
    except taiko_exe.TaikoExeError:
        raise KeyboardPatchError(
            "cannot check taiko: need the T14LOAD key material - pass the dongle "
            "card image, --t14load PATH, or put T14LOAD.bin next to the toolkit "
            "(or skip the check with --no-check)") from None


def check_hdd_taiko(img, partition: str | None = None, t14load=None, card=None,
                    log=None) -> list[str]:
    """Decrypt ``taiko`` from a T14 HDD image and run :func:`check_taiko`."""
    from taiko2x6.exe import taiko_exe
    from taiko2x6.ps2 import ps2hdd
    path, cleanup = _t14load_path(t14load, card)
    try:
        h = ps2hdd.Ps2Hdd(str(img))
        try:
            part = partition or find_game_partition(h)
            enc = h.pfs_read(part, "/taiko")
        finally:
            h.close()
        try:
            dec = taiko_exe.decrypt_taiko(enc, path)
        except taiko_exe.TaikoExeError as exc:
            raise KeyboardPatchError(f"cannot decrypt taiko: {exc}") from None
    finally:
        if cleanup:
            cleanup()
    problems = check_taiko(dec)
    if log:
        log("taiko check: " + ("OK - all 7 hook blocks and the memory cave match"
                               if not problems else "FAILED"))
        for p in problems:
            log("  " + p)
    return problems


def _require_compatible(problems):
    if problems:
        raise KeyboardPatchError(
            "this taiko does not match the build KBD2EE was made for; installing "
            "would overwrite live game code:\n  " + "\n  ".join(problems))


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
                log=print, check: bool = True, t14load=None) -> dict:
    """Write stock IRXARC + KBD2EE into the game partition of a T14 image.

    ``source`` (card image or IRXARC.BIN) is only needed the first time; a
    re-install reuses the archive already on the HDD. Undo-journaled
    (ps2hdd.write_session): an error rolls the image back. ``check`` first
    verifies the image's ``taiko`` (see :func:`check_taiko`)."""
    from taiko2x6.ps2 import ps2hdd
    st = hdd_status(img, partition)
    part = st["partition"]
    if check:
        _require_compatible(check_hdd_taiko(img, part, t14load, source, log))
    else:
        log("taiko check SKIPPED")
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


def card_install(card, irx=None, log=print, img=None, partition=None) -> dict:
    """Patch the card. ``img`` (the T14 HDD image the card boots) lets the
    install check that image's ``taiko`` first, using the card's T14LOAD."""
    if img:
        _require_compatible(check_hdd_taiko(img, partition, None, card, log))
    else:
        log("taiko not checked (no HDD image given)")
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
    for name in ("status", "install", "remove", "check"):
        sp = sub.add_parser(name)
        sp.add_argument("target", help="TK14 HDD .img, or dongle card .ps2/.bin")
        sp.add_argument("--partition", help="HDD partition (default: the one with `taiko`)")
        if name in ("install", "check"):
            sp.add_argument("--card", help="HDD target: card image (or IRXARC.BIN) "
                                           "with the stock module archive + T14LOAD")
            sp.add_argument("--t14load", help="T14LOAD key file for the taiko check")
            sp.add_argument("--img", help="card target: the HDD image to check")
        if name == "install":
            sp.add_argument("--no-check", action="store_true",
                            help="skip the taiko compatibility check")
            sp.add_argument("--kbdonly", action="store_true",
                            help="keyboard only, for a machine that keeps its "
                                 "real I/O board (default: keyboard + dummy JVS)")
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
        elif a.cmd == "check":
            img = a.img if card else a.target
            if not img:
                raise KeyboardPatchError("card target: pass --img TK14.img to check")
            problems = check_hdd_taiko(img, a.partition, a.t14load,
                                       a.target if card else a.card, log=print)
            return 1 if problems else 0
        elif a.cmd == "install":
            irx = bundled_irx("kbdonly" if a.kbdonly else "full")
            if card:
                if a.no_check:
                    card_install(a.target, irx)
                else:
                    card_install(a.target, irx, img=a.img, partition=a.partition)
            else:
                hdd_install(a.target, a.card, a.partition, irx=irx,
                            check=not a.no_check, t14load=a.t14load)
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
    from taiko2x6.app.i18n import tr

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
            self.setWindowTitle(tr("USB keyboard patch (Taiko 14)"))
            self.resize(780, 700)
            self._worker = None
            self._st = None             # last state read from the target
            self._again = False
            lay = QVBoxLayout(self)

            intro = QLabel(tr(
                "Lets a USB keyboard play Taiko 14: the KBD2EE module is added "
                "to the game's module archive (IRXARC.BIN). Pick where, read "
                "the current state, then install or remove."))
            intro.setWordWrap(True)
            lay.addWidget(intro)

            mode = QGroupBox(tr("Where to install"))
            ml = QVBoxLayout(mode)
            self.rb_hdd = QRadioButton(tr(
                "HDD image — write IRXARC.BIN into the game partition "
                "(card untouched; not yet tested on hardware)"))
            self.rb_card = QRadioButton(tr(
                "Dongle card image — patch IRXARC.BIN + T14LOAD on the card "
                "(the hardware-tested method)"))
            self.rb_hdd.setChecked(True)
            ml.addWidget(self.rb_hdd); ml.addWidget(self.rb_card)
            lay.addWidget(mode)

            build = QGroupBox(tr("Which build"))
            bl = QVBoxLayout(build)
            self.rb_full = QRadioButton(tr(
                "Keyboard + dummy JVS — play with NO I/O board attached "
                "(the hardware-tested build)"))
            self.rb_kbdonly = QRadioButton(tr(
                "Keyboard only — the machine keeps its real I/O board, the "
                "keyboard works alongside it (not yet tested on hardware)"))
            self.rb_full.setChecked(True)
            bl.addWidget(self.rb_full); bl.addWidget(self.rb_kbdonly)
            lay.addWidget(build)

            grid = QGridLayout()
            self.ed_target = QLineEdit(default_img or appconfig.last_existing("hddimg"))
            b1 = QPushButton(tr("Browse…")); b1.clicked.connect(self._pick_target)
            self.ed_card = QLineEdit(appconfig.last_existing("ps2mc"))
            self.ed_card.setPlaceholderText(tr(
                "stock module archive: dongle card image or IRXARC.BIN "
                "(first HDD install only)"))
            b2 = QPushButton(tr("Browse…")); b2.clicked.connect(self._pick_card)
            self.lb_card = QLabel(tr("Card / IRXARC.BIN:"))
            self.ed_img = QLineEdit(appconfig.last_existing("hddimg"))
            self.ed_img.setPlaceholderText(tr(
                "card mode: the T14 HDD image this card boots (to check taiko)"))
            b3 = QPushButton(tr("Browse…")); b3.clicked.connect(self._pick_img)
            self.lb_img = QLabel(tr("HDD image:"))
            self.ed_t14 = QLineEdit(appconfig.last_existing("t14load"))
            self.ed_t14.setPlaceholderText(tr(
                "optional: T14LOAD key file (otherwise taken from the card / T14LOAD.bin)"))
            b4 = QPushButton(tr("Browse…")); b4.clicked.connect(self._pick_t14)
            grid.addWidget(QLabel(tr("Target:")), 0, 0); grid.addWidget(self.ed_target, 0, 1)
            grid.addWidget(b1, 0, 2)
            grid.addWidget(self.lb_card, 1, 0); grid.addWidget(self.ed_card, 1, 1)
            grid.addWidget(b2, 1, 2)
            grid.addWidget(self.lb_img, 2, 0); grid.addWidget(self.ed_img, 2, 1)
            grid.addWidget(b3, 2, 2)
            grid.addWidget(QLabel(tr("T14LOAD:")), 3, 0); grid.addWidget(self.ed_t14, 3, 1)
            grid.addWidget(b4, 3, 2)
            lay.addLayout(grid)

            self.lb_state = QLabel()
            self.lb_state.setWordWrap(True)
            self.lb_state.setStyleSheet("font-weight:bold;")
            lay.addWidget(self.lb_state)

            keys = QLabel("<br>".join(f"<b>{k}</b>: {tr(v)}" for v, k in KEYMAP)
                          + "<br><i>" + tr(
                              "Unplug and replug the keyboard once after boot. "
                              "Close PCSX2 first — it locks the image.") + "</i>")
            keys.setWordWrap(True)
            lay.addWidget(keys)

            row = QHBoxLayout()
            self.b_status = QPushButton(tr("Read current state + check"))
            self.b_status.clicked.connect(self._status)
            self.b_install = QPushButton(tr("Install")); self.b_install.clicked.connect(self._install)
            self.b_remove = QPushButton(tr("Remove")); self.b_remove.clicked.connect(self._remove)
            for b in (self.b_status, self.b_install, self.b_remove):
                row.addWidget(b)
            row.addStretch(1)
            lay.addLayout(row)
            self.log = QPlainTextEdit(); self.log.setReadOnly(True)
            lay.addWidget(self.log, 1)

            self.rb_card.toggled.connect(self._mode_changed)
            self.rb_kbdonly.toggled.connect(self._update_buttons)
            self.ed_target.editingFinished.connect(self._refresh)
            self._mode_changed()

        # ---- state --------------------------------------------------------
        def _busy(self):
            return self._worker is not None

        def _mode_changed(self, *_):
            hdd = self.rb_hdd.isChecked()
            self.ed_card.setEnabled(hdd); self.lb_card.setEnabled(hdd)
            self.ed_img.setEnabled(not hdd); self.lb_img.setEnabled(not hdd)
            self._refresh()

        def _wanted_build(self):
            return "kbdonly" if self.rb_kbdonly.isChecked() else "full"

        def _needs_install(self):
            st = self._st
            if not st:
                return False
            if not st.get("installed"):
                return True
            if st.get("build") != self._wanted_build():
                return True
            if self.rb_hdd.isChecked():
                return len(st.get("files", [])) < len(HDD_NAMES)
            return not st.get("t14load")

        def _describe(self):
            st = self._st
            if not st:
                return tr("State unknown — choose a target to read it.")
            if not st.get("installed"):
                return tr("Not installed.")
            build = {"full": tr("keyboard + dummy JVS"),
                     "kbdonly": tr("keyboard only")}.get(
                         st.get("build"), tr("a different build"))
            msg = tr("Installed: {build}.").format(build=build)
            if self._needs_install():
                msg += " " + tr("Install again to switch to the chosen build or "
                                "finish a partial install.")
            return msg

        def _update_buttons(self, *_):
            idle = not self._busy()
            self.b_status.setEnabled(idle)
            self.b_install.setEnabled(idle and self._needs_install())
            self.b_remove.setEnabled(idle and bool(self._st and self._st.get("installed")))
            self.lb_state.setText(self._describe())

        def _refresh(self):
            """Read the target's current state (read-only) in the background."""
            t = self.ed_target.text().strip()
            if self._busy():
                self._again = True
                return
            self._st = None
            self._update_buttons()
            if not t or not Path(t).is_file():
                return
            hdd = self.rb_hdd.isChecked()

            def job(log):
                return ("STATE", hdd_status(t) if hdd else card_status(t))
            self._run(job, quiet=True)

        # ---- pickers ------------------------------------------------------
        def _pick_img(self):
            p = appconfig.pick_open(self, "hddimg", tr("T14 HDD image"),
                                    tr("HDD image") + " (*.img);;" + tr("All files") + " (*)")
            if p:
                self.ed_img.setText(p)

        def _pick_t14(self):
            p = appconfig.pick_open(self, "t14load", tr("T14LOAD key file"),
                                    "T14LOAD (T14LOAD*);;" + tr("All files") + " (*)")
            if p:
                self.ed_t14.setText(p)

        def _opt(self, ed):
            return ed.text().strip() or None

        def _pick_target(self):
            if self.rb_hdd.isChecked():
                p = appconfig.pick_open(self, "hddimg", tr("T14 HDD image"),
                                        tr("HDD image") + " (*.img);;" + tr("All files") + " (*)")
            else:
                p = appconfig.pick_open(self, "ps2mc", tr("Dongle card image"),
                                        tr("Card image") + " (*.ps2 *.bin);;" + tr("All files") + " (*)")
            if p:
                self.ed_target.setText(p)
                self._refresh()

        def _pick_card(self):
            p = appconfig.pick_open(self, "ps2mc", tr("Card image or IRXARC.BIN"),
                                    tr("Card / IRXARC") + " (*.ps2 *.bin *.BIN);;"
                                    + tr("All files") + " (*)")
            if p:
                self.ed_card.setText(p)

        def _target(self):
            t = self.ed_target.text().strip()
            if not t or not Path(t).is_file():
                QMessageBox.warning(self, tr("USB keyboard"), tr("Choose a target file first."))
                return None
            return t

        def _remember(self):
            """Keep the paths the user actually used for next time."""
            hdd = self.rb_hdd.isChecked()
            for key, ed in (("hddimg" if hdd else "ps2mc", self.ed_target),
                            ("ps2mc", self.ed_card if hdd else None),
                            ("hddimg", None if hdd else self.ed_img),
                            ("t14load", self.ed_t14)):
                if ed is not None and ed.text().strip():
                    appconfig.remember(key, ed.text().strip())

        # ---- worker -------------------------------------------------------
        def _run(self, fn, quiet=False):
            self._worker = _Worker(fn)
            self._update_buttons()
            self._worker.line.connect(self.log.appendPlainText)
            self._worker.done.connect(lambda r: self._done(r, quiet))
            self._worker.finished.connect(self._finished)
            self._worker.start()

        def _finished(self):
            w, self._worker = self._worker, None
            if w is not None:
                w.deleteLater()
            self._update_buttons()
            if self._again:
                self._again = False
                self._refresh()

        def _done(self, r, quiet=False):
            if isinstance(r, tuple) and r and r[0] == "ERROR":
                from taiko2x6.exe.taiko_exe import friendly_error
                msg = friendly_error(r[1])
                self._st = None
                if quiet:
                    self.log.appendPlainText(f"{tr('Could not read the target')}: {r[1]}")
                else:
                    self.log.appendPlainText(f"ERROR: {r[1]}")
                    QMessageBox.critical(self, tr("USB keyboard"), msg)
                return
            if isinstance(r, tuple) and r and r[0] == "STATE":
                self._st = r[1]
                if quiet:
                    self.log.appendPlainText(self._describe())
                return
            if isinstance(r, dict):
                for k, v in r.items():
                    self.log.appendPlainText(f"  {k}: {v}")
            self._again = True          # re-read the state after a write

        # ---- actions ------------------------------------------------------
        def _status(self):
            t = self._target()
            if not t:
                return
            self._remember()
            hdd = self.rb_hdd.isChecked()
            src, img, t14 = self._opt(self.ed_card), self._opt(self.ed_img), self._opt(self.ed_t14)

            def job(log):
                st = hdd_status(t) if hdd else card_status(t)
                for k, v in st.items():
                    log(f"  {k}: {v}")
                hdd_img = t if hdd else img
                if hdd_img:
                    try:
                        check_hdd_taiko(hdd_img, None, t14, src if hdd else t, log)
                    except KeyboardPatchError as exc:
                        log(tr("taiko check not run: {err}").format(err=exc))
                else:
                    log(tr("taiko not checked (choose the HDD image)"))
                return ("STATE", st)
            self._run(job)

        def _confirm(self, verb, t):
            what = (tr("The image is modified in place; everything overwritten is "
                       "saved to an undo journal first.") if self.rb_hdd.isChecked()
                    else tr("A .bak copy of the card is made first."))
            return QMessageBox.question(
                self, tr("USB keyboard"), verb.format(t=t) + "\n\n" + what + " "
                + tr("Close PCSX2 first.")) == QMessageBox.Yes

        def _install(self):
            t = self._target()
            if not t or not self._confirm(tr("Install the keyboard patch in\n{t}?"), t):
                return
            self._remember()
            t14 = self._opt(self.ed_t14)
            irx = bundled_irx(self._wanted_build())
            if self.rb_hdd.isChecked():
                src = self._opt(self.ed_card)
                self._run(lambda log: hdd_install(t, src, irx=irx, log=log, t14load=t14))
            else:
                img = self._opt(self.ed_img)
                if not img and QMessageBox.question(
                        self, tr("USB keyboard"),
                        tr("No HDD image chosen, so the game executable cannot be "
                           "checked. Install on the card anyway?")) != QMessageBox.Yes:
                    return
                self._run(lambda log: card_install(t, irx, log=log, img=img))

        def _remove(self):
            t = self._target()
            if not t or not self._confirm(tr("Remove the keyboard patch from\n{t}?"), t):
                return
            self._remember()
            hdd = self.rb_hdd.isChecked()
            self._run(lambda log: hdd_remove(t, log=log) if hdd else card_remove(t, log=log))

        def closeEvent(self, ev):
            if self._worker is not None and self._worker.isRunning():
                self._worker.wait(5000)
            super().closeEvent(ev)


if __name__ == "__main__":
    sys.exit(_main())

import struct
import zlib

import pytest

from taiko2x6.exe import usb_keyboard as K
from taiko2x6.ps2 import ps2hdd


def _fake_irxarc(names=("sio2man.irx", "usbd.irx", "pfs.irx")):
    blob = bytearray(0x40)
    blob[0] = len(names)
    for i, n in enumerate(names):
        blob += K._make_entry(n, bytes([i + 1]) * (100 + 7 * i))
    return K._encode(blob)


def _fake_t14load():
    return bytes(K.STR_OFF + 0x100)


def test_bundled_irx_is_the_validated_build():
    assert len(K.bundled_irx()) == 6657


def test_irxarc_patch_unpatch_roundtrip():
    stock = _fake_irxarc()
    patched = K.patch_irxarc(stock)
    assert K.irxarc_modules(patched)[-1] == "KBD2EE"
    again = K.patch_irxarc(patched)                 # never stacks up
    assert K.irxarc_modules(again).count("KBD2EE") == 1
    assert K.unpatch_irxarc(again) == K.unpatch_irxarc(patched)
    assert zlib.decompress(K._scramble(K.unpatch_irxarc(patched))) == \
        zlib.decompress(K._scramble(stock))
    assert K._irxarc_info(patched)["matches_bundled"] is True


def test_irxarc_without_usbd_is_refused():
    with pytest.raises(K.KeyboardPatchError):
        K.patch_irxarc(_fake_irxarc(("sio2man.irx", "pfs.irx")))


def test_t14load_patch_unpatch():
    t = _fake_t14load()
    p = K.patch_t14load(t)
    assert K.t14load_has_kbd(p) and K.patch_t14load(p) == p
    assert K.unpatch_t14load(p) == t
    bad = bytearray(t)
    struct.pack_into("<I", bad, K.PTR_OFF, 0x1234)
    with pytest.raises(K.KeyboardPatchError):
        K.patch_t14load(bytes(bad))


def test_pfs_create_and_delete(pfs_image):
    img, part, _f = pfs_image
    h = ps2hdd.Ps2Hdd(str(img))
    bits0 = bytes(h._get_bitmap(part, 0).bits)
    h.close()
    big = bytes((i * 3) & 0xFF for i in range(70_000))      # needs a grow
    with ps2hdd.write_session(str(img)) as w:
        w.pfs_create(part, "/new;1", big)
        w.pfs_create(part, "/empty")
        with pytest.raises(FileExistsError):
            w.pfs_create(part, "/new;1", b"x")
    h = ps2hdd.Ps2Hdd(str(img))
    names = {e["name"]: e["size"] for e in h.pfs_listdir(part, "/")}
    assert names == {"f.bin": names["f.bin"], "new;1": len(big), "empty": 0}
    assert h.pfs_read(part, "/new;1") == big
    h.close()
    with ps2hdd.write_session(str(img)) as w:
        w.pfs_delete(part, "/new;1")
        w.pfs_delete(part, "/empty")
    h = ps2hdd.Ps2Hdd(str(img))
    assert [e["name"] for e in h.pfs_listdir(part, "/")] == ["f.bin"]
    assert bytes(h._get_bitmap(part, 0).bits) == bits0
    h.close()


def test_hdd_install_remove(pfs_image):
    img, part, _f = pfs_image
    with ps2hdd.write_session(str(img)) as w:          # make it look like T14
        w.pfs_create(part, "/taiko", b"\x7fELF" + bytes(60))
    src = img.parent / "IRXARC.BIN"
    src.write_bytes(_fake_irxarc())
    with pytest.raises(K.KeyboardPatchError):
        K.hdd_install(img, log=lambda s: None, check=False)          # first install needs it
    K.hdd_install(img, src, log=lambda s: None, check=False)
    st = K.hdd_status(img)
    assert st["installed"] and st["files"] == list(K.HDD_NAMES) and st["count"] == 1
    K.hdd_install(img, log=lambda s: None, check=False)              # reinstall from the HDD copy
    assert K.hdd_status(img)["count"] == 1
    K.hdd_remove(img, log=lambda s: None)
    assert K.hdd_status(img) == {"partition": part, "files": [], "installed": False}
    assert K._main(["status", str(img)]) == 0


# --------------------------------------------------------------------------- #
#  taiko compatibility check
# --------------------------------------------------------------------------- #
def _fake_taiko(mutate=None):
    """Minimal ELF32 with one PT_LOAD at 0x100000 holding the stock hook
    blocks; the cave lies in its zero-filled .bss tail."""
    base, off = 0x100000, 0x1000
    fsz = 0x190000                       # covers the hook blocks
    msz = 0x210000                       # .bss reaches past the cave
    img = bytearray(off + fsz)
    img[:4] = b"\x7fELF"
    struct.pack_into("<I", img, 0x1C, 0x34)
    struct.pack_into("<H", img, 0x2C, 1)
    struct.pack_into("<8I", img, 0x34, 1, off, base, base, fsz, msz, 7, 0x10)
    for va, words in K.HOOK_BLOCKS.items():
        struct.pack_into("<4I", img, off + va - base, *words)
    if mutate:
        mutate(img, lambda va: off + va - base)
    return bytes(img)


def test_hook_blocks_match_bundled_irx():
    """Each stock block must appear in KBD2EE.irx with exactly one word
    replaced - i.e. the table is the one this IRX was built against."""
    irx = K.bundled_irx()
    for va, words in K.HOOK_BLOCKS.items():
        found = False
        for pos in range(0, len(irx) - 16, 4):
            got = struct.unpack_from("<4I", irx, pos)
            if sum(g != w for g, w in zip(got, words)) == 1 and \
                    sum(g == w for g, w in zip(got, words)) == 3:
                found = True
                break
        assert found, hex(va)


def test_check_taiko_accepts_stock():
    assert K.check_taiko(_fake_taiko()) == []


def test_check_taiko_rejects_changed_hook():
    def m(img, at):
        struct.pack_into("<I", img, at(0x00186A28), 0x08000000)
    probs = K.check_taiko(_fake_taiko(m))
    assert len(probs) == 1 and "0x186a28" in probs[0]
    with pytest.raises(K.KeyboardPatchError):
        K._require_compatible(probs)


def test_check_taiko_rejects_used_cave():
    def m(img, at):                       # extend file data over the cave
        img.extend(bytes(0x80000))
        struct.pack_into("<8I", img, 0x34, 1, 0x1000, 0x100000, 0x100000,
                         len(img) - 0x1000, 0x210000, 7, 0x10)
        img[at(0x300800)] = 1
    probs = K.check_taiko(_fake_taiko(m))
    assert probs and "cave" in probs[0]


def test_hdd_install_requires_key_for_check(pfs_image, monkeypatch):
    img, part, _f = pfs_image
    with ps2hdd.write_session(str(img)) as w:
        w.pfs_create(part, "/taiko", b"\x7fELF" + bytes(60))
    src = img.parent / "IRXARC.BIN"
    src.write_bytes(_fake_irxarc())
    from taiko2x6.exe import taiko_exe
    monkeypatch.setattr(taiko_exe, "find_t14load", lambda hint=None: (_ for _ in ()).throw(
        taiko_exe.TaikoExeError("missing")) if hint is None else hint)
    with pytest.raises(K.KeyboardPatchError, match="T14LOAD"):
        K.hdd_install(img, src, log=lambda s: None)          # check is on by default
    assert K.hdd_status(img)["installed"] is False             # nothing written

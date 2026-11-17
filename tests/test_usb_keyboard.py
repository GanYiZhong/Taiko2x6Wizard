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
        K.hdd_install(img, log=lambda s: None)          # first install needs it
    K.hdd_install(img, src, log=lambda s: None)
    st = K.hdd_status(img)
    assert st["installed"] and st["files"] == list(K.HDD_NAMES) and st["count"] == 1
    K.hdd_install(img, log=lambda s: None)              # reinstall from the HDD copy
    assert K.hdd_status(img)["count"] == 1
    K.hdd_remove(img, log=lambda s: None)
    assert K.hdd_status(img) == {"partition": part, "files": [], "installed": False}
    assert K._main(["status", str(img)]) == 0

"""TIM2 encoder: 4/8-bit indexed (16/24/32-bit CLUT) and direct colour."""
import struct

import numpy as np
import pytest

from taiko2x6.graphics import tim2


def make_tim2(w, h, image_type, colors=0, clut_type=3, extra=0):
    bpp_px = {1: 2, 2: 3, 3: 4}.get(image_type)
    if bpp_px:
        img = bytes(w * h * bpp_px)
    else:
        img = bytes((w * h + 1) // 2 if image_type == 4 else w * h)
    img += bytes(extra)
    cb = {1: 2, 2: 3, 3: 4}[clut_type & 0x3F] if image_type >= 4 else 0
    clut = bytes(colors * cb)
    head = b"TIM2" + bytes([4, 0]) + struct.pack("<H", 1) + bytes(8)
    pic = bytearray(0x30)
    struct.pack_into("<III", pic, 0, 0x30 + len(img) + len(clut), len(clut), len(img))
    struct.pack_into("<HH", pic, 12, 0x30, colors)
    pic[0x12] = clut_type
    pic[0x13] = image_type
    struct.pack_into("<HH", pic, 0x14, w, h)
    pic[0x18:0x30] = bytes(range(0x18, 0x30))      # recognisable GS regs
    return head + bytes(pic) + img + clut


def img_with(n, w=16, h=16, alpha=255):
    rng = np.random.RandomState(1)
    cols = np.unique(rng.randint(0, 32, (n * 2, 3)).astype(np.uint8) * 8 + 8, axis=0)
    cols = cols[rng.permutation(len(cols))[:n]]
    assert len(cols) == n
    out = np.zeros((h, w, 4), np.uint8)
    flat = out.reshape(-1, 4)
    flat[:, :3] = cols[np.arange(w * h) % n]
    flat[:, 3] = alpha
    return out


def test_indexed8_32bit_round_trip_and_header_kept():
    t = make_tim2(32, 32, 5, 256)
    src = img_with(200, 32, 32)
    src[0, 0] = (0, 0, 0, 0)                       # one transparent pixel
    out = tim2.encode_rgba_into_template(t, src)
    assert len(out) == len(t)
    assert out[:0x40] == t[:0x40]                  # file + picture header kept
    assert (tim2.decode_tim2(out)[0][2] == src).all()


def test_indexed8_palette_swizzle_matches_decoder():
    t = make_tim2(16, 16, 5, 256)
    src = np.zeros((16, 16, 4), np.uint8)          # 256 distinct colours
    src[..., 0] = np.arange(256, dtype=np.uint8).reshape(16, 16)
    src[..., 1] = 255 - src[..., 0]
    src[..., 3] = 255
    out = tim2.encode_rgba_into_template(t, src)
    assert (tim2.decode_tim2(out)[0][2] == src).all()
    lay = tim2.first_picture_layout(out)
    disk = np.frombuffer(out, np.uint8, 1024, lay["clut_start"]).reshape(256, 4)
    idx = np.frombuffer(out, np.uint8, 256, lay["img_start"])

    def slot(i):                                   # 8..15 <-> 16..23 per 32
        return i + 8 if (i & 0x18) == 0x08 else i - 8 if (i & 0x18) == 0x10 else i
    assert slot(8) == 16 and slot(16) == 8 and slot(7) == 7 and slot(24) == 24
    flat = src.reshape(-1, 4)
    for p in range(256):
        assert tuple(disk[slot(int(idx[p])), :3]) == tuple(flat[p, :3])
    assert not np.array_equal(disk[8], disk[16])


def test_indexed8_linear_csm2_not_swizzled():
    t = make_tim2(16, 16, 5, 256, clut_type=0x83)
    src = img_with(150)
    out = tim2.encode_rgba_into_template(t, src)
    assert (tim2.decode_tim2(out)[0][2] == src).all()
    lay = tim2.first_picture_layout(out)
    idx = np.frombuffer(out, np.uint8, 256, lay["img_start"])
    disk = np.frombuffer(out, np.uint8, 1024, lay["clut_start"]).reshape(256, 4)
    assert tuple(disk[idx[5], :3]) == tuple(src.reshape(-1, 4)[5, :3])


def test_alpha_scale_ps2():
    t = make_tim2(16, 16, 5, 256)
    src = img_with(10)
    src[8:, :, :] = 0                              # lower half transparent
    out = tim2.encode_rgba_into_template(t, src)
    lay = tim2.first_picture_layout(out)
    disk = np.frombuffer(out, np.uint8, 1024, lay["clut_start"]).reshape(256, 4)
    assert set(np.unique(disk[:11, 3])) == {0, 0x80}
    assert (tim2.decode_tim2(out)[0][2] == src).all()
    t3 = make_tim2(16, 16, 3)
    o3 = tim2.encode_rgba_into_template(t3, src)
    lay3 = tim2.first_picture_layout(o3)
    px = np.frombuffer(o3, np.uint8, 1024, lay3["img_start"]).reshape(-1, 4)
    assert set(np.unique(px[:, 3])) == {0, 0x80}
    assert (tim2.decode_tim2(o3)[0][2] == src).all()


def _exact5551(src):
    v = (src[..., :3].astype(int) * 31 + 127) // 255
    out = src.copy()
    out[..., :3] = (v * 255 // 31).astype(np.uint8)
    return out


@pytest.mark.parametrize("clut_type", [1, 2, 3])
@pytest.mark.parametrize("itype,colors,n", [(4, 16, 12), (5, 256, 100)])
def test_indexed_clut_formats(itype, colors, n, clut_type):
    t = make_tim2(16, 16, itype, colors, clut_type)
    src = img_with(n)
    if clut_type == 1:
        src = _exact5551(src)
    out = tim2.encode_rgba_into_template(t, src)
    assert len(out) == len(t) and out[:0x40] == t[:0x40]
    assert (tim2.decode_tim2(out)[0][2] == src).all()


def test_direct_formats_round_trip():
    src = img_with(40)
    src[3, 3] = (0, 0, 0, 0)
    for itype in (2, 3):
        t = make_tim2(16, 16, itype)
        out = tim2.encode_rgba_into_template(t, src)
        want = src.copy()
        if itype == 2:
            want[..., 3] = 255
        assert (tim2.decode_tim2(out)[0][2] == want).all()
        assert out[:0x40] == t[:0x40]
    s16 = _exact5551(src)
    s16[3, 3] = (0, 0, 0, 0)
    t = make_tim2(16, 16, 1)
    out = tim2.encode_rgba_into_template(t, s16)
    assert (tim2.decode_tim2(out)[0][2] == s16).all()


def test_quantises_photo_into_8bit_and_keeps_tail():
    t = make_tim2(32, 32, 5, 256, extra=64)        # trailing mip-ish bytes
    rng = np.random.RandomState(3)
    src = rng.randint(0, 256, (32, 32, 4)).astype(np.uint8)
    src[..., 3] = 255
    out = tim2.encode_rgba_into_template(t, src)
    lay = tim2.first_picture_layout(out)
    assert out[lay["img_start"] + 1024:lay["clut_start"]] == bytes(64)
    got = tim2.decode_tim2(out)[0][2]
    assert got.shape == (32, 32, 4)
    assert len(np.unique(got.reshape(-1, 4), axis=0)) <= 256


def test_rejects_bad_input():
    with pytest.raises(ValueError):
        tim2.encode_rgba_into_template(make_tim2(16, 16, 5, 256),
                                       np.zeros((8, 8, 4), np.uint8))
    t = bytearray(make_tim2(16, 16, 5, 256))
    t[0x10 + 0x13] = 9
    with pytest.raises(ValueError):
        tim2.encode_rgba_into_template(bytes(t), np.zeros((16, 16, 4), np.uint8))

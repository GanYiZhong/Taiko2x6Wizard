"""Free-palette image import: quality, exact round-trip, determinism, speed."""
import struct
import time

import numpy as np
import pytest
from PIL import Image

from _qmetrics import delta_e, psnr, synth
from taiko2x6.graphics import quantize, tim2


def _pil(img, n):
    im = Image.fromarray(img, "RGBA").quantize(
        n, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    return np.asarray(im.convert("RGBA"))


def _template(w, h, colors=16):
    img = bytes(w * h // 2)
    clut = bytes(colors * 4)
    head = bytearray(b"TIM2" + bytes([4, 0]) + struct.pack("<H", 1) + bytes(8))
    pic = bytearray(0x30)
    struct.pack_into("<III", pic, 0, 0x30 + len(img) + len(clut), len(clut), len(img))
    struct.pack_into("<HH", pic, 12, 0x30, colors)
    pic[0x12] = 3          # 32-bit CLUT
    pic[0x13] = 4          # 4-bit indexed
    struct.pack_into("<HH", pic, 0x14, w, h)
    return bytes(head + pic) + img + clut


def _decoded(data):
    return tim2.decode_tim2(data)[0][2].reshape(-1, 4)


@pytest.mark.parametrize("kind", ["photo", "alpha"])
def test_quality_beats_pillow_16(kind):
    img = synth(kind)
    idx, pal = quantize.quantize_rgba(img, 16)
    out = pal[idx]
    base = _pil(img, 16)
    assert psnr(img, out) > psnr(img, base) + 2.0
    assert delta_e(img, out) < delta_e(img, base)


def test_quality_256_all_kinds():
    for kind in ("gradient", "photo", "text", "alpha"):
        img = synth(kind)
        idx, pal = quantize.quantize_rgba(img, 256)
        out = pal[idx]
        base = _pil(img, 256)
        assert psnr(img, out) >= psnr(img, base) + 1.0, kind
        assert delta_e(img, out) <= delta_e(img, base), kind


def test_text_16_not_worse():
    img = synth("text")
    idx, pal = quantize.quantize_rgba(img, 16)
    assert psnr(img, pal[idx]) > psnr(img, _pil(img, 16)) - 0.5


def test_alpha_edges_preserved():
    img = synth("alpha")
    idx, pal = quantize.quantize_rgba(img, 16)
    out = pal[idx]
    assert (out[..., 3][img[..., 3] == 0] == 0).all()
    assert (out[..., 3][img[..., 3] == 255] == 255).all()
    assert pal[0, 3] == 0


@pytest.mark.parametrize("n", [16, 256])
def test_exact_roundtrip_low_colour(n):
    rs = np.random.RandomState(3)
    cols = rs.randint(0, 256, (n, 4)).astype(np.uint8)
    cols[:, 3] = 255
    cols[0] = (0, 0, 0, 0)
    cols[1, 3] = 100
    img = cols[rs.randint(0, n, (40, 50))]
    idx, pal = quantize.quantize_rgba(img, n)
    assert np.array_equal(pal[idx], img)
    assert len(pal) <= n


def test_transparent_rgb_collapsed():
    img = np.zeros((8, 8, 4), np.uint8)
    img[..., :3] = np.arange(64).reshape(8, 8, 1)       # junk RGB under a == 0
    img[0, 0] = (9, 8, 7, 255)
    idx, pal = quantize.quantize_rgba(img, 16)
    assert len(pal) == 2 and pal[0, 3] == 0
    assert (pal[idx][..., 3] == img[..., 3]).all()


def test_deterministic():
    img = synth("photo")
    for d in (False, True):
        a = quantize.quantize_rgba(img, 16, dither=d)
        b = quantize.quantize_rgba(img.copy(), 16, dither=d)
        assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])


def test_tim2_template_roundtrip_exact():
    w = h = 32
    cols = np.array([[0, 0, 0, 0], [255, 0, 0, 255], [0, 255, 0, 255],
                     [10, 20, 30, 100], [255, 255, 255, 255]], np.uint8)
    img = cols[np.random.RandomState(1).randint(0, 5, (h, w))]
    out = tim2.encode_rgba_into_template(_template(w, h), img)
    assert np.array_equal(_decoded(out), img.reshape(-1, 4))


def test_tim2_template_lossy_close():
    w = h = 64
    img = synth("photo", 64)
    for dither in (False, True):
        out = tim2.encode_rgba_into_template(_template(w, h), img, dither=dither)
        dec = _decoded(out).reshape(h, w, 4)
        assert psnr(img, dec) > 18


def test_tim2_template_size_mismatch():
    with pytest.raises(ValueError):
        tim2.encode_rgba_into_template(_template(16, 16), np.zeros((8, 8, 4), np.uint8))


def test_speed_512():
    img = np.kron(synth("photo", 128), np.ones((4, 4, 1), np.uint8))
    for n in (16, 256):
        t = time.perf_counter()
        quantize.quantize_rgba(img, n)
        assert time.perf_counter() - t < 1.5       # ~0.1 s typical; slack for CI

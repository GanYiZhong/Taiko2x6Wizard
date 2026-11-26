"""Optimised texture quantiser must match the original dense implementation."""
import numpy as np

from taiko2x6.graphics import songtex_all as s


def _old_quantize_to_palette(rgba, pal):
    h, w, _ = rgba.shape
    px = rgba.reshape(-1, 4).astype(np.int32)
    P = pal.astype(np.int32)
    wv = np.array([1, 1, 1, 3], np.int32)
    d = (((px[:, None, :] - P[None, :, :]) ** 2) * wv).sum(axis=2)
    return d.argmin(axis=1).astype(np.uint8).reshape(h, w)


def test_quantize_identical_to_reference():
    rng = np.random.default_rng(7)
    for h, w in [(1, 1), (3, 5), (64, 256), (160, 640), (272, 64)]:
        pal = rng.integers(0, 256, (16, 4), dtype=np.uint8)
        pal[3] = pal[9]                      # duplicate entries: tie-break must match
        noisy = rng.integers(0, 256, (h, w, 4), dtype=np.uint8)
        few = pal[rng.integers(0, 16, (h, w))]          # exact palette hits
        flat = np.zeros((h, w, 4), np.uint8)
        flat[..., :3] = 255
        flat[..., 3] = rng.integers(0, 256, (h, w), dtype=np.uint8)
        for img in (noisy, few, flat):
            assert np.array_equal(s._quantize_to_palette(img, pal),
                                  _old_quantize_to_palette(img, pal))
    # non-contiguous input
    img = rng.integers(0, 256, (20, 30, 4), dtype=np.uint8)[::2, ::3]
    pal = rng.integers(0, 256, (16, 4), dtype=np.uint8)
    assert np.array_equal(s._quantize_to_palette(img, pal),
                          _old_quantize_to_palette(img, pal))

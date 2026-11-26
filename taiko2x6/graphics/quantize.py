"""Alpha-aware palette generation for importing a user's image as an indexed
(4-bit / 8-bit) game texture with a FREE palette.

This is *not* used by the fixed 16-colour song-plate path in ``songtex_all``;
that path must stay byte-identical.

Pipeline (pure numpy, deterministic, no new dependencies):

1. Images that already have <= n distinct colours are returned *exactly*.
2. Fully transparent pixels (a == 0) share one reserved palette entry.
3. Palette = weighted median-cut on the colour histogram, refined with a few
   weighted k-means rounds, in a premultiplied-alpha feature space with a
   perceptual channel weighting (so alpha is part of the distance and
   half-transparent edge pixels cluster by how they look when composited).
4. Pixels are mapped to the nearest entry; opaque pixels are only mapped to
   opaque entries (and a == 0 to the transparent one) so edges stay crisp.
5. Optional 8x8 ordered (Bayer) dithering of RGB for gradients.
"""
from __future__ import annotations

import numpy as np

# Perceptual-ish channel weights (R, G, B) and alpha weight in premultiplied space.
_WRGB = np.array([0.30, 0.59, 0.11], np.float32) ** 0.5 * 1.7
_WA = np.float32(1.0)
_OPAQUE_SNAP = 248            # alpha >= this snaps to 255 in generated palettes

_BAYER8 = (np.array([
    [0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]],
    np.float32) + 0.5) / 64.0 - 0.5


def _features(px: np.ndarray) -> np.ndarray:
    """(N,4) uint8/float RGBA -> (N,4) float32 weighted premultiplied features."""
    p = px.astype(np.float32)
    a = p[:, 3:4] / 255.0
    f = np.empty((p.shape[0], 4), np.float32)
    f[:, :3] = p[:, :3] * a * _WRGB
    f[:, 3] = p[:, 3] * _WA
    return f


def _from_features(f: np.ndarray) -> np.ndarray:
    a = np.clip(f[:, 3] / _WA, 0, 255)
    out = np.zeros((f.shape[0], 4), np.float32)
    safe = np.maximum(a, 1e-3)[:, None] / 255.0
    out[:, :3] = f[:, :3] / _WRGB / safe
    out[:, 3] = a
    out[a < 0.5, :3] = 0
    out = np.clip(np.rint(out), 0, 255).astype(np.uint8)
    out[out[:, 3] >= _OPAQUE_SNAP, 3] = 255
    return out


def _median_cut(f: np.ndarray, w: np.ndarray, n: int) -> np.ndarray:
    """Weighted median cut on feature rows; returns up to n centroids (features)."""
    boxes = [np.arange(len(f))]
    def score(ix):
        if len(ix) < 2:
            return -1.0
        x, ww = f[ix], w[ix]
        m = (x * ww[:, None]).sum(0) / ww.sum()
        return float((((x - m) ** 2) * ww[:, None]).sum())
    scores = [score(boxes[0])]
    while len(boxes) < n:
        k = int(np.argmax(scores))
        if scores[k] <= 0:
            break
        ix = boxes.pop(k)
        scores.pop(k)
        x = f[ix]
        axis = int(np.argmax(x.max(0) - x.min(0)))
        order = ix[np.argsort(x[:, axis], kind="stable")]
        cw = np.cumsum(w[order])
        cut = int(np.searchsorted(cw, cw[-1] / 2.0)) + 1
        cut = min(max(cut, 1), len(order) - 1)
        for part in (order[:cut], order[cut:]):
            boxes.append(part)
            scores.append(score(part))
    return np.array([(f[b] * w[b][:, None]).sum(0) / w[b].sum() for b in boxes],
                    np.float32)


def _nearest(f: np.ndarray, cent: np.ndarray, penalty: np.ndarray | None = None,
             chunk: int = 65536) -> np.ndarray:
    """argmin_j |f - c_j|^2 via matmul. `penalty` is (N, K) additive or None."""
    c2 = (cent ** 2).sum(1)
    out = np.empty(len(f), np.int32)
    for s in range(0, len(f), chunk):
        x = f[s:s + chunk]
        d = c2[None, :] - 2.0 * (x @ cent.T)
        if penalty is not None:
            d = d + penalty(s, s + len(x))
        out[s:s + chunk] = d.argmin(1)
    return out


def _kmeans(f, w, cent, iters):
    for _ in range(iters):
        lab = _nearest(f, cent)
        k = len(cent)
        wsum = np.bincount(lab, weights=w, minlength=k)
        new = cent.copy()
        for ch in range(4):
            s = np.bincount(lab, weights=w * f[:, ch], minlength=k)
            ok = wsum > 0
            new[ok, ch] = (s[ok] / wsum[ok]).astype(np.float32)
        if np.allclose(new, cent, atol=0.05):
            cent = new
            break
        cent = new
    return cent


def quantize_rgba(rgba, colors: int = 16, dither: bool = False,
                  kmeans_iters: int = 4):
    """Quantise HxWx4 uint8 RGBA to <= `colors` palette entries.

    Returns ``(indices HxW uint8, palette Nx4 uint8 with 0..255 alpha)``.
    Index 0 is the fully transparent entry whenever the image has any a == 0
    pixel. Images with <= `colors` distinct colours round-trip exactly.
    """
    rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
    h, w, _ = rgba.shape
    flat = rgba.reshape(-1, 4)
    packed = flat.view(np.uint32).ravel()
    uniq, inv, cnt = np.unique(packed, return_inverse=True, return_counts=True)
    inv = inv.reshape(-1)
    ucol = uniq.view(np.uint8).reshape(-1, 4)

    # Collapse all fully transparent colours into one canonical entry.
    trans_u = ucol[:, 3] == 0
    if trans_u.any() and trans_u.sum() > 1 or (trans_u.any() and ucol[trans_u][0, :3].any()):
        canon = ucol.copy()
        canon[trans_u, :3] = 0
        packed2 = np.ascontiguousarray(canon).view(np.uint32).ravel()
        uniq2, remap = np.unique(packed2, return_inverse=True)
        remap = remap.reshape(-1)
        cnt = np.bincount(remap, weights=cnt, minlength=len(uniq2)).astype(np.int64)
        inv = remap[inv]
        ucol = uniq2.view(np.uint8).reshape(-1, 4)
        trans_u = ucol[:, 3] == 0

    if len(ucol) <= colors:                        # exact
        order = np.lexsort((ucol[:, 2], ucol[:, 1], ucol[:, 0], ucol[:, 3] != 0))
        pal = ucol[order]
        rank = np.empty(len(order), np.int64)
        rank[order] = np.arange(len(order))
        return rank[inv].astype(np.uint8).reshape(h, w), pal

    has_t = bool(trans_u.any())
    body = ~trans_u
    f_u = _features(ucol[body])
    w_u = cnt[body].astype(np.float64)
    n_body = colors - (1 if has_t else 0)
    # Weight by sqrt-damped counts so rare accent colours are not starved.
    wm = np.sqrt(w_u)
    # Build the palette from a bounded, deterministic sample of the histogram.
    if len(f_u) > 30000:
        sel = np.random.RandomState(0).choice(len(f_u), 30000, replace=False)
        sel.sort()
        f_u, wm = f_u[sel], wm[sel]
    cent = _kmeans(f_u, wm, _median_cut(f_u, wm, n_body), kmeans_iters)
    body_pal = _from_features(cent)
    # Order by luminance for a stable, readable palette.
    lum = body_pal[:, :3].astype(np.float32) @ np.array([.299, .587, .114], np.float32)
    body_pal = body_pal[np.argsort(lum, kind="stable")]
    pal = (np.vstack([np.zeros((1, 4), np.uint8), body_pal]) if has_t else body_pal)
    off = 1 if has_t else 0
    cent_f = _features(pal)
    pal_opaque = pal[:, 3] == 255
    if has_t:
        pal_opaque[0] = False

    def map_pixels(px, ptr_alpha_opaque):
        f = _features(px)
        if pal_opaque.any() and not pal_opaque.all():
            def pen(s, e):
                return (ptr_alpha_opaque[s:e, None] & ~pal_opaque[None, :]) * 1e9
        else:
            pen = None
        return _nearest(f, cent_f, pen)

    if not dither:
        opq_u = ucol[:, 3] == 255
        lab_u = map_pixels(ucol, opq_u)
        if has_t:
            lab_u[trans_u] = 0
        return lab_u[inv].astype(np.uint8).reshape(h, w), pal

    # Ordered dither: nudge RGB of non-transparent pixels, keep alpha.
    step = 255.0 / max(2.0, n_body ** (1.0 / 3.0)) * 0.55
    thr = np.tile(_BAYER8, (-(-h // 8), -(-w // 8)))[:h, :w].reshape(-1, 1)
    px = flat.astype(np.float32)
    px[:, :3] = np.clip(px[:, :3] + thr * step, 0, 255)
    px = np.rint(px).astype(np.uint8)
    lab = map_pixels(px, flat[:, 3] == 255)
    if has_t:
        lab[flat[:, 3] == 0] = 0
    return lab.astype(np.uint8).reshape(h, w), pal


def to_ps2_alpha(pal: np.ndarray) -> np.ndarray:
    """0..255 alpha -> PS2 0..128 (255 -> 128, round to nearest)."""
    out = np.array(pal, np.uint8, copy=True)
    out[:, 3] = ((out[:, 3].astype(np.uint16) + 1) // 2).astype(np.uint8)
    return out

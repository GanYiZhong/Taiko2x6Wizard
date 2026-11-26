"""Image-quality helpers + synthetic images for the quantiser tests."""
import numpy as np


def comp(rgba, bg):
    a = rgba[..., 3:4].astype(np.float64) / 255
    return rgba[..., :3] * a + bg * (1 - a)


def psnr(a, b):
    m = np.mean([np.mean((comp(a, bg) - comp(b, bg)) ** 2) for bg in (0.0, 255.0)])
    return 99.0 if m == 0 else 10 * np.log10(255 ** 2 / m)


def _lab(rgb):
    c = rgb / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    m = np.array([[.4124, .3576, .1805], [.2126, .7152, .0722], [.0193, .1192, .9505]])
    xyz = (c @ m.T) / np.array([.95047, 1, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])], -1)


def delta_e(a, b):
    return float(np.mean([np.linalg.norm(_lab(comp(a, bg)) - _lab(comp(b, bg)), axis=-1).mean()
                          for bg in (60.0, 200.0)]))


def synth(kind, n=128, seed=1):
    rs = np.random.RandomState(seed)
    y, x = np.mgrid[0:n, 0:n].astype(np.float32) / (n - 1)
    img = np.full((n, n, 4), 255, np.uint8)
    if kind == "gradient":
        img[..., 0] = x * 255
        img[..., 1] = y * 255
        img[..., 2] = (1 - x) * 255
    elif kind == "photo":
        base = np.stack([np.sin(x * 6) * .4 + .5, np.cos(y * 5 + x * 2) * .4 + .5,
                         (x * y) ** .5], -1)
        nz = rs.rand(n // 4 + 1, n // 4 + 1, 3)
        nz = np.kron(nz, np.ones((4, 4, 1)))[:n, :n]
        img[..., :3] = np.clip((base * .7 + nz * .3) * 255 + rs.randn(n, n, 3) * 6, 0, 255)
    elif kind == "text":
        from PIL import Image, ImageDraw, ImageFilter
        im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        for i in range(0, n, 24):
            d.text((4, i), "Taiko AaBb 123", fill=(255, 255, 255, 255))
            d.ellipse((n - 40, i, n - 10, i + 20), outline=(220, 40, 40, 255), width=3)
        img = np.array(im.filter(ImageFilter.GaussianBlur(0.7)))
    elif kind == "alpha":
        r = np.hypot(x - .5, y - .5)
        img[..., :3] = np.stack([x * 255, y * 255, (1 - x) * 200], -1)
        img[..., 3] = np.clip((0.4 - r) / 0.08, 0, 1) * 255
        img[r > .45, :3] = 0
    return img

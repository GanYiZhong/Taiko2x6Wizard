#!/usr/bin/env python3
"""
Per-song texture-set generator for Taiko no Tatsujin (SYSTEM256 / PS2 arcade).

A single song needs MANY name/title textures, each a 4-bit-indexed TIM2 (".nut")
of white brush-script text on transparency. This module renders all eight texture
types with the authentic 勘亭流 brush font (Font.ttf, DFPKanTeiRyu-XB) and encodes
each into a real nut by splicing the rendered pixels + a 16-level white-alpha
palette into an existing nut used as a TIM2 *template* (keeping the header / GS
registers / sizes game-valid). The encoding path reuses tim2.encode_indexed4_into_template
exactly like songtex.py does for the kenri plate.

The 8 types (all image_type 4, 16-colour, base 0x10), with the layout matched
from decoding real originals under test/music_texture/:

  type           size      orientation / placement (from real samples)
  games          640x32    horizontal, RIGHT-aligned, vertically centred
  kenri_song     640x160   4 lines: title / 作詞：lyr / 作曲：comp / © copyright
  result         352x48    horizontal, RIGHT-aligned, vertically centred
  songlevel      264x48    horizontal, RIGHT-aligned, vertically centred
  topten         280x80    horizontal, CENTERED both axes
  total_result   480x88    horizontal, CENTERED both axes
  select_full    96x272    VERTICAL (tate-gaki), column centred, top-anchored
  select_non     56x248    VERTICAL (tate-gaki), column centred, top-anchored

Public API:
  render_texture(type_name, template_nut, title, lyricist, composer, copyright, **opts) -> bytes
  generate_song_textures(templates, title, lyricist, composer, copyright) -> {type: nut_bytes}
  class SongTextureDialog(QDialog)   # title/lyricist/composer/copyright + per-type preview grid

Only this file is created; tim2.py and songtex.py are reused, never modified.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import tim2

# --------------------------------------------------------------------------- #
#  Constants
# --------------------------------------------------------------------------- #
import apppaths
_HERE = Path(__file__).resolve().parent
FONT_PATH = str(apppaths.resource_dir() / "Font.ttf")   # MANDATORY 勘亭流 brush font

TEXTURE_TYPES = [
    "games", "kenri_song", "result", "songlevel",
    "topten", "total_result", "select_full", "select_short", "select_non",
]

# Canonical dimensions per type (must match the template; used for sanity only).
TYPE_DIMS = {
    "games":        (640, 32),
    "kenri_song":   (640, 160),
    "result":       (352, 48),
    "songlevel":    (264, 48),
    "topten":       (280, 80),
    "total_result": (480, 88),
    "select_full":  (96, 272),
    "select_short": (56, 248),
    "select_non":   (56, 248),
}

# Real-sample sub-folders for templates (folder pattern, file-name pattern).
# {} is the song id. For the *_<id> directory style the file is literally "nut".
_TEMPLATE_LOCATIONS = {
    "games":        ("games_{}", "nut"),
    "kenri_song":   ("kenri_song_{}", "nut"),
    "result":       ("result_{}", "nut"),
    "songlevel":    ("songlevel_{}", "nut"),
    "topten":       ("topten_{}", "nut"),
    "total_result": ("total_result_{}", "nut"),
    "select_full":  ("music_select", "select_full_{}"),
    "select_short": ("music_select", "select_short_{}"),
    "select_non":   ("music_select_easy", "select_non_{}"),
}
_TEST_TEXTURE_ROOT = _HERE / "test" / "music_texture"


# --------------------------------------------------------------------------- #
#  Font helpers
# --------------------------------------------------------------------------- #
_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}


def _font(size: int) -> ImageFont.FreeTypeFont:
    """Load Font.ttf (DFPKanTeiRyu-XB) at the given pixel size, cached."""
    size = max(6, int(size))
    ft = _FONT_CACHE.get(size)
    if ft is None:
        ft = ImageFont.truetype(FONT_PATH, size)
        _FONT_CACHE[size] = ft
    return ft


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int, int, int]:
    """Return (left, top, width, height) tight bbox of `text` for `font`."""
    if not text:
        return (0, 0, 0, 0)
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    return l, t, r - l, b - t


def _fit_font_to_width(text: str, base_size: int, max_w: int,
                       min_size: int = 8) -> ImageFont.FreeTypeFont:
    """Pick the largest size <= base_size whose rendered width fits max_w.

    Uses the font's advance width (getlength) rather than the ink bbox, and
    keeps a small safety pad, so brush glyphs with right-side bearing don't
    touch or overrun the right edge.
    """
    pad = 2
    size = max(min_size, int(base_size))
    while size > min_size:
        f = _font(size)
        # advance width includes side bearings the ink bbox would omit
        adv = f.getlength(text)
        if adv <= max_w - pad:
            return f
        size -= 1
    return _font(min_size)


# --------------------------------------------------------------------------- #
#  RGBA -> 4-bit white-alpha indices + palette  (reuse songtex's approach)
# --------------------------------------------------------------------------- #
try:
    # Reuse the proven mapping from songtex.py when importable.
    from songtex import rgba_to_indexed4_white as _rgba_to_indexed4_white_ext
except Exception:                                   # pragma: no cover
    _rgba_to_indexed4_white_ext = None


def rgba_to_indexed4_white(rgba: np.ndarray):
    """Map white-on-transparent RGBA to a 16-level white alpha ramp.

    Index 0 = fully transparent; 1..15 = solid white at increasing PS2 alpha
    (0..128, 128 == opaque). Returns (indices HxW uint8, palette 16x4 uint8).

    Delegates to songtex.rgba_to_indexed4_white when available so both modules
    stay byte-for-byte identical; otherwise uses an equivalent local copy.
    """
    if _rgba_to_indexed4_white_ext is not None:
        return _rgba_to_indexed4_white_ext(rgba)
    alpha = rgba[:, :, 3].astype(np.float32)
    idx = np.clip(np.round(alpha / 255.0 * 15.0), 0, 15).astype(np.uint8)
    # A fresh linear 16-entry palette is correct here: 16-colour 4-bit CLUTs are
    # NOT subject to the 256-entry CSM1 swizzle, so this must never be
    # unswizzled (do not "fix" this by reordering entries).
    pal = np.zeros((16, 4), np.uint8)
    for i in range(16):
        a8 = round(i / 15 * 255)
        ps2a = round(a8 / 255 * 128)
        pal[i] = (255, 255, 255, ps2a)
    return idx, pal


def _ink_rgb_from_template(template_nut: bytes, default=(255, 255, 255)) -> tuple:
    """RGB of the template's solid ink colour (its most-opaque non-transparent
    pixels). These text plates are a single colour on transparent — BLACK for the
    kenri/credits plate, etc. — so reusing that colour keeps our generated text the
    SAME colour as the original instead of forcing white (which vanished in-game)."""
    try:
        _w, _h, rgba = tim2.decode_tim2(template_nut)[0]
        px = rgba.reshape(-1, 4)
        op = px[px[:, 3] > 0]
        if len(op) == 0:
            return default
        # The single most-opaque colour = the solid ink (e.g. pure black). Taking
        # its RGB directly avoids blending in anti-aliased edge pixels (which would
        # wash a black ink out to grey).
        uniq = np.unique(op, axis=0)
        ink = uniq[int(uniq[:, 3].argmax())]
        return (int(ink[0]), int(ink[1]), int(ink[2]))
    except Exception:
        return default


def rgba_to_indexed4(rgba: np.ndarray, ink_rgb=(255, 255, 255)):
    """Like rgba_to_indexed4_white but in an arbitrary ink colour (from template)."""
    alpha = rgba[:, :, 3].astype(np.float32)
    idx = np.clip(np.round(alpha / 255.0 * 15.0), 0, 15).astype(np.uint8)
    r, g, b = ink_rgb
    pal = np.zeros((16, 4), np.uint8)
    for i in range(16):
        ps2a = round((i / 15 * 255) / 255 * 128)
        pal[i] = (r, g, b, ps2a)
    return idx, pal


# --------------------------------------------------------------------------- #
#  Explicit per-type styling (colours/sizes matched to decoded retail nuts)
# --------------------------------------------------------------------------- #
from PIL import ImageFilter  # noqa: E402

# One reusable measuring context (ink-bbox probing without a real canvas).
_MEASURE = ImageDraw.Draw(Image.new("RGBA", (4, 4)))

# Per-type render style, derived from decoding the retail nuts:
#   kind    : "h" horizontal line, "v" vertical (tate-gaki), "kenri" credits plate
#   align   : horizontal alignment for "h" ("right"/"center")
#   margin  : side padding in px
#   margin  : side padding in px (this is the (1 - width_fill) space)
#   hcap    : max ink height as a FRACTION of the texture height
#   outline : True = white fill + black outline; False = flat white
# Sizing model: the glyph is grown to FILL the available width (w - 2*margin)
# OR reach `hcap` of the height, whichever binds first — this reproduces the
# retail proportions (topten fills ~92% width, result ~79% height, etc.) instead
# of shrinking long titles to a small height-only target.
STYLE = {
    "games":        {"kind": "h", "align": "right",  "margin": 33, "hcap": 0.70, "outline": False},
    "songlevel":    {"kind": "h", "align": "right",  "margin": 6,  "hcap": 0.82, "outline": False},
    "result":       {"kind": "h", "align": "right",  "margin": 6,  "hcap": 0.64, "outline": True},
    "topten":       {"kind": "h", "align": "center", "margin": 11, "hcap": 0.72, "outline": True},
    "total_result": {"kind": "h", "align": "center", "margin": 14, "hcap": 0.38, "outline": True},
    "kenri_song":   {"kind": "kenri", "outline": True},
    # select_full / select_short are white + black outline in retail; the
    # per-difficulty select_non plates (easy/normal/hard/mania/music) are flat.
    "select_full":  {"kind": "v", "outline": True},
    "select_short": {"kind": "v", "outline": True},
    "select_non":   {"kind": "v", "outline": False},
}


# --------------------------------------------------------------------------- #
#  Template analysis: measure the ACTUAL template so new art matches it
# --------------------------------------------------------------------------- #
def analyze_template(template_nut: bytes, type_name: str) -> dict:
    """Measure a template texture's own text so renders can match it exactly.

    Decodes the template's first TIM2 picture and inspects the inked (opaque)
    pixels to learn how THIS plate is drawn, instead of trusting the static
    STYLE table (which was measured on one retail set and is wrong for
    templates from other songs/games):

      outline   True when the ink holds BOTH a dark edge and a bright fill
                (white/gold text with a black border). False for flat
                single-colour plates.
      fill_rgb  colour of the glyph fill (the bright core when outlined).
      ink_rgb   colour of a flat plate's ink (black credits text stays black).
      stroke    outline thickness in px, estimated as dark-edge area over the
                ink boundary length.
      hcap      ink height as a fraction of the plate height (horizontal kinds).
      margin    the anchored side margin in px (right margin when
                right-aligned; smallest side margin otherwise).
      align     'right' | 'center' | 'left' from where the ink bbox sits.
      top_margin  first inked row (vertical kinds).
      valid     False when the template is empty/undecodable — caller should
                fall back to STYLE defaults.
    """
    out = {"valid": False}
    try:
        _w, _h, rgba = tim2.decode_tim2(template_nut)[0]
    except Exception:
        return out
    h, w = rgba.shape[:2]
    alpha = rgba[:, :, 3].astype(np.int32)
    mask = alpha > 40
    n_ink = int(mask.sum())
    if n_ink < 20:                       # blank/placeholder plate: nothing to learn
        return out

    ys, xs = np.nonzero(mask)
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    left, right = x0, (w - 1 - x1)
    top, bottom = y0, (h - 1 - y1)

    px = rgba[mask].astype(np.int32)
    lum = (299 * px[:, 0] + 587 * px[:, 1] + 114 * px[:, 2]) // 1000
    dark = lum < 70
    bright = lum > 150
    dark_frac = float(dark.mean())
    bright_frac = float(bright.mean())
    # Outlined = a real black edge AND a bright fill both present in quantity.
    outline = dark_frac >= 0.08 and bright_frac >= 0.15

    def _modal_rgb(sel: np.ndarray, default=(255, 255, 255)) -> tuple:
        if not sel.any():
            return default
        cols, counts = np.unique(px[sel][:, :3], axis=0, return_counts=True)
        r, g, b = cols[counts.argmax()]
        return (int(r), int(g), int(b))

    fill_rgb = _modal_rgb(bright)          # bright core = the fill colour
    ink_rgb = _modal_rgb(np.ones(len(px), bool))   # overall modal = flat ink

    # Outline thickness ~= dark-edge area / ink boundary length.
    stroke = 0
    if outline:
        m = mask
        boundary = m & ~(np.roll(m, 1, 0) & np.roll(m, -1, 0) &
                         np.roll(m, 1, 1) & np.roll(m, -1, 1))
        blen = max(1, int(boundary.sum()))
        stroke = int(round(int(dark.sum()) / blen))
        stroke = max(2, min(7, stroke))

    # Alignment from where the ink box sits (tolerance scales with the plate).
    tol = max(4, int(w * 0.08))
    if abs(left - right) <= tol:
        align = "center"
    elif right < left:
        align = "right"
    else:
        align = "left"

    # Vertical (tate-gaki) plates: learn the template's STANDARD glyph size by
    # segmenting the inked rows into character cells. Retail plates draw every
    # title at one fixed size (long titles shrink) — without this cap a short
    # title balloons to fill the whole column.
    vglyph_w = vglyph_h = 0
    proj = np.flatnonzero(mask.any(axis=1))
    if len(proj):
        # contiguous row runs, merging small (<=3 px) internal stroke gaps
        breaks = np.flatnonzero(np.diff(proj) > 3)
        starts = np.r_[proj[0], proj[breaks + 1]]
        ends = np.r_[proj[breaks], proj[-1]]
        heights = ends - starts + 1
        if len(heights):
            vglyph_h = int(heights.max())
            vglyph_w = int(x1 - x0 + 1)

    out.update({
        "valid": True, "outline": outline,
        "fill_rgb": fill_rgb, "ink_rgb": ink_rgb, "stroke": stroke,
        "hcap": min(0.95, max(0.20, (y1 - y0 + 1) / h)),
        "margin": max(2, right if align == "right" else
                      (left if align == "left" else min(left, right))),
        "align": align,
        "top_margin": max(2, top), "bottom_margin": max(2, bottom),
        "vglyph_w": vglyph_w, "vglyph_h": vglyph_h,
    })
    return out


def _ink_alpha_palette(rgb: tuple) -> np.ndarray:
    """16-entry alpha-ramp palette in an arbitrary ink colour (0..255 alpha)."""
    r, g, b = rgb
    pal = np.zeros((16, 4), np.uint8)
    for i in range(16):
        pal[i] = (r, g, b, round(i / 15 * 255))
    return pal


def _outline_palette_for(fill_rgb: tuple) -> np.ndarray:
    """Black-outline→fill greyscale-style ramp, but ending at the template's
    fill colour (retail 'result' plates are gold-filled, not white)."""
    fr, fg, fb = fill_rgb
    entries = [(0, 0, 0, 0), (0, 0, 0, 128), (0, 0, 0, 204)]
    steps = 13
    for i in range(steps):
        t = i / (steps - 1)
        entries.append((round(2 + (fr - 2) * t), round(2 + (fg - 2) * t),
                        round(2 + (fb - 2) * t), 255))
    return np.asarray(entries, np.uint8)


def _white_alpha_palette() -> np.ndarray:
    """16-entry palette: index 0 transparent, 1..15 opaque-white alpha ramp
    (0..255 alpha space; converted to PS2 0..128 before it is written)."""
    pal = np.zeros((16, 4), np.uint8)
    for i in range(16):
        pal[i] = (255, 255, 255, round(i / 15 * 255))
    return pal


def _greyscale_outline_palette() -> np.ndarray:
    """16-entry palette for white-fill + black-outline text, mirroring the
    black→white greyscale ramp the retail topten/kenri/total_result nuts use.
    index 0 = transparent; 1..2 = semi-transparent black (outer anti-aliased
    edge); 3..15 = opaque black→white ramp (outline body → grey transition →
    white fill). 0..255 alpha space; converted to PS2 0..128 on write."""
    entries = [
        (0, 0, 0, 0),          # transparent
        (0, 0, 0, 128),        # faint outer edge
        (0, 0, 0, 204),        # outer edge
        (2, 2, 2, 255),        # black outline body
        (38, 38, 38, 255),
        (63, 63, 63, 255),
        (88, 88, 88, 255),
        (112, 112, 112, 255),
        (140, 140, 140, 255),
        (165, 165, 165, 255),
        (191, 191, 191, 255),
        (212, 212, 212, 255),
        (230, 230, 230, 255),
        (245, 245, 245, 255),
        (255, 255, 255, 255),  # white fill
        (255, 255, 255, 255),  # spare white
    ]
    return np.asarray(entries, np.uint8)


def _to_ps2_alpha(pal255: np.ndarray) -> np.ndarray:
    """Convert a 0..255-alpha palette to PS2 0..128 alpha for CLUT writing."""
    out = pal255.copy()
    out[:, 3] = np.round(pal255[:, 3].astype(np.float32) / 255.0 * 128.0).astype(np.uint8)
    return out


def _flat_white(coverage: np.ndarray) -> np.ndarray:
    """Flat white RGBA from a coverage mask (no outline)."""
    h, w = coverage.shape
    out = np.zeros((h, w, 4), np.float32)
    out[:, :, :3] = 255.0
    out[:, :, 3] = coverage * 255.0
    return out.clip(0, 255).astype(np.uint8)


def _render_white_outline(coverage: np.ndarray, stroke: int) -> np.ndarray:
    """White fill + black outline RGBA from a coverage mask (HxW float 0..1).

    The outline is the coverage dilated by `stroke` (square MaxFilter for the
    body, then a light blur to ROUND the corners so it reads like the smooth
    retail outline rather than a boxy one), painted opaque black; the fill is
    white scaled by coverage, so the fill→outline boundary is a natural black→
    white greyscale transition that maps cleanly onto the outline palette.
    """
    h, w = coverage.shape
    cov_u = (coverage * 255).astype(np.uint8)
    cov_img = Image.fromarray(cov_u, "L")
    k = max(1, stroke) * 2 + 1
    dil_img = cov_img.filter(ImageFilter.MaxFilter(k))
    # Round the square dilation corners; re-threshold so the ring stays crisp.
    dil_img = dil_img.filter(ImageFilter.GaussianBlur(max(0.6, stroke * 0.5)))
    dil = np.asarray(dil_img, dtype=np.float32) / 255.0
    dil = np.clip((dil - 0.35) / 0.30, 0.0, 1.0)     # crisp rounded edge
    out = np.zeros((h, w, 4), np.float32)
    # RGB = white where the glyph is inked (coverage=1 → 255 white); in the
    # dilated outline ring coverage=0 → 0 black. alpha = the dilated mask so the
    # ring is opaque black around the white core.
    out[:, :, 0] = out[:, :, 1] = out[:, :, 2] = coverage * 255.0
    out[:, :, 3] = np.maximum(dil, coverage) * 255.0
    return out.clip(0, 255).astype(np.uint8)


def _quantize_to_palette(rgba: np.ndarray, pal: np.ndarray) -> np.ndarray:
    """Nearest-colour map HxWx4 RGBA -> HxW indices into the 16-entry palette."""
    h, w, _ = rgba.shape
    px = rgba.reshape(-1, 4).astype(np.int32)
    P = pal.astype(np.int32)
    # Weight alpha strongly so transparent snaps to the transparent entry.
    wv = np.array([1, 1, 1, 3], np.int32)
    d = (((px[:, None, :] - P[None, :, :]) ** 2) * wv).sum(axis=2)
    return d.argmin(axis=1).astype(np.uint8).reshape(h, w)


def _font_for_box(text: str, max_w: float, max_h: float) -> ImageFont.FreeTypeFont:
    """Largest font that fills the box: ink height ≤ max_h AND advance width ≤
    max_w. Whichever bound binds first decides the size, so the text grows to
    FILL the plate (matching the bold retail proportions) instead of staying at
    a small fixed height target."""
    if not text:
        return _font(8)
    lo, hi, best = 8, max(12, int(max_h * 3)), 8
    while lo <= hi:
        mid = (lo + hi) // 2
        f = _font(mid)
        _, _, _tw, th = _text_size(_MEASURE, text, f)
        if th <= max_h and f.getlength(text) <= max_w:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return _font(best)


# --------------------------------------------------------------------------- #
#  Per-type RGBA renderers
# --------------------------------------------------------------------------- #
def _new_canvas(w: int, h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _render_horizontal(w: int, h: int, text: str, hcap: float,
                       align: str, margin: int) -> tuple[np.ndarray, int]:
    """Render one horizontal line, white on transparent, grown to FILL the plate
    (width to `w - 2*margin`, height capped at `hcap*h`). Returns (RGBA, font px).

    align: 'right' anchors the text's right edge `margin` px from the right;
           'center' centres horizontally. Vertically always centred.
    """
    img, d = _new_canvas(w, h)
    if not text:
        return np.asarray(img, dtype=np.uint8), 12
    max_w = max(8, w - 2 * margin)
    font = _font_for_box(text, max_w, h * hcap)
    l, t, tw, th = _text_size(d, text, font)
    if align == "right":
        x = w - margin - tw - l
    else:  # center
        x = (w - tw) // 2 - l
    y = (h - th) // 2 - t
    d.text((x, y), text, font=font, fill=(255, 255, 255, 255))
    return np.asarray(img, dtype=np.uint8), font.size


def _render_kenri(w: int, h: int, title: str, lyricist: str,
                  composer: str) -> tuple[np.ndarray, int]:
    """kenri plate: the SONG TITLE only — CENTERED both axes, grown to fill the
    plate width (height capped so it stays a title band). White fill (black
    outline added later). No 作詞/作曲/copyright lines (per the user's spec).
    `lyricist`/`composer` are accepted for a stable signature but unused."""
    img, d = _new_canvas(w, h)
    if not title:
        return np.asarray(img, dtype=np.uint8), 12
    margin = 20
    font = _font_for_box(title, w - 2 * margin, h * 0.42)
    l, t, tw, th = _text_size(d, title, font)
    x = (w - tw) // 2 - l
    y = (h - th) // 2 - t
    d.text((x, y), title, font=font, fill=(255, 255, 255, 255))
    return np.asarray(img, dtype=np.uint8), font.size


def _render_vertical(w: int, h: int, text: str, base_size: int,
                     top_margin: int = 10, bottom_margin: int = 8,
                     cell_px: float | None = None,
                     max_glyph_w: int | None = None) -> np.ndarray:
    """Render text VERTICALLY (tate-gaki): glyphs stacked top-to-bottom.

    Each character is drawn on its own row, the column centred horizontally at
    `w/2`, starting `top_margin` px from the top. The per-character cell height
    auto-shrinks so the whole title fits between top_margin and h-bottom_margin;
    individual glyph width is clamped so wide kana/kanji never overflow `w`.
    Newlines/spaces are honoured as soft breaks (skipped, with a small gap).
    """
    img, d = _new_canvas(w, h)
    chars = [c for c in text if c not in ("\n", "\r")]
    if not chars:
        return np.asarray(img, dtype=np.uint8), base_size

    avail_h = max(8, h - top_margin - bottom_margin)
    avail_w = max(6, w - 4)

    # Choose a glyph size so that n cells stack within avail_h, and each glyph
    # also fits avail_w. Start from base_size and shrink until both hold.
    n = len(chars)

    def _metrics(f) -> tuple[int, int, int]:
        """Return (cell_height, max_glyph_width, max_glyph_height) for chars."""
        asc, desc = f.getmetrics()
        max_gw = max_gh = 0
        for c in chars:
            if c.strip() == "":
                continue
            _, _, gw, gh = _text_size(d, c, f)
            max_gw = max(max_gw, gw)
            max_gh = max(max_gh, gh)
        # Tate-gaki spacing = the glyph ink height + a small gap (~10%). Using the
        # full font line box (asc+desc) leaves too much air between characters and
        # spreads the column out vs. the tight retail plates.
        cell = max(max_gh, int(round(max_gh * 1.03)))
        return cell, max_gw, max_gh

    if cell_px:
        # Template-standard CELL model (retail behaviour): every character
        # gets a fixed-height cell learned from the template; the glyph fills
        # its cell. Only a too-long title shrinks the cell — a short title
        # keeps the retail size instead of ballooning to fill the column.
        cell_t = min(float(cell_px), avail_h / n)
        wcap = min(avail_w, max_glyph_w or avail_w)
        size = max(base_size, int(cell_t * 1.8) + 2)
        while size > 8:
            f = _font(size)
            _, max_gw, max_gh = _metrics(f)
            if max_gh <= cell_t * 0.88 and max_gw <= wcap:
                break
            size -= 1
        f = _font(size)
        cell = max(8, int(round(cell_t)))
    else:
        size = base_size
        while size > 8:
            f = _font(size)
            cell, max_gw, max_gh = _metrics(f)
            # require the stacked cells AND the tallest/widest glyph to all fit
            if cell * n <= avail_h and max_gw <= avail_w and max_gh <= avail_h:
                break
            size -= 1
        f = _font(size)
        cell, _, _ = _metrics(f)

    total_h = cell * n
    y = top_margin + max(0, (avail_h - total_h) // 2)
    cx = w / 2.0
    for c in chars:
        if c.strip() == "":
            y += cell // 2
            continue
        l, t, gw, gh = _text_size(d, c, f)
        x = int(round(cx - gw / 2.0)) - l
        # vertically centre the glyph inside its cell
        gy = y + (cell - gh) // 2 - t
        d.text((x, gy), c, font=f, fill=(255, 255, 255, 255))
        y += cell
    return np.asarray(img, dtype=np.uint8), size


# Per-type render dispatch. Returns (RGBA HxWx4 white-on-clear, chosen font px).
def _render_rgba(type_name: str, w: int, h: int, title: str, lyricist: str,
                 composer: str, opts: dict) -> tuple[np.ndarray, int]:
    st = STYLE[type_name]
    kind = st["kind"]
    if kind == "h":
        return _render_horizontal(
            w, h, title, opts.get("hcap", st["hcap"]),
            align=opts.get("align", st["align"]),
            margin=opts.get("margin", st["margin"]))
    if kind == "kenri":
        return _render_kenri(w, h, title, lyricist, composer)
    if kind == "v":
        # Both kana/kanji and Latin stack UPRIGHT (tate-gaki), like the retail
        # select plates (CAPTAIN NEO / RHYTHM AND POLICE) — one glyph per row,
        # grown as large as the column allows (start from the column width so a
        # glyph can fill it; the height cap shrinks longer titles). No rotation.
        return _render_vertical(w, h, title, opts.get("size", w),
                                top_margin=opts.get("top_margin", 8),
                                cell_px=opts.get("cell_px"),
                                max_glyph_w=opts.get("max_glyph_w"))
    raise ValueError(f"unknown texture type {type_name!r}")


# --------------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------------- #
def render_texture(type_name: str, template_nut: bytes, title: str,
                   lyricist: str = "", composer: str = "", copyright: str = "",
                   **opts) -> bytes:
    """Render `type_name`'s text into `template_nut` and return new nut bytes.

    The template fixes the output dimensions: the text is rendered onto a canvas
    matching the template's first TIM2 picture (width x height), mapped to a
    16-level white-alpha index image + palette, then spliced back in.
    """
    if type_name not in TEXTURE_TYPES:
        raise ValueError(f"unknown texture type {type_name!r}")
    lay = tim2.first_picture_layout(template_nut)
    w, h = lay["width"], lay["height"]

    # Measure the ACTUAL template (black edge or not, colours, proportions,
    # margins) so the new art matches it; the static STYLE table is only the
    # fallback for blank/unreadable templates. Explicit **opts always win.
    info = analyze_template(template_nut, type_name) if opts.get(
        "analyze", True) else {"valid": False}
    if info["valid"]:
        st = STYLE[type_name]
        if st["kind"] == "h":
            opts.setdefault("hcap", info["hcap"])
            # A left-aligned measurement on a right/center retail kind is more
            # likely a short-title artefact than a real layout — only adopt the
            # measured align when it is one the renderer supports.
            if info["align"] in ("right", "center"):
                opts.setdefault("align", info["align"])
                if info["align"] == "right":
                    opts.setdefault("margin", min(info["margin"], w // 4))
        elif st["kind"] == "v":
            opts.setdefault("top_margin", min(info["top_margin"], h // 6))
            # Retail plates give every character a fixed-height CELL and the
            # glyph fills it; only long titles shrink. The cell height is a
            # UI-layout constant (calibrated on retail genpe/KAGEKIYO, whose
            # character count is known: select_full 32.4/272, short 24.2/264);
            # the template supplies the width cap — its ink width minus the
            # outline the render adds back.
            ratio = {"select_full": 0.119}.get(type_name, 0.092)
            opts.setdefault("cell_px", h * ratio)
            if info["vglyph_w"] >= 10:
                opts.setdefault(
                    "max_glyph_w",
                    max(10, info["vglyph_w"] - 2 * info["stroke"]))
        outline = info["outline"]
    else:
        outline = STYLE[type_name]["outline"]

    rgba, font_px = _render_rgba(type_name, w, h, title, lyricist, composer, opts)
    coverage = rgba[:, :, 3].astype(np.float32) / 255.0
    # Colourise to match the template and WRITE a matching CLUT: outlined
    # plates get a black-edge→fill ramp in the TEMPLATE's fill colour (white,
    # gold, …); flat plates get an alpha ramp in the template's own ink colour
    # (black credits text stays black instead of being forced white).
    if outline:
        # Outline weight: the template's measured stroke if we have it,
        # otherwise ~9% of the font size (floor 3, cap 7).
        stroke = (info.get("stroke") or 0) if info["valid"] else 0
        if not stroke:
            stroke = max(3, min(7, round(font_px * 0.09)))
        final = _render_white_outline(coverage, stroke)
        pal255 = (_outline_palette_for(info["fill_rgb"])
                  if info["valid"] else _greyscale_outline_palette())
    else:
        final = _flat_white(coverage)
        pal255 = (_ink_alpha_palette(info["ink_rgb"])
                  if info["valid"] else _white_alpha_palette())
    idx = _quantize_to_palette(final, pal255)
    return tim2.encode_indexed4_into_template(template_nut, idx, _to_ps2_alpha(pal255))


def generate_song_textures(templates: dict, title: str, lyricist: str = "",
                           composer: str = "", copyright: str = "") -> dict:
    """Generate every provided texture type for one song.

    `templates`: {type_name: template_nut_bytes}. Unknown keys are ignored.
    Returns {type_name: new_nut_bytes} for each valid template.
    """
    out = {}
    for type_name, tpl in templates.items():
        if type_name not in TEXTURE_TYPES:
            continue
        if not tpl:
            # Empty/missing template: skip but make it visible so a missing
            # template isn't silently dropped.
            import warnings
            warnings.warn(f"skipping texture type {type_name!r}: empty template")
            continue
        out[type_name] = render_texture(type_name, tpl, title, lyricist,
                                        composer, copyright)
    return out


# --------------------------------------------------------------------------- #
#  Template discovery (for the dialog's own use / self-test)
# --------------------------------------------------------------------------- #
def load_test_templates(song_id: str = "anp") -> dict:
    """Load a real template per type from test/music_texture/ for `song_id`.

    Returns {type_name: nut_bytes} for every type whose sample exists on disk.
    """
    out = {}
    for type_name, (folder, fname) in _TEMPLATE_LOCATIONS.items():
        if "{}" in folder:
            p = _TEST_TEXTURE_ROOT / folder.format(song_id) / fname
        else:
            p = _TEST_TEXTURE_ROOT / folder / fname.format(song_id)
        if p.exists():
            out[type_name] = p.read_bytes()
    return out


# --------------------------------------------------------------------------- #
#  Qt generator dialog
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtCore import Qt, QRectF
    from PySide6.QtGui import QImage, QPixmap, QPainter, QColor
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout, QLineEdit,
        QPushButton, QWidget, QLabel, QScrollArea, QMessageBox, QFrame,
    )

    class _NutPreview(QWidget):
        """Draws a decoded nut over a checkerboard, scaled to fit."""

        def __init__(self, label: str, max_w: int = 220, max_h: int = 150):
            super().__init__()
            self._label = label
            self._pix = None
            self._max_w, self._max_h = max_w, max_h
            self.setMinimumSize(max_w, max_h)

        def set_nut(self, nut: bytes):
            try:
                w, h, rgba = tim2.decode_tim2(nut)[0]
                buf = np.ascontiguousarray(rgba, np.uint8).tobytes()
                self._pix = QPixmap.fromImage(
                    QImage(buf, w, h, w * 4, QImage.Format_RGBA8888).copy())
            except Exception:
                self._pix = None
            self.update()

        def paintEvent(self, _):
            p = QPainter(self)
            cb = 8
            for yy in range(self.height() // cb + 1):
                for xx in range(self.width() // cb + 1):
                    p.fillRect(xx * cb, yy * cb, cb, cb,
                               QColor(58, 58, 64) if (xx + yy) & 1
                               else QColor(44, 44, 50))
            if self._pix and self._pix.width():
                s = min(self.width() / self._pix.width(),
                        self.height() / self._pix.height(), 1.0)
                dw, dh = self._pix.width() * s, self._pix.height() * s
                p.drawPixmap(
                    QRectF((self.width() - dw) / 2, (self.height() - dh) / 2, dw, dh),
                    self._pix, QRectF(self._pix.rect()))
            p.setPen(QColor(210, 210, 220))
            p.drawText(4, self.height() - 4, self._label)
            p.end()

    class SongTextureDialog(QDialog):
        """Generate the full per-song texture set.

        On *Save* sets ``self.result`` to ``{type_name: nut_bytes}`` for all
        templates provided (or discovered from test/music_texture); on cancel
        ``self.result`` stays ``None``.
        """

        def __init__(self, templates: dict | None = None, title: str = "",
                     lyricist: str = "", composer: str = "",
                     copyright: str = "", parent=None):
            super().__init__(parent)
            self.setWindowTitle("Per-song texture-set generator")
            self.resize(820, 640)
            if not templates:
                templates = load_test_templates("anp")
            self._templates = templates or {}
            self.result: dict | None = None
            self._previews: dict[str, _NutPreview] = {}
            self._build_ui(title, lyricist, composer, copyright)
            self._refresh()

        def _build_ui(self, title, lyricist, composer, copyright):
            lay = QVBoxLayout(self)

            form = QFormLayout()
            self.ed_title = QLineEdit(title)
            self.ed_lyr = QLineEdit(lyricist)
            self.ed_comp = QLineEdit(composer)
            self.ed_copy = QLineEdit(copyright)
            for w in (self.ed_title, self.ed_lyr, self.ed_comp, self.ed_copy):
                w.textChanged.connect(self._refresh)
            form.addRow("title 曲名:", self.ed_title)
            form.addRow("作詞 lyricist:", self.ed_lyr)
            form.addRow("作曲 composer:", self.ed_comp)
            form.addRow("© copyright:", self.ed_copy)
            lay.addLayout(form)

            lay.addWidget(QLabel("preview (white text on checkerboard):"))
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            grid_host = QWidget()
            grid = QGridLayout(grid_host)
            grid.setSpacing(10)
            cols = 2
            order = [t for t in TEXTURE_TYPES if t in self._templates] or TEXTURE_TYPES
            for i, t in enumerate(order):
                box = QFrame()
                box.setFrameShape(QFrame.StyledPanel)
                bl = QVBoxLayout(box)
                w, h = TYPE_DIMS.get(t, (220, 150))
                # cap preview cell for very tall vertical textures
                pw = min(max(w, 120), 260)
                ph = min(max(h, 60), 200)
                pv = _NutPreview(t, pw, ph)
                self._previews[t] = pv
                bl.addWidget(QLabel(f"{t}  ({w}x{h})"))
                bl.addWidget(pv)
                grid.addWidget(box, i // cols, i % cols)
            scroll.setWidget(grid_host)
            lay.addWidget(scroll, 1)

            btns = QHBoxLayout()
            btns.addStretch(1)
            b_save = QPushButton("Use these textures")
            b_save.clicked.connect(self._save)
            b_cancel = QPushButton("Cancel")
            b_cancel.clicked.connect(self.reject)
            btns.addWidget(b_save)
            btns.addWidget(b_cancel)
            lay.addLayout(btns)

        def _generate(self) -> dict:
            return generate_song_textures(
                self._templates, self.ed_title.text(), self.ed_lyr.text(),
                self.ed_comp.text(), self.ed_copy.text())

        def _refresh(self, *_):
            try:
                gen = self._generate()
            except Exception:
                return
            for t, nut in gen.items():
                pv = self._previews.get(t)
                if pv is not None:
                    pv.set_nut(nut)

        def _save(self):
            try:
                self.result = self._generate()
            except Exception as exc:
                QMessageBox.critical(self, "Generate failed", str(exc))
                return
            if not self.result:
                QMessageBox.warning(self, "Nothing generated",
                                    "No templates were available to generate.")
                return
            self.accept()

except ImportError:                                 # pragma: no cover
    SongTextureDialog = None  # type: ignore


# --------------------------------------------------------------------------- #
#  Self-test
# --------------------------------------------------------------------------- #
def _composite_on_dark(nut: bytes) -> Image.Image:
    """Decode a nut and composite over a dark grey background for visual check."""
    w, h, rgba = tim2.decode_tim2(nut)[0]
    fg = Image.fromarray(np.ascontiguousarray(rgba), "RGBA")
    bg = Image.new("RGBA", (w, h), (32, 32, 38, 255))
    bg.alpha_composite(fg)
    return bg.convert("RGB")


if __name__ == "__main__":
    import sys

    scratch = Path(r"C:\Users\User\AppData\Local\Temp\claude\D--"
                   r"\ee6b327c-e139-4924-9604-a691103f5eaa\scratchpad")
    scratch.mkdir(parents=True, exist_ok=True)

    test_title = "新曲テスト"
    test_lyr = "作詞太郎"
    test_comp = "作曲花子"
    test_copy = "© 2024 TEST"

    templates = load_test_templates("anp")
    if not templates:
        print("FAIL: no templates found under test/music_texture/")
        sys.exit(1)

    all_ok = True
    png_paths = []
    save_png_for = {"games", "kenri_song", "result", "topten",
                    "select_full", "select_non"}

    print(f"Found templates for: {sorted(templates)}")
    for t in TEXTURE_TYPES:
        tpl = templates.get(t)
        if tpl is None:
            print(f"  {t:13s}: SKIP (no template)")
            continue
        tlay = tim2.first_picture_layout(tpl)
        try:
            nut = render_texture(t, tpl, test_title, test_lyr, test_comp, test_copy)
        except Exception as exc:
            print(f"  {t:13s}: FAIL render error {exc}")
            all_ok = False
            continue

        glay = tim2.first_picture_layout(nut)
        same_size = (len(nut) == len(tpl))
        same_dims = (glay["width"] == tlay["width"]
                     and glay["height"] == tlay["height"])
        valid_tim2 = tim2.is_tim2(nut)
        # decode + count text pixels
        try:
            w, h, rgba = tim2.decode_tim2(nut)[0]
            text_px = int((rgba[:, :, 3] > 8).sum())
            decoded_ok = True
        except Exception as exc:
            decoded_ok = False
            text_px = 0
            print(f"  {t:13s}: decode error {exc}")

        ok = (valid_tim2 and same_size and same_dims and decoded_ok
              and text_px > 0)
        all_ok = all_ok and ok
        print(f"  {t:13s}: {'PASS' if ok else 'FAIL'} "
              f"dims={glay['width']}x{glay['height']} "
              f"len={len(nut)}=={len(tpl)}({same_size}) "
              f"text_px={text_px}")

        if t in save_png_for:
            png = scratch / f"songtex_{t}.png"
            _composite_on_dark(nut).save(png)
            png_paths.append(str(png))

    # also exercise generate_song_textures aggregate
    gen = generate_song_textures(templates, test_title, test_lyr, test_comp, test_copy)
    agg_ok = all(len(gen[t]) == len(templates[t]) and tim2.is_tim2(gen[t])
                 for t in gen)
    all_ok = all_ok and agg_ok and len(gen) == len(templates)

    print(f"\ngenerate_song_textures: {len(gen)} textures, aggregate "
          f"{'PASS' if agg_ok else 'FAIL'}")
    print("\nSaved preview PNGs:")
    for p in png_paths:
        print(f"  {p}")

    print(f"\nOVERALL: {'PASS' if all_ok else 'FAIL'}")
    sys.exit(0 if all_ok else 1)

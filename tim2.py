#!/usr/bin/env python3
"""
Minimal TIM2 (.TM2 / PS2 ".nut") decoder -> RGBA, with PNG export.

Taiko SYSTEM256 ".nut" files are actually TIM2 images (magic "TIM2").
Supported image types: 16-bit (RGBA5551), 24-bit, 32-bit, 4-bit and 8-bit
indexed (with CSM1 palette unswizzle for 256-colour CLUTs).

Reference for the format: the well-documented PS2 TIM2 layout, as also handled
by marco-calautti/Rainbow's TIM2 segment.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np


def is_tim2(data: bytes) -> bool:
    return len(data) >= 4 and data[:4] == b"TIM2"


def _expand_alpha(a: np.ndarray) -> np.ndarray:
    """PS2 alpha is 0..128 (128 = opaque) -> scale to 0..255."""
    return np.minimum(255, a.astype(np.uint16) * 2).astype(np.uint8)


def _rgba5551(u: np.ndarray) -> np.ndarray:
    u = u.astype(np.uint32)
    out = np.empty((u.size, 4), np.uint8)
    out[:, 0] = ((u & 0x1F) * 255 // 31).astype(np.uint8)
    out[:, 1] = (((u >> 5) & 0x1F) * 255 // 31).astype(np.uint8)
    out[:, 2] = (((u >> 10) & 0x1F) * 255 // 31).astype(np.uint8)
    out[:, 3] = np.where(((u >> 15) & 1) == 1, 255, 0).astype(np.uint8)
    return out


def _clut_bpp(clut_type: int, clut_size: int, colors: int) -> int:
    """Return palette bytes-per-entry (2/3/4)."""
    fmt = clut_type & 0x3F
    if fmt == 1:
        return 2
    if fmt == 2:
        return 3
    if fmt == 3:
        return 4
    if colors:                      # infer from on-disk size
        per = clut_size // colors
        if per in (2, 3, 4):
            return per
    return 4


def _read_clut(clut: bytes, colors: int, clut_type: int, clut_size: int) -> np.ndarray:
    bpp = _clut_bpp(clut_type, clut_size, colors)
    # The slice below truncates to whole entries; a buffer shorter than
    # colors * bpp indicates genuine corruption rather than something to hide.
    if len(clut) < colors * bpp:
        raise ValueError(
            f"TIM2 CLUT truncated: have {len(clut)} bytes, "
            f"need {colors * bpp} for {colors} x {bpp}-byte entries")
    if bpp == 4:
        pal = np.frombuffer(clut[: colors * 4], np.uint8).reshape(-1, 4).copy()
        pal[:, 3] = _expand_alpha(pal[:, 3])
    elif bpp == 3:
        rgb = np.frombuffer(clut[: colors * 3], np.uint8).reshape(-1, 3)
        pal = np.empty((rgb.shape[0], 4), np.uint8)
        pal[:, :3] = rgb
        pal[:, 3] = 255
    else:  # 16-bit
        u = np.frombuffer(clut[: colors * 2], "<u2")
        pal = _rgba5551(u)
    return pal


def _unswizzle_clut256(pal: np.ndarray) -> np.ndarray:
    """CSM1 256-colour palette unswizzle: swap entries 8..15 <-> 16..23 per 32.

    Iterates every 32-entry block and only swaps blocks that are fully present,
    so a palette whose size isn't a multiple of 32 leaves its trailing partial
    block untouched instead of being skipped or mangled.
    """
    out = pal.copy()
    n = pal.shape[0]
    for i in range(0, n, 32):
        if i + 24 <= n:
            out[i + 8:i + 16] = pal[i + 16:i + 24]
            out[i + 16:i + 24] = pal[i + 8:i + 16]
    return out


def _decode_pixels(img: bytes, clut: bytes, w: int, h: int, image_type: int,
                   clut_type: int, clut_colors: int, clut_size: int) -> np.ndarray:
    n = w * h
    if image_type == 4 or image_type == 5:
        pal = _read_clut(clut, clut_colors, clut_type, clut_size)
        if image_type == 4:                      # 4-bit, two pixels per byte
            b = np.frombuffer(img[:(n + 1) // 2], np.uint8)
            idx = np.empty(b.size * 2, np.uint8)
            idx[0::2] = b & 0x0F
            idx[1::2] = b >> 4
            idx = idx[:n]
        else:                                    # 8-bit
            idx = np.frombuffer(img[:n], np.uint8)
            # 256-colour CLUTs are stored swizzled (CSM1) and must be
            # unswizzled to interleave entries 8..15 <-> 16..23 per 32-block.
            # In this TIM2 variant the low 6 bits of the CLUT-type byte are the
            # palette *pixel format* (see _clut_bpp), and bit 7 is the CSM flag:
            # clear => CSM1 (swizzled, unswizzle needed); set => CSM2/linear
            # (already in order, must NOT be touched). Real Taiko 8-bit nuts use
            # clut_type 0x03 (RGBA32, CSM1) and rely on this unswizzle.
            csm2_linear = bool(clut_type & 0x80)
            if clut_colors >= 256 and not csm2_linear:
                pal = _unswizzle_clut256(pal)
        idx = np.clip(idx, 0, pal.shape[0] - 1)
        rgba = pal[idx]
    elif image_type == 3:                        # 32-bit RGBA
        rgba = np.frombuffer(img[:n * 4], np.uint8).reshape(-1, 4).copy()
        rgba[:, 3] = _expand_alpha(rgba[:, 3])
    elif image_type == 2:                        # 24-bit RGB
        rgb = np.frombuffer(img[:n * 3], np.uint8).reshape(-1, 3)
        rgba = np.empty((n, 4), np.uint8)
        rgba[:, :3] = rgb
        rgba[:, 3] = 255
    elif image_type == 1:                        # 16-bit RGBA5551
        u = np.frombuffer(img[:n * 2], "<u2")
        rgba = _rgba5551(u)
    else:
        raise ValueError(f"unsupported TIM2 image_type {image_type}")
    # After the caller's bounds validation a short pixel buffer means the
    # declared image_size was too small for width*height — genuine corruption.
    if rgba.shape[0] < n:
        raise ValueError(
            f"TIM2 pixel data short: decoded {rgba.shape[0]} of {n} "
            f"pixels for {w}x{h} image_type {image_type}")
    return rgba[:n].reshape(h, w, 4)


def decode_tim2(data: bytes) -> list[tuple[int, int, np.ndarray]]:
    """Return a list of (width, height, rgba HxWx4 uint8) for every picture."""
    if not is_tim2(data):
        raise ValueError("not a TIM2 image")
    if len(data) < 8:
        raise ValueError("truncated TIM2 file header")
    format_b = data[5]
    pic_count = struct.unpack_from("<H", data, 6)[0]
    if pic_count == 0:
        raise ValueError("TIM2 declares 0 pictures")
    base = 0x80 if format_b == 1 else 0x10
    pics = []
    off = base
    for _ in range(pic_count):
        if off + 0x18 > len(data):
            raise ValueError(f"TIM2 picture header at off {off} past end of file")
        total_size, clut_size, image_size = struct.unpack_from("<III", data, off)
        header_size, clut_colors = struct.unpack_from("<HH", data, off + 12)
        clut_type = data[off + 0x12]
        image_type = data[off + 0x13]
        width, height = struct.unpack_from("<HH", data, off + 0x14)
        # Validate the declared section sizes before trusting them as slices:
        # Python slicing never raises, so a bogus header would otherwise yield a
        # silently truncated (zero-padded) decode that masks corruption.
        if header_size < 0x30 or image_size < 0 or clut_size < 0:
            raise ValueError(f"invalid TIM2 picture header at off {off}")
        img_start = off + header_size
        if img_start + image_size + clut_size > len(data):
            raise ValueError(f"TIM2 picture at off {off} extends past end of file")
        img = data[img_start: img_start + image_size]
        clut = data[img_start + image_size: img_start + image_size + clut_size]
        rgba = _decode_pixels(img, clut, width, height, image_type,
                              clut_type, clut_colors, clut_size)
        pics.append((width, height, rgba))
        if not total_size:
            break
        # The stride must cover at least this picture's own sections, otherwise
        # the next picture offset would overlap/desync.
        if total_size < header_size + image_size + clut_size:
            raise ValueError(
                f"TIM2 total_size {total_size} at off {off} smaller than "
                f"its sections ({header_size}+{image_size}+{clut_size})")
        off += total_size
    if not pics:
        raise ValueError("no pictures decoded from TIM2")
    return pics


def tim2_summary(data: bytes) -> str:
    """One-line-per-field human summary of a TIM2 header (for the Info tab)."""
    if not is_tim2(data) or len(data) < 8:
        raise ValueError("truncated TIM2 header")
    format_b = data[5]
    pic_count = struct.unpack_from("<H", data, 6)[0]
    base = 0x80 if format_b == 1 else 0x10
    if len(data) < base + 0x18:
        raise ValueError("truncated TIM2 header")
    total_size, clut_size, image_size = struct.unpack_from("<III", data, base)
    header_size, clut_colors = struct.unpack_from("<HH", data, base + 12)
    clut_type = data[base + 0x12]
    image_type = data[base + 0x13]
    width, height = struct.unpack_from("<HH", data, base + 0x14)
    names = {1: "16bit RGBA5551", 2: "24bit RGB", 3: "32bit RGBA",
             4: "4bit indexed", 5: "8bit indexed"}
    return (f"TIM2 · {pic_count} picture(s)\n"
            f"size        : {width}x{height}\n"
            f"image_type  : {image_type} ({names.get(image_type, '?')})\n"
            f"clut        : {clut_colors} colors, type 0x{clut_type:02X}\n"
            f"image bytes : {image_size:,}  clut bytes: {clut_size:,}")


def save_png(pics, path_base: Path) -> list[Path]:
    """Save decoded pictures as PNG. Returns the written paths."""
    from PIL import Image
    path_base = Path(path_base)
    written = []
    multi = len(pics) > 1
    for i, (w, h, rgba) in enumerate(pics):
        im = Image.fromarray(np.ascontiguousarray(rgba), "RGBA")
        if multi:
            out = path_base.with_name(f"{path_base.stem}_{i}.png")
        else:
            out = path_base.with_suffix(".png")
        im.save(out)
        written.append(out)
    return written


def convert_nut_bytes_to_png(data: bytes, out_nut_path: Path) -> list[Path]:
    """Decode a .nut (TIM2) blob and write PNG(s) next to out_nut_path."""
    pics = decode_tim2(data)
    return save_png(pics, Path(out_nut_path).with_suffix(""))


# --------------------------------------------------------------------------- #
#  Encoding — template-based, for generating new textures
# --------------------------------------------------------------------------- #
def first_picture_layout(data: bytes) -> dict:
    """Return the first TIM2 picture's section offsets/sizes and pixel format."""
    if not is_tim2(data):
        raise ValueError("not a TIM2 image")
    if len(data) < 8:
        raise ValueError("truncated TIM2 header")
    format_b = data[5]
    base = 0x80 if format_b == 1 else 0x10
    if len(data) < base + 0x18:
        raise ValueError("truncated TIM2 header")
    total_size, clut_size, image_size = struct.unpack_from("<III", data, base)
    header_size, clut_colors = struct.unpack_from("<HH", data, base + 12)
    clut_type = data[base + 0x12]
    image_type = data[base + 0x13]
    width, height = struct.unpack_from("<HH", data, base + 0x14)
    img_start = base + header_size
    return {
        "base": base, "img_start": img_start, "image_size": image_size,
        "clut_start": img_start + image_size, "clut_size": clut_size,
        "clut_colors": clut_colors, "clut_type": clut_type,
        "image_type": image_type, "width": width, "height": height,
    }


def encode_indexed4_into_template(template: bytes, indices, palette_rgba) -> bytes:
    """Splice 4-bit indices + a 16-colour RGBA palette into a TIM2 template.

    `template` must be a 4-bit-indexed (image_type 4), 16-colour TIM2 of the same
    width/height as `indices`. Only the pixel and CLUT regions are replaced; the
    header, GS registers and sizes are kept verbatim, so the result is a valid
    .nut the game accepts. `palette_rgba` is 16x4 uint8 with PS2 alpha (0..128).
    """
    import numpy as np
    lay = first_picture_layout(template)
    if lay["image_type"] != 4:
        raise ValueError("template must be a 4-bit indexed (image_type 4) TIM2")
    w, h = lay["width"], lay["height"]
    idx = np.asarray(indices, dtype=np.uint8).reshape(h, w)
    flat = idx.reshape(-1)
    if flat.size % 2:
        flat = np.append(flat, np.uint8(0))
    packed = ((flat[1::2] << 4) | (flat[0::2] & 0x0F)).astype(np.uint8).tobytes()
    pal = np.asarray(palette_rgba, dtype=np.uint8).reshape(-1, 4)
    if pal.shape[0] < lay["clut_colors"]:
        pad = np.zeros((lay["clut_colors"] - pal.shape[0], 4), np.uint8)
        pal = np.vstack([pal, pad])
    clut = pal[: lay["clut_colors"]].tobytes()

    out = bytearray(template)
    if len(packed) != lay["image_size"]:
        raise ValueError(f"packed image {len(packed)} != template image_size {lay['image_size']}")
    out[lay["img_start"]:lay["img_start"] + lay["image_size"]] = packed
    out[lay["clut_start"]:lay["clut_start"] + lay["clut_size"]] = clut[: lay["clut_size"]]
    return bytes(out)

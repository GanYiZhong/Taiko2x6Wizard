"""Select title textures: transparent rows cut, groups compacted (heap fix)."""
import struct

import numpy as np

from taiko2x6.archive.archive_model import Archive
from taiko2x6.archive import taiko256_archive_tool_v2 as core
from taiko2x6.graphics import tim2

from conftest import build_archive


def _tim2(w, h, ink_rows):
    """A 4-bit TIM2 like the select titles: 0x50-byte picture header,
    colour 0 transparent, colour 1 opaque white on `ink_rows`."""
    idx = np.zeros((h, w), np.uint8)
    for r in ink_rows:
        idx[r, 3:7] = 1
    pixels = ((idx[:, 1::2] << 4) | idx[:, 0::2]).astype(np.uint8).tobytes()
    clut = bytes(4) + bytes([255, 255, 255, 128]) + bytes(4 * 14)
    head = bytearray(0x50)
    struct.pack_into("<IIIHHBBBBHH", head, 0, 0x50 + len(pixels) + len(clut),
                     len(clut), len(pixels), 0x50, 16, 0, 1, 3, 4, w, h)
    return b"TIM2\x04\x00\x01\x00" + bytes(8) + bytes(head) + pixels + clut


def _rgba(data):
    return tim2.decode_tim2(data)[0][2]


def test_crop_keeps_every_visible_row():
    src = _tim2(48, 264, [0, 5, 41])
    out = tim2.crop_transparent_bottom(src)
    lay = tim2.first_picture_layout(out)
    assert lay["height"] == 48                      # 42 rows, rounded up to 8
    assert len(out) % 16 == 0 and lay["image_size"] % 16 == 0
    assert np.array_equal(_rgba(out), _rgba(src)[:48])
    assert not _rgba(src)[48:, :, 3].any()          # only blank rows went


def test_crop_leaves_full_or_other_pictures_alone():
    assert tim2.crop_transparent_bottom(_tim2(48, 264, [260])) is None
    assert tim2.crop_transparent_bottom(b"not a tim2") is None


def _find(a, gname):
    return next(g for g in a.layout.groups if g["name"] == gname)


def test_compacted_group_shrinks_its_decompressed_size(tmp_path):
    files = [(f"select_non_s{i}", _tim2(48, 264, [0, 30])) for i in range(4)]
    lb, db = build_archive([("music_texture.music_select_music", files),
                            ("bins", [("x.bin", b"x" * 64)])])
    lp, dp = tmp_path / "list.bin", tmp_path / "DATA.000"
    lp.write_bytes(lb)
    dp.write_bytes(db)
    from taiko2x6.songs import omnimix_maker as O
    a = Archive(lp, dp)
    before = _find(a, "music_texture.music_select_music")["unpacked_size"]
    assert O.trim_title_textures(a) > 0
    a.save(tmp_path / "l2", tmp_path / "d2", allow_relayout=True)
    a.close()
    b = Archive(tmp_path / "l2", tmp_path / "d2")
    try:
        g = _find(b, "music_texture.music_select_music")
        assert g["unpacked_size"] < before / 4      # 264 -> 32 rows
        payload = b.decode_group(g, use_cache=False)
        assert g["unpacked_size"] == len(payload)
        assert g["unknown2"] == core.compute_unknown2(payload)
        for (name, src), e in zip(files, b.layout.files_for_group(g)):
            assert e["name"] == name and e["offset"] % 16 == 0
            got = b.read_file(g, e)
            assert np.array_equal(_rgba(got), _rgba(src)[:32])
        assert b.read_file(_find(b, "bins"), b.layout.files_for_group(_find(b, "bins"))[0]) == b"x" * 64
    finally:
        b.close()

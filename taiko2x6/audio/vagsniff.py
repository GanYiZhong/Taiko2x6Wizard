"""Cheap VAG-container sniffing, kept free of numpy/Qt so the Explorer can
classify a file without importing the audio player."""
from __future__ import annotations

import struct


def is_vag(data: bytes) -> bool:
    """Heuristic: the game's interleaved-VAG container header.

    Accept interleave in (0, 0x8000): decode_vag tolerates interleave == 0 by
    treating it as the standard block size, so is_vag must classify the same
    files as decodable (kept in sync with vagtool.decode_vag).
    """
    if len(data) < 0x10:
        return False
    interleave, _size, ch, rate = struct.unpack_from("<4I", data, 0)
    return interleave in (0, 0x8000) and ch in (1, 2) and 8000 <= rate <= 48000

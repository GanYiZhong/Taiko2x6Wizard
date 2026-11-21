"""Taiko 7 .sht charts -> the Gen2 (Taiko 8+) layout.

Taiko 7 uses an older chart layout. Copied verbatim into a Taiko 8..14 archive
the newer engine misreads it: it takes the integer BPM 96 as a float (~1e-43),
and every measure record after the first is misaligned. Reversed by comparing
the same charts in Taiko 7 and Taiko 8 (e.g. fumen.anp1p_e):

    header (16 bytes)      identical
    note record (16 bytes) identical
    measure record         T7 = 112 bytes, T8+ = 136 bytes (tja2sht.TRACK_NEW_SIZE)

    T7 measure                     T8+ measure
    i32 time   (ms)           ->   f32 time
    i32 bpm                   ->   f32 bpm
    i32 trackLine                  i32 trackLine
    i32 gogoFlag                   i32 gogoFlag
    i32 bunkis[6]                  i32 bunkis[6]
                                   f32 scrollSpeeds[6]   (T7 has none: 1.0)
    {i32 st, cnt, pts}[6]          {i32 st, cnt, pts}[6]

So the conversion only widens the measure table: no note moves, and the header
noteOffset is recomputed. T7 stores time and BPM as whole numbers, which is
exact for T7's own audio (the T8 re-releases of some songs carry refined float
timings for their re-cut audio; a T7 song keeps T7's audio, so T7's timing).
"""
from __future__ import annotations

import struct

from taiko2x6.charts import tja2sht

TRACK_T7_SIZE = 112
_HDR = tja2sht.HEADER_SIZE
_NOTE = tja2sht.NOTE_SIZE


def _layout(data: bytes):
    """(track_count, note_offset, note_count, record_size) or None."""
    if len(data) < _HDR:
        return None
    count, note_off, notes, pad = struct.unpack_from("<IIii", data, 0)
    region = note_off - _HDR
    if count == 0 or region <= 0 or region % count or notes < 0:
        return None
    if note_off + notes * _NOTE > len(data):
        return None
    return count, note_off, notes, region // count


def is_t7_sht(data: bytes) -> bool:
    """True if `data` is a Taiko 7 (112-byte measure record) chart."""
    lay = _layout(data)
    return bool(lay) and lay[3] == TRACK_T7_SIZE


def t7_to_sht(data: bytes) -> bytes:
    """Convert a Taiko 7 chart to the Gen2 (T8+) 136-byte measure layout."""
    lay = _layout(data)
    if not lay or lay[3] != TRACK_T7_SIZE:
        raise ValueError("not a Taiko 7 chart (measure record is not %d bytes)"
                         % TRACK_T7_SIZE)
    count, note_off, notes, _rec = lay
    pad = struct.unpack_from("<i", data, 12)[0]
    new_note_off = _HDR + count * tja2sht.TRACK_NEW_SIZE
    out = bytearray(struct.pack("<IIii", count, new_note_off, notes, pad))
    off = _HDR
    for _ in range(count):
        time, bpm, line, gogo = struct.unpack_from("<4i", data, off)
        if bpm <= 0:
            raise ValueError("measure at 0x%x has bpm %d" % (off, bpm))
        out += struct.pack("<ffii", float(time), float(bpm), line, gogo)
        out += data[off + 16: off + 40]                 # bunkis[6]
        out += struct.pack("<6f", *([1.0] * 6))         # scrollSpeeds
        out += data[off + 40: off + TRACK_T7_SIZE]      # subtracks[6]
        off += TRACK_T7_SIZE
    out += data[note_off:]                              # notes + any tail
    return bytes(out)


def ensure_gen2(data: bytes) -> tuple[bytes, bool]:
    """(chart in the Gen2 layout, converted?) - leaves non-T7 charts alone."""
    if is_t7_sht(data):
        return t7_to_sht(data), True
    return data, False

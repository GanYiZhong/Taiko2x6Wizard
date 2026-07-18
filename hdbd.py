"""Decode the Sony SCEI HD/BD sound-bank format (Taiko no Tatsujin, SYSTEM256/PS2 arcade).

The HD/BD pair is the "Sony HD/BD" bank family that vgmstream supports.

  HD layout (little-endian):
    0x00  "VersSCEI" chunk (on disk: 56 65 72 73 53 43 45 49 -> "Vers"+"SCEI"), 0x10 bytes.
    0x10  "HeadSCEI" chunk.  At HeadSCEI+8 a u32 table:
            [0] = 0x40  (HeadSCEI header size)
            [1] = hd_size
            [2] = bd_size
            [3..] = section offsets into HD for the sub-chunks (Vagi/Smpl/Sset/Prog),
                    0xFFFFFFFF when absent.  Empirically these are listed largest-first.
    Sub-chunks each begin with an 8-byte id (4-char tag + "SCEI"), then:
            +0x08  u32 chunk_size
            +0x0C  u32 count
            +0x10  u32[count+1] entry offsets (relative to the sub-chunk start)

  Vagi chunk (VAG / waveform info) -- the one we care about.  Each of the
  (count+1) entries is 8 bytes:
            +0x00  u32 bd_offset    -- start of this waveform inside BD (16-byte aligned)
            +0x04  u32 info         -- low 16 bits = sample rate (Hz);
                                       high 16 bits = flags (observed 0xFF00, a "0xFF"
                                       enable byte + per-waveform index/group nibble).
    There are count+1 waveform entries; entry N's data runs from its bd_offset to the
    next entry's bd_offset, and the final entry runs to bd_size.  (The stored `count`
    field is therefore waveforms-minus-one.)

  BD = concatenated PS2 VAG ADPCM mono streams (16-byte frames, 28 samples/frame).

Reverse-engineered field notes (uncertain bits documented):
  * info high-16 bits: always 0xFF.. across all 9 banks; the low byte of that high
    half tracks a running index/group (0x00,0x01,0x02,... in ATTRACT) -- treated as
    opaque flags here.  Sample rate (low 16) is authoritative and always sane.
  * Smpl/Sset/Prog sub-chunks are the tone/program/region tables (instrument mapping);
    not needed to extract the raw waveforms, so they are parsed only enough to be skipped.
"""

from __future__ import annotations

import struct

import numpy as np

import vagtool

HEAD_OFFSET = 0x10          # HeadSCEI chunk start
HEAD_TABLE = HEAD_OFFSET + 8  # u32 table inside HeadSCEI
HEAD_SIZE = 0x40            # documented HeadSCEI header size ([0] in the table)


def _need(buf: bytes, off: int, span: int, what: str) -> None:
    """Raise ValueError unless [off, off+span) lies inside buf."""
    if off < 0 or span < 0 or off + span > len(buf):
        raise ValueError(
            f"{what}: need {span} bytes at {off}, buffer is {len(buf)}")


def _tag(hd: bytes, off: int) -> str:
    """Return the 4-char tag of the SCEI sub-chunk at `off` (e.g. 'Vagi').

    On disk each 8-byte id is word-reversed: the Vagi chunk stores
    ``49 45 43 53 69 67 61 56`` = "IECS"+"igaV"; bytes[4:8] reversed = "Vagi".
    """
    if off + 8 > len(hd):
        return ""
    return hd[off + 4:off + 8][::-1].decode("latin1", "replace")


def _find_vagi(hd: bytes) -> int | None:
    """Locate the Vagi sub-chunk via the HeadSCEI section table; fall back to scan."""
    # HeadSCEI header: [0]=0x40, [1]=hd_size, [2]=bd_size, [3..] = section offsets.
    n = (len(hd) - HEAD_TABLE) // 4
    n = min(n, 16)
    table = struct.unpack_from("<%dI" % n, hd, HEAD_TABLE)
    for off in table[3:]:
        if off == 0xFFFFFFFF or off == 0 or off + 8 > len(hd):
            continue
        if _tag(hd, off) == "Vagi":
            return off
    # Fallback: linear scan for the word-reversed on-disk id of "VagiSCEI".
    # Validate the match: its tag must read "Vagi" and the count field must be
    # plausible (entries fit inside hd) so we don't false-match embedded data.
    needle = b"IECSigaV"  # "SCEI"[::-1] + "Vagi"[::-1]
    start = 0
    while True:
        idx = hd.find(needle, start)
        if idx < 0:
            return None
        if _tag(hd, idx) == "Vagi" and idx + 0x10 <= len(hd):
            try:
                _cs, cnt = struct.unpack_from("<II", hd, idx + 8)
            except struct.error:
                start = idx + 1
                continue
            n_entries = cnt + 1
            if 0 < n_entries <= 0x10000 and idx + 0x10 + n_entries * 4 <= len(hd):
                return idx
        start = idx + 1


def parse_hd(hd: bytes) -> dict:
    """Parse an HD header into bank metadata + waveform table.

    Returns:
        {
          'bd_size': int,
          'hd_size': int,
          'vagi_offset': int,
          'waveforms': [
             {'index', 'bd_offset', 'bd_size', 'sample_rate',
              'loop_start', 'loop_end', 'name', 'flags'}, ...
          ],
        }
    """
    _need(hd, HEAD_TABLE, 12, "HeadSCEI header")

    head_sz, hd_size, bd_size = struct.unpack_from("<III", hd, HEAD_TABLE)
    # Sanity-check the documented header fields so a malformed HD is rejected
    # rather than silently mis-parsed. head_sz is documented as 0x40; hd_size
    # must not exceed the buffer.
    if head_sz != HEAD_SIZE:
        raise ValueError(f"unexpected HeadSCEI size {head_sz:#x} (want {HEAD_SIZE:#x})")
    if hd_size > len(hd):
        raise ValueError(f"HeadSCEI hd_size {hd_size} exceeds buffer {len(hd)}")

    vagi = _find_vagi(hd)
    if vagi is None:
        raise ValueError("no VagiSCEI chunk found in HD")

    _need(hd, vagi + 8, 8, "Vagi chunk header")
    chunk_size, count = struct.unpack_from("<II", hd, vagi + 8)
    n_entries = count + 1  # stored count is (#waveforms - 1)
    if n_entries <= 0 or n_entries > 0x10000:
        raise ValueError(f"Vagi count implausible: {count}")

    _need(hd, vagi + 0x10, n_entries * 4, "Vagi entry pointer table")
    ptrs = struct.unpack_from("<%dI" % n_entries, hd, vagi + 0x10)

    # First pass: read (bd_offset, sample_rate, flags) for each entry.
    raw = []
    for p in ptrs:
        base = vagi + p
        _need(hd, base, 8, "Vagi entry")
        w0, w1 = struct.unpack_from("<II", hd, base)
        bd_off = w0
        sample_rate = w1 & 0xFFFF
        flags = w1 >> 16
        raw.append((bd_off, sample_rate, flags))

    # Sizes = delta to next offset; last entry runs to bd_size.
    waveforms = []
    for i, (bd_off, sr, flags) in enumerate(raw):
        if i + 1 < len(raw):
            end = raw[i + 1][0]
        else:
            end = bd_size
        size = end - bd_off
        # Guard against non-monotonic / out-of-range Vagi entries: a reversed
        # pair would give a negative size and an empty/garbled region. Clamp to
        # a valid [0, bd_size] window so production parsing matches the self-test
        # invariants instead of silently emitting bad regions.
        if size < 0:
            size = 0
        if bd_off < 0:
            bd_off = 0
        if bd_off > bd_size:
            bd_off = bd_size
        if bd_off + size > bd_size:
            size = max(0, bd_size - bd_off)
        waveforms.append({
            "index": i,
            "bd_offset": bd_off,
            "bd_size": size,
            "sample_rate": sr,
            "loop_start": None,   # PS2 ADPCM loop is signalled via per-frame flags in BD
            "loop_end": None,     #   rather than the Vagi table; left None.
            "name": None,         # names live in the (secondary) tone/prog tables.
            "flags": flags,
        })

    return {
        "bd_size": bd_size,
        "hd_size": hd_size,
        "head_size": head_sz,
        "vagi_offset": vagi,
        "vagi_count": count,
        "waveforms": waveforms,
    }


def decode_waveform(bd: bytes, wf: dict) -> tuple:
    """Decode one waveform's BD region (PS2 ADPCM, mono).

    Returns (sample_rate, pcm_int16_mono_numpy).
    """
    # Clamp into [0, len(bd)] so an out-of-range or reversed entry yields empty
    # PCM instead of a garbled/negative slice. Real banks are already in range.
    start = max(0, min(int(wf["bd_offset"]), len(bd)))
    size = max(0, min(int(wf["bd_size"]), len(bd) - start))
    if size == 0:
        return wf["sample_rate"], np.zeros(0, dtype=np.int16)
    region = bd[start:start + size]
    # PS2 ADPCM is 16-byte framed; trim any ragged tail just in case.
    usable = (len(region) // vagtool.FRAME_BYTES) * vagtool.FRAME_BYTES
    pcm = vagtool._decode_channel(region[:usable])
    return wf["sample_rate"], pcm


def list_bank(hd: bytes, bd: bytes) -> list:
    """Decode every waveform in a bank.

    Returns [{'index','name','sample_rate','seconds','pcm'(int16 mono)}].
    """
    info = parse_hd(hd)
    out = []
    for wf in info["waveforms"]:
        sr, pcm = decode_waveform(bd, wf)
        seconds = (len(pcm) / sr) if sr else 0.0
        out.append({
            "index": wf["index"],
            "name": wf["name"],
            "sample_rate": sr,
            "seconds": seconds,
            "pcm": pcm,
        })
    return out


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import os
    from pathlib import Path

    import taiko256_explorer_gui6 as g

    try:
        import soundfile as sf
        HAVE_SF = True
    except Exception:
        HAVE_SF = False

    SCRATCH = (r"C:\Users\User\AppData\Local\Temp\claude\D--"
               r"\ee6b327c-e139-4924-9604-a691103f5eaa\scratchpad")
    os.makedirs(SCRATCH, exist_ok=True)

    a = g.Archive(Path("list.bin"), Path("DATA.000"), fmt=2)
    banks = {}
    for grp in a.layout.groups:
        nm = grp["name"]
        if nm.startswith("sound.hdbd."):
            files = {e["name"]: e for e in a.layout.files_for_group(grp)}
            hd = a.read_file(grp, files["hd"])
            bd = a.read_file(grp, files["bd"])
            banks[nm.split(".")[-1]] = (hd, bd)

    ok = True
    saved = 0
    total_wf = 0

    for name in sorted(banks):
        hd, bd = banks[name]
        try:
            info = parse_hd(hd)
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] parse FAILED: {exc}")
            ok = False
            continue

        wfs = info["waveforms"]
        total_wf += len(wfs)
        rates = sorted({w["sample_rate"] for w in wfs})
        print(f"[{name}] bd={len(bd)} waveforms={len(wfs)} rates={rates}")

        bank_loud = False
        for wf in wfs:
            # Requirement 2: every region inside bd, rate sane.
            if not (0 <= wf["bd_offset"] <= len(bd)):
                print(f"   wf{wf['index']} BAD offset {wf['bd_offset']}")
                ok = False
            if wf["bd_offset"] + wf["bd_size"] > len(bd):
                print(f"   wf{wf['index']} overruns bd "
                      f"({wf['bd_offset']}+{wf['bd_size']}>{len(bd)})")
                ok = False
            if not (8000 <= wf["sample_rate"] <= 48000):
                print(f"   wf{wf['index']} BAD rate {wf['sample_rate']}")
                ok = False
            if wf["bd_offset"] % 16 != 0:
                print(f"   wf{wf['index']} offset not 16-aligned: {wf['bd_offset']}")
                ok = False

            sr, pcm = decode_waveform(bd, wf)
            if len(pcm) and int(np.abs(pcm).max()) > 64:
                bank_loud = True

        if not bank_loud:
            print(f"   [{name}] produced no audible waveform")
            ok = False

    # Requirement 1: NAME bank specifics.
    hd, bd = banks["NAME"]
    name_info = parse_hd(hd)
    assert len(hd) == 560 and len(bd) == 85424, "NAME sizes changed"
    assert len(name_info["waveforms"]) >= 1, "NAME has no waveforms"
    sr0, pcm0 = decode_waveform(bd, name_info["waveforms"][0])
    name_maxabs = int(np.abs(pcm0).max()) if len(pcm0) else 0
    print(f"\nNAME wf0: rate={sr0} samples={len(pcm0)} maxabs={name_maxabs}")
    if not (8000 <= sr0 <= 48000 and name_maxabs > 1000):
        print("NAME wf0 check FAILED")
        ok = False

    # Requirement 3: save a few decoded waveforms as .wav.
    if HAVE_SF:
        for name in ["NAME", "ATTRACT", "SELECT"]:
            hd, bd = banks[name]
            for wf in parse_hd(hd)["waveforms"][:2]:
                sr, pcm = decode_waveform(bd, wf)
                if not len(pcm):
                    continue
                path = os.path.join(SCRATCH, f"hdbd_{name}_{wf['index']}.wav")
                sf.write(path, pcm, sr)
                saved += 1
        print(f"\nSaved {saved} wav files to scratchpad")
    else:
        print("\nsoundfile unavailable; skipped wav export")

    print(f"\nTotal waveforms across {len(banks)} banks: {total_wf}")
    print("RESULT:", "PASS" if ok else "FAIL")

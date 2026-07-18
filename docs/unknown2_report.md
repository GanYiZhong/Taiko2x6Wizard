# Taiko no Tatsujin (PS2) LIST.BIN `unknown2` — Reverse-Engineering Report

## 1. Cracked?

**YES — fully cracked and verified against all 1686 groups (100% match).**

## 2. Exact algorithm spec

`unknown2` is **not** a CRC, adler, FNV, murmur, or any multiplicative hash. It is a
**salted SHA-1 truncation**:

```
unknown2 = little_endian_u32( SHA1( SALT || decompressed_payload )[0:4] )
```

| Item | Value |
|------|-------|
| Hash primitive | Standard **SHA-1** (standard IV incl. C3D2E1F0; byte-oriented) |
| Buffer hashed | The group's **DECOMPRESSED payload** — exactly `unpacked_size` bytes. (For fumen groups this is the `sht` file zero-padded to a 16-byte boundary, which is what the game inflates and holds in RAM. NOT the compressed block, NOT the on-disk crypted block, NOT sht-without-pad.) |
| Salt / seed | Fixed **5-byte prefix** `b"nULIb"` = bytes `6e 55 4c 49 62`, fed to SHA-1 **before** the payload: `SHA1_Update(salt,5)` then `SHA1_Update(payload,len)` |
| Polynomial / multiplier | None — it is SHA-1, so no CRC poly / no multiplier constant |
| Byte vs word | Byte-oriented (SHA-1) |
| Endianness of output | Take `digest[0:4]` and read it as a **little-endian** u32 |
| Final XOR | None |
| Applies to | ALL group types (fumen `sht`, `nut` textures, `vag`, …), both compression modes (2 and 6) |

Notes / red herrings ruled out along the way:
- The binary **does** contain a standard zlib CRC-32 (reflected poly `0xEDB88320`), table at
  file `0x23b3c8` stored at **8-byte stride** (which is why naive 4-byte-stride table scans miss
  it), `crc32()` at file `0x7d408`. This CRC is used **only by zlib's own gzip path** and is
  **never** used for `unknown2`.
- The on-disk block is XOR-crypted with a 256-byte key; equivalently `descramble(block)` where
  `buf[i] ^= (~((i>>4)&0xA5))&0xFF` for `i` in steps of 16 reproduces the decrypted zlib stream.
  This descrambled/compressed stream is **not** the hashed buffer — the hashed buffer is the
  *decompressed* content.

## 3. Standalone Python function

```python
import hashlib, struct

SALT = b"nULIb"  # 5 bytes: 6e 55 4c 49 62

def compute_unknown2(decompressed_payload: bytes) -> int:
    """
    decompressed_payload = the group's inflated content (unpacked_size bytes;
    for fumen groups = sht bytes padded to a 16-byte boundary).
    Returns the 32-bit unknown2 value stored at group record offset +0x1C.
    """
    digest = hashlib.sha1(SALT + decompressed_payload).digest()
    return struct.unpack('<I', digest[:4])[0]
```

Usage with the existing tool:

```python
import taiko256_archive_tool_v2 as core
from pathlib import Path
lay = core.load_layout(Path('list.bin'), 2)
raw = open('DATA.000', 'rb').read()
g = lay.groups[i]
payload = core.decode_group_payload(raw, g)   # decompressed payload
assert compute_unknown2(payload) == (g['unknown2'] & 0xffffffff)
```

## 4. Verification

- Tested against **ALL 1686 groups** of NM00033 (not just a sample):
  - 676 fumen `sht` groups (compression 2)
  - `music_texture.songlevel_*` and other `nut` texture groups
  - `vag` audio and other types
  - both compression modes present (2 and 6)
- **Result: 1686 / 1686 matched, 0 mismatches (100%).**
- Also confirmed identical for both `payload` (full) and `payload[:unpacked_size]` (they are equal).
- Spot checks passed exactly:
  - `fumen.10tai1p_e`  → `0x95D285E2`
  - `fumen.10tai1p_h`  → `0xF2274431`
  - `fumen.10tai1p_m`  → `0x113DD352`
  - `fumen.porno1p_e`  → `0x523D9A2B`
  - `fumen.nada1p_e` and `fumen.nada2p_e` (content-identical) both → `0x0E54B0FB`
  - `music_texture.songlevel_10tai` (nut) → `0xC23C8865`

## 5. File offsets (binary `TA8GAME.dec`; file offset = runtime VA − 0x100000)

| Component | File offset | Runtime VA |
|-----------|-------------|------------|
| Salted-SHA-1 wrapper `sha1_salted(ctx, buf, len)` | **0x99898** | 0x199898 |
| SHA-1 Init + salt feed `SHA1_Init; SHA1_Update(ctx, gp-0x58b8, 5)` | **0x997a0** | 0x1997a0 |
| `addiu t7, gp, -0x58b8` (salt pointer) | 0x997b8 | 0x1997b8 |
| Salt bytes `"nULIb"` (null-terminated, .data) | **0x24c9b8** | 0x34c9b8 |
| SHA-1 Init | 0x96060 | 0x196060 |
| SHA-1 transform (block compress) | 0x960e4 | 0x1960e4 |
| SHA-1 Update (core) | 0x99800 | 0x199800 |
| SHA-1 Update (public) | 0x99448 | 0x199448 |
| SHA-1 Final | 0x99850 | 0x199850 |
| **Validator compare site** (in `getFile`, `flag&4` branch) | **0x3e70** | 0x103e70 |
| `getFile` function start | 0x3c68 | 0x103c68 |
| Digest read as LE u32 | 0x3e6c | 0x103e6c |
| Stored `unknown2` load (record + 0x1C) | 0x3e64 | 0x103e64 |

`gp = 0x352270`, so `gp - 0x58b8 = 0x34c9b8` (file 0x24c9b8) = the salt — file-resident in .data.

### Validator behavior
`getFile` (0x103c68), in the `record[+0x0C] & 4` branch (0x103d24), loads the stored
`unknown2` from group record offset `+0x1C` (0x103e64), computes
`LE_u32(SHA1("nULIb" || decompressed_payload)[0:4])` via the salted-SHA-1 wrapper (0x199898),
and `beq`-compares the two at **0x103e70**. On mismatch the game does not proceed to use the
group (observed behavior: freeze/hang), which is why corrupting only `unknown2` freezes the game.

## Reproducing the salt discovery
The salt is initialized in file-resident .data (not BSS). It was recovered by an exhaustive
whole-binary scan: for a small-payload group, testing every 5-byte window `w` in the image for
`LE_u32(SHA1(w || decompressed_payload)[0:4]) == unknown2` uniquely yielded
`w = 6e 55 4c 49 62 = "nULIb"` at file 0x24c9b8, consistent with the `gp-0x58b8` pointer used by
the SHA-1 init routine at 0x1997a0.

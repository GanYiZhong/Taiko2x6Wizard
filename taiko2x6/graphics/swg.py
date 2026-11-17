#!/usr/bin/env python3
"""
SWG (objSWGFileObject) container parser for Taiko SYSTEM256 archives.

SWG is Namco's serialized 2D scene/layout/animation format. One ".swg" per
archive group ties that group's TIM2 (".nut") textures into an on-screen
layout. The format is a flattened object graph: array fields are stored as
(absolute_offset, element_count) pairs, strings live in a pool referenced by
pointer tables, transforms are 4x4 float matrices.

This module reverse-engineers the format *empirically* from the clean archive
data (the game executable dump available for static RE is byte-corrupted, so it
is not trusted). The parse is non-destructive: the original bytes are kept and
edits are patched in place, so re-serialisation is byte-exact unless a value is
deliberately changed.

The full object graph (`_SYM_` pointer tables + the `0x48` root) is NOT yet
reverse-engineered, so strings and matrices are discovered with conservative
heuristic scans. Because those scans cannot *prove* the true slot length of a
string or that a float block is genuinely a transform, every edit path is
deliberately defensive:

  * String slots only ever claim the bytes the string provably owns (its own
    characters plus its single terminating null). Trailing padding that might
    belong to a following field is never absorbed, and length-changing edits to
    these pointer-delimited strings are refused (the pointer tables that would
    need rewriting are not parsed yet).
  * Heuristically discovered matrices are flagged `verified=False`; the editor
    must gate editing of unverified matrices.

Confirmed layout (offsets from file start):
  0x00  char[4]   magic "SWG\0"
  0x04  u32       format constant (0x7782D30F across all files; not per-file)
  0x08  char[64]  object name (null padded)
  0x48  ...       root object: (offset,count) pointer pairs + leaf data
  0x50  u16,u16   screen width, height (e.g. 640x480)
  ....  "_SYM_"   symbol section: groups of (string_ptr_table, count)
  ....  pool      null-terminated ASCII symbol/element names
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from pathlib import Path


MAGIC = b"SWG\0"
NAME_OFFSET = 0x08
NAME_SIZE = 0x40
RES_OFFSET = 0x50
BODY_OFFSET = 0x48

# Plausible screen-resolution bounds, used to gate the (unconfirmed) 0x50 field.
RES_MIN = 1
RES_MAX = 8192


class SwgError(ValueError):
    """Raised for malformed input or refused (unsafe) edits."""


def is_swg(data: bytes) -> bool:
    return len(data) >= 8 and data[:4] == MAGIC


@dataclass
class SwgString:
    offset: int          # absolute offset of the string bytes in the file
    text: str
    # capacity == number of bytes this string provably owns: len(text) + 1 for
    # the single terminating null. Trailing padding past the terminator is NOT
    # claimed, because it may belong to the next field/offset-table entry.
    capacity: int


@dataclass
class SwgFloat:
    offset: int
    value: float


@dataclass
class SwgMatrix:
    offset: int          # absolute offset of 16 floats (row-major 4x4)
    values: list         # 16 floats
    # Heuristic scans cannot prove a float block is a real transform. Until the
    # object graph is parsed, scanned matrices are unverified and the editor
    # must not let edits to them corrupt non-matrix data.
    verified: bool = False

    # element [12],[13],[14] are the translation; [0],[5],[10] the scale
    @property
    def tx(self) -> float: return self.values[12]
    @property
    def ty(self) -> float: return self.values[13]
    @property
    def tz(self) -> float: return self.values[14]
    @property
    def sx(self) -> float: return self.values[0]
    @property
    def sy(self) -> float: return self.values[5]


@dataclass
class SwgFile:
    raw: bytearray
    format_const: int
    name: str
    width: int
    height: int
    name_raw: bytes = b""                                # original 64-byte name field
    strings: list = field(default_factory=list)         # list[SwgString]
    matrices: list = field(default_factory=list)         # list[SwgMatrix]

    # ---- parsing --------------------------------------------------------
    @classmethod
    def parse(cls, data: bytes) -> "SwgFile":
        if not is_swg(data):
            raise SwgError("not an SWG file (bad magic)")
        # parse reads through 0x53 (the u16,u16 resolution at 0x50); guard
        # against a valid-magic but truncated file rather than raising an
        # opaque struct.error deep inside.
        if len(data) < RES_OFFSET + 4:
            raise SwgError(
                f"truncated SWG: need at least {RES_OFFSET + 4} bytes, got {len(data)}")
        raw = bytearray(data)
        format_const = struct.unpack_from("<I", raw, 0x04)[0]
        name_raw = bytes(raw[NAME_OFFSET:NAME_OFFSET + NAME_SIZE])
        name = name_raw.split(b"\0", 1)[0].decode("ascii", "replace")
        width, height = struct.unpack_from("<HH", raw, RES_OFFSET)
        self = cls(raw=raw, format_const=format_const, name=name,
                   width=width, height=height, name_raw=name_raw)
        self.strings = self._extract_strings()
        self.matrices = self._extract_matrices()
        return self

    def _extract_strings(self) -> list:
        """Collect null-terminated ASCII strings in the body (offset >= 0x48).

        capacity is intentionally limited to the bytes the string provably
        owns: its characters plus exactly one terminating null. Trailing
        padding past the terminator is *not* counted, because a following field
        (e.g. a little-endian float/int/offset whose low bytes are zero) can
        begin with 0x00 and must never be absorbed into this string's slot.
        """
        raw = self.raw
        out = []
        i = BODY_OFFSET
        n = len(raw)
        while i < n:
            b = raw[i]
            if 0x20 <= b < 0x7F:
                j = i
                while j < n and 0x20 <= raw[j] < 0x7F:
                    j += 1
                if j < n and raw[j] == 0 and (j - i) >= 2:
                    text = raw[i:j].decode("ascii")
                    # proven slot = string bytes + the single terminating null
                    out.append(SwgString(offset=i, text=text, capacity=(j - i) + 1))
                    i = j + 1
                else:
                    i = j
            else:
                i += 1
        return out

    def _extract_matrices(self) -> list:
        """Find 4x4 float blocks that look like affine transforms.

        Scan starts at BODY_OFFSET (0x48), never inside the header. Matches are
        marked unverified because a blind float scan cannot prove a block is a
        real transform; the editor gates editing accordingly.
        """
        raw = self.raw
        out = []
        n = len(raw)
        o = BODY_OFFSET                       # start at the body, not in the header
        while o + 64 <= n:
            vals = struct.unpack_from("<16f", raw, o)
            if _looks_like_matrix(vals):
                out.append(SwgMatrix(offset=o, values=list(vals), verified=False))
                o += 64
            else:
                o += 4                         # 4-byte (float) stride, not 16
        return out

    # ---- editing (in place, byte-exact for everything untouched) --------
    def set_resolution(self, width: int, height: int):
        if not (RES_MIN <= width <= RES_MAX) or not (RES_MIN <= height <= RES_MAX):
            raise SwgError(
                f"resolution {width}x{height} out of plausible range "
                f"[{RES_MIN}..{RES_MAX}]")
        struct.pack_into("<HH", self.raw, RES_OFFSET, width, height)
        self.width, self.height = width, height

    def set_name(self, name: str):
        """Rewrite the 64-byte name slot.

        Validates ASCII + length, preserves any original bytes past the new
        name's terminator so trailing slot data is not blindly zeroed.
        """
        try:
            b = name.encode("ascii")
        except UnicodeEncodeError as exc:
            raise SwgError(f"name must be ASCII (offending char: {exc.object[exc.start]!r})")
        if b"\0" in b:
            raise SwgError("name must not contain a null byte")
        if len(b) >= NAME_SIZE:
            raise SwgError(f"name too long (max {NAME_SIZE - 1} bytes)")
        # Preserve the tail of the original field beyond the new terminator,
        # so any legitimate trailing slot data survives a name-only change.
        new_field = bytearray(self.name_raw)
        if len(new_field) != NAME_SIZE:
            new_field = bytearray(NAME_SIZE)
        new_field[:len(b)] = b
        new_field[len(b)] = 0                  # terminator for the new name
        self.raw[NAME_OFFSET:NAME_OFFSET + NAME_SIZE] = new_field
        self.name = name
        self.name_raw = bytes(new_field)

    def _check_bounds(self, offset: int, size: int):
        if offset < 0 or offset + size > len(self.raw):
            raise SwgError(
                f"offset 0x{offset:X}+{size} out of bounds (file is {len(self.raw)} bytes)")

    def set_float(self, offset: int, value: float):
        if not math.isfinite(value):
            raise SwgError("refusing to write non-finite float")
        self._check_bounds(offset, 4)
        struct.pack_into("<f", self.raw, offset, value)

    def set_matrix_translation(self, mat_offset: int, x: float, y: float):
        if not (math.isfinite(x) and math.isfinite(y)):
            raise SwgError("refusing to write non-finite translation")
        self._check_bounds(mat_offset, 64)
        struct.pack_into("<f", self.raw, mat_offset + 12 * 4, x)
        struct.pack_into("<f", self.raw, mat_offset + 13 * 4, y)
        for m in self.matrices:
            if m.offset == mat_offset:
                m.values[12], m.values[13] = x, y

    def set_matrix_scale(self, mat_offset: int, sx: float, sy: float):
        if not (math.isfinite(sx) and math.isfinite(sy)):
            raise SwgError("refusing to write non-finite scale")
        self._check_bounds(mat_offset, 64)
        struct.pack_into("<f", self.raw, mat_offset + 0 * 4, sx)
        struct.pack_into("<f", self.raw, mat_offset + 5 * 4, sy)
        for m in self.matrices:
            if m.offset == mat_offset:
                m.values[0], m.values[5] = sx, sy

    def set_u32(self, offset: int, value: int):
        if not (0 <= value <= 0xFFFFFFFF):
            raise SwgError(f"u32 value {value} out of range")
        self._check_bounds(offset, 4)
        struct.pack_into("<I", self.raw, offset, value)

    def set_string(self, offset: int, text: str):
        """Overwrite a pointer-delimited string in place.

        These strings have an unproven slot length (the `_SYM_`/pointer tables
        that would describe the true extent are not parsed yet), so we never
        write past the bytes the string provably owns and we refuse length
        changes — growing or shrinking would leave referencing pointers/counts
        stale and could clobber the following field. An edit must therefore be
        the same byte length as the original string.
        """
        s = next((x for x in self.strings if x.offset == offset), None)
        if s is None:
            raise SwgError(f"no string tracked at 0x{offset:X}")
        try:
            b = text.encode("ascii")
        except UnicodeEncodeError as exc:
            raise SwgError(f"string must be ASCII (offending char: {exc.object[exc.start]!r})")
        if b"\0" in b:
            raise SwgError("string must not contain a null byte")
        old_len = s.capacity - 1               # original string byte length
        if len(b) != old_len:
            raise SwgError(
                f"length change not allowed for pointer-delimited string: "
                f"'{text}' is {len(b)} bytes but slot holds exactly {old_len} "
                f"(pointer rewrite is not implemented)")
        # write only the proven slot [offset, offset+capacity): chars + terminator
        self._check_bounds(offset, s.capacity)
        self.raw[offset:offset + len(b)] = b
        self.raw[offset + len(b)] = 0          # preserve the terminator
        s.text = text

    def repack(self) -> bytes:
        return bytes(self.raw)


def _looks_like_matrix(vals) -> bool:
    """Conservative affine-transform predicate.

    A blind float scan inevitably yields false positives (vertex arrays, color
    tables, keyframes). This tightened heuristic requires the structure of an
    affine 2D/3D transform: a finite, sanely-bounded block whose bottom row is
    (0,0,0,1) and whose diagonal contains 1.0 entries. Even so, matches are
    surfaced as `verified=False`; callers must gate edits.
    """
    for v in vals:
        if not math.isfinite(v) or abs(v) > 1e6:
            return False
    # bottom row of an affine 4x4 is exactly (0, 0, 0, 1)
    if not (vals[3] == 0.0 and vals[7] == 0.0 and vals[11] == 0.0):
        return False
    if abs(vals[15] - 1.0) > 1e-6:
        return False
    # diagonal scales must be nonzero and at least one must be a clean 1.0
    diag = [vals[0], vals[5], vals[10]]
    if any(d == 0.0 for d in diag):
        return False
    ones = sum(1 for d in diag if abs(d - 1.0) < 1e-6)
    if ones < 1:
        return False
    # a translation-or-identity transform: require some structure beyond zeros
    nonzero = sum(1 for v in vals if v != 0.0)
    return nonzero >= 5


def load_swg(path) -> SwgFile:
    return SwgFile.parse(Path(path).read_bytes())

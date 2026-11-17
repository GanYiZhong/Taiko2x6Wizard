#!/usr/bin/env python3
"""
Extractor / safer patcher for namco SYSTEM256 PS2-base Taiko archives using LIST.BIN + DATA.000.

This v2 keeps the original DATA.000 sector layout for patching, which is safer on real SYSTEM256.

Confirmed properties for Taiko 7-style LIST.BIN:
  - LIST.BIN is XORed byte-by-byte with a 256-byte key.
  - DATA.000 blocks are XORed at byte 0 of every 16-byte chunk.
  - compression type 2 = zlib stream, usually header 78 DA / zlib level 9.
  - compression type 6 = raw/stored.

Typical usage:
  python taiko256_archive_tool_v2.py parse --list LIST.BIN --format 2
  python taiko256_archive_tool_v2.py extract --list LIST.BIN --data DATA.000 --out extracted --format 2

Recommended for real hardware after editing files:
  python taiko256_archive_tool_v2.py patch --list LIST.BIN --data DATA.000 --src extracted --out-list LIST.new.BIN --out-data DATA.new.000 --format 2

The patch command:
  - copies original DATA.000 first;
  - leaves unchanged groups byte-for-byte unchanged;
  - keeps original group sectors;
  - keeps original file offsets inside each decompressed group;
  - keeps original decompressed group size;
  - only updates file size fields and packed_size for groups that changed;
  - refuses files that no longer fit their original in-group slot unless --allow-relayout is used.

This is intentionally conservative because real hardware may depend on layout details that a simple full repack changes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import struct
import sys
import zlib
from typing import Iterable

KEY = bytes([
    0xFF,0xFE,0xFF,0xFE,0xFB,0xFA,0xFB,0xFA,0xFF,0xFE,0xFF,0xFE,0xFB,0xFA,0xFB,0xFA,
    0xFF,0xFE,0xFF,0xFE,0xFB,0xFA,0xFB,0xFA,0xFF,0xFE,0xFF,0xFE,0xFB,0xFA,0xFB,0xFA,
    0xDF,0xDE,0xDF,0xDE,0xDB,0xDA,0xDB,0xDA,0xDF,0xDE,0xDF,0xDE,0xDB,0xDA,0xDB,0xDA,
    0xDF,0xDE,0xDF,0xDE,0xDB,0xDA,0xDB,0xDA,0xDF,0xDE,0xDF,0xDE,0xDB,0xDA,0xDB,0xDA,
    0xFF,0xFE,0xFF,0xFE,0xFB,0xFA,0xFB,0xFA,0xFF,0xFE,0xFF,0xFE,0xFB,0xFA,0xFB,0xFA,
    0xFF,0xFE,0xFF,0xFE,0xFB,0xFA,0xFB,0xFA,0xFF,0xFE,0xFF,0xFE,0xFB,0xFA,0xFB,0xFA,
    0xDF,0xDE,0xDF,0xDE,0xDB,0xDA,0xDB,0xDA,0xDF,0xDE,0xDF,0xDE,0xDB,0xDA,0xDB,0xDA,
    0xDF,0xDE,0xDF,0xDE,0xDB,0xDA,0xDB,0xDA,0xDF,0xDE,0xDF,0xDE,0xDB,0xDA,0xDB,0xDA,
    0x7F,0x7E,0x7F,0x7E,0x7B,0x7A,0x7B,0x7A,0x7F,0x7E,0x7F,0x7E,0x7B,0x7A,0x7B,0x7A,
    0x7F,0x7E,0x7F,0x7E,0x7B,0x7A,0x7B,0x7A,0x7F,0x7E,0x7F,0x7E,0x7B,0x7A,0x7B,0x7A,
    0x5F,0x5E,0x5F,0x5E,0x5B,0x5A,0x5B,0x5A,0x5F,0x5E,0x5F,0x5E,0x5B,0x5A,0x5B,0x5A,
    0x5F,0x5E,0x5F,0x5E,0x5B,0x5A,0x5B,0x5A,0x5F,0x5E,0x5F,0x5E,0x5B,0x5A,0x5B,0x5A,
    0x7F,0x7E,0x7F,0x7E,0x7B,0x7A,0x7B,0x7A,0x7F,0x7E,0x7F,0x7E,0x7B,0x7A,0x7B,0x7A,
    0x7F,0x7E,0x7F,0x7E,0x7B,0x7A,0x7B,0x7A,0x7F,0x7E,0x7F,0x7E,0x7B,0x7A,0x7B,0x7A,
    0x5F,0x5E,0x5F,0x5E,0x5B,0x5A,0x5B,0x5A,0x5F,0x5E,0x5F,0x5E,0x5B,0x5A,0x5B,0x5A,
    0x5F,0x5E,0x5F,0x5E,0x5B,0x5A,0x5B,0x5A,0x5F,0x5E,0x5F,0x5E,0x5B,0x5A,0x5B,0x5A,
])

SECTOR_SIZE = 2048
DATA_ALIGN = 16
PAYLOAD_TRAILER_SIZE = 32
PAYLOAD_ALIGN = 64


def align(n: int, a: int) -> int:
    return (n + a - 1) // a * a


def crypt_list(data: bytes) -> bytes:
    return bytes(b ^ KEY[i & 0xFF] for i, b in enumerate(data))


def crypt_data_block(data: bytes) -> bytes:
    out = bytearray(data)
    for chunk_index in range((len(out) + 15) // 16):
        out[chunk_index * 16] ^= KEY[chunk_index & 0xFF]
    return bytes(out)


def zlib_compress_level9(data: bytes) -> bytes:
    # Original blocks observed in Taiko 7 start with zlib header 78 DA, i.e. max compression.
    co = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=zlib.MAX_WBITS)
    return co.compress(data) + co.flush()


def read_cstr(buf: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(buf):
        raise ValueError(f"string offset out of range: 0x{offset:X}")
    end = buf.find(b"\0", offset)
    if end < 0:
        raise ValueError(f"unterminated string at 0x{offset:X}")
    # Decode as latin-1 (1:1 byte<->codepoint) so names round-trip byte-exactly
    # even when they contain non-ASCII bytes. A strict-ascii "replace" decode would
    # map non-ASCII to U+FFFD and break the rebuild re-encode.
    return buf[offset:end].decode("latin-1")


def group_to_path(group_name: str) -> Path:
    return Path(*group_name.split("."))


class ArchiveLayout:
    def __init__(self, decoded_list: bytes, fmt: int):
        if fmt not in (1, 2):
            raise ValueError("format must be 1 or 2")
        self.decoded_list = decoded_list
        self.format = fmt
        self.group_count, self.file_count = struct.unpack_from("<II", decoded_list, 0)
        if fmt == 1:
            # Format-1 base/name offsets were never verified against a real
            # format-1 LIST.BIN and are structurally inconsistent with the
            # confirmed format-2 packing. Rather than silently read garbage
            # names/sizes, refuse until the layout is validated against a sample.
            raise NotImplementedError(
                "format 1 layout is unverified and disabled; only format 2 is "
                "confirmed. Provide a real format-1 LIST.BIN to validate the offsets."
            )
        else:
            self.group_base = 32
            self.group_size = 32
            self.file_base = (self.group_count + 1) * 32
            self.name_base = (self.group_count * 2 + self.file_count + 2) * 16
        if self.name_base >= len(decoded_list):
            raise ValueError(f"computed name table offset outside LIST.BIN: 0x{self.name_base:X} >= 0x{len(decoded_list):X}")
        self.groups = self._parse_groups()
        self.files = self._parse_files()

    def _parse_groups(self) -> list[dict]:
        groups = []
        for index in range(self.group_count):
            off = self.group_base + index * self.group_size
            if self.format == 1:
                name_off, count, first_file, comp, sector, packed_size, unk1 = struct.unpack_from("<7I", self.decoded_list, off)
                unk2 = 0
            else:
                name_off, count, first_file, comp, sector, packed_size, unk1, unk2 = struct.unpack_from("<8I", self.decoded_list, off)
            groups.append({
                "index": index,
                "name_offset": name_off,
                "name": read_cstr(self.decoded_list, self.name_base + name_off),
                "file_count": count,
                "first_file": first_file,
                "compression": comp,
                "sector": sector,
                "packed_size": packed_size,
                "unpacked_size": unk1,
                "unknown2": unk2,
                "list_offset": off,
            })
        return groups

    def _parse_files(self) -> list[dict]:
        files = []
        for index in range(self.file_count):
            off = self.file_base + index * 16
            name_off, size, data_offset, unk = struct.unpack_from("<4I", self.decoded_list, off)
            files.append({
                "index": index,
                "name_offset": name_off,
                "name": read_cstr(self.decoded_list, self.name_base + name_off),
                "size": size,
                "offset": data_offset,
                "unknown": unk,
                "list_offset": off,
            })
        return files

    def files_for_group(self, group: dict) -> list[dict]:
        return self.files[group["first_file"]:group["first_file"] + group["file_count"]]

    def write_group(self, buf: bytearray, group: dict) -> None:
        off = self.group_base + group["index"] * self.group_size
        if self.format == 1:
            struct.pack_into(
                "<7I", buf, off,
                group["name_offset"], group["file_count"], group["first_file"], group["compression"],
                group["sector"], group["packed_size"], group["unpacked_size"],
            )
        else:
            struct.pack_into(
                "<8I", buf, off,
                group["name_offset"], group["file_count"], group["first_file"], group["compression"],
                group["sector"], group["packed_size"], group["unpacked_size"], group["unknown2"],
            )

    def write_file(self, buf: bytearray, entry: dict) -> None:
        off = self.file_base + entry["index"] * 16
        struct.pack_into("<4I", buf, off, entry["name_offset"], entry["size"], entry["offset"], entry["unknown"])


def load_layout(list_path: Path, fmt: int) -> ArchiveLayout:
    return ArchiveLayout(crypt_list(list_path.read_bytes()), fmt)


# Upper bound on a single group's decompressed payload. Real Taiko groups are a
# few MB; this cap guards against a corrupt/hostile packed_size triggering an
# unbounded zlib expansion (zip-bomb / OOM). Tune up if a legitimate group ever
# exceeds it.
MAX_DECOMPRESSED_GROUP = 256 * 1024 * 1024


def get_group_encoded_block(data_bytes, group: dict) -> bytes:
    start = group["sector"] * SECTOR_SIZE
    end = start + group["packed_size"]
    if start < 0 or end < start:
        raise ValueError(f"invalid group block range for group {group['index']} {group['name']}: 0x{start:X}..0x{end:X}")
    if end > len(data_bytes):
        raise EOFError(f"DATA too short for group {group['index']} {group['name']}: needs 0x{end:X}, size 0x{len(data_bytes):X}")
    return bytes(data_bytes[start:end])


def _bounded_decompress(block: bytes, group: dict) -> bytes:
    """zlib decompress with an output cap to avoid unbounded expansion."""
    d = zlib.decompressobj()
    out = d.decompress(block, MAX_DECOMPRESSED_GROUP)
    if d.unconsumed_tail:
        raise ValueError(
            f"decompressed group exceeds {MAX_DECOMPRESSED_GROUP} byte cap: "
            f"group {group['index']} {group['name']}"
        )
    out += d.flush()
    return out


def decode_group_payload(data_bytes, group: dict) -> bytes:
    """Decode one group's payload. `data_bytes` may be bytes, bytearray, or mmap
    (any sliceable buffer)."""
    block = crypt_data_block(get_group_encoded_block(data_bytes, group))
    if group["compression"] == 2:
        return _bounded_decompress(block, group)
    if group["compression"] == 6:
        return block
    raise ValueError(f"unsupported compression type {group['compression']} in group {group['index']} {group['name']}")


def encode_group_payload(payload: bytes, comp: int) -> bytes:
    if comp == 2:
        return crypt_data_block(zlib_compress_level9(payload))
    if comp == 6:
        return crypt_data_block(payload)
    raise ValueError(f"unsupported compression type {comp}")


# The 8th u32 of each group record ("unknown2") is a content integrity hash the
# game VALIDATES at load time: it recomputes this over the DECOMPRESSED group
# payload and, on mismatch, freezes/black-screens the song (confirmed by
# experiment + reversed from TA8GAME). It is the first 4 bytes (little-endian)
# of SHA-1 over a fixed 5-byte salt "nULIb" followed by the payload. Any rebuilt
# or edited group MUST write the correct value here or the game will hang.
# Verified against all 1686 groups of a retail LIST.BIN/DATA.000.
_UNKNOWN2_SALT = b"nULIb"


def compute_unknown2(payload: bytes) -> int:
    import hashlib
    return struct.unpack("<I", hashlib.sha1(_UNKNOWN2_SALT + payload).digest()[:4])[0]


def sorted_group_starts(layout: ArchiveLayout) -> list[int]:
    """Sorted, de-duplicated list of group start byte-offsets (sector positions)."""
    return sorted({g["sector"] * SECTOR_SIZE for g in layout.groups})


def group_capacity_bytes(layout: ArchiveLayout, data_len: int, group: dict, starts: list[int] | None = None) -> int:
    """Maximum bytes this group may occupy without moving its sector, based on the
    next group by sector or EOF.

    Pass a precomputed `starts` (from sorted_group_starts) to avoid an O(n log n)
    re-sort on every call when patching many groups.
    """
    if starts is None:
        starts = sorted_group_starts(layout)
    my_start = group["sector"] * SECTOR_SIZE
    # First start strictly greater than my_start (bisect over the sorted list).
    import bisect
    idx = bisect.bisect_right(starts, my_start)
    end = starts[idx] if idx < len(starts) else data_len
    return end - my_start


def command_parse(args: argparse.Namespace) -> int:
    layout = load_layout(args.list, args.format)
    print(f"format      : {layout.format}")
    print(f"groups      : {layout.group_count}")
    print(f"files       : {layout.file_count}")
    print(f"group table : 0x{layout.group_base:X}")
    print(f"file table  : 0x{layout.file_base:X}")
    print(f"name table  : 0x{layout.name_base:X}")
    comps = {}
    for g in layout.groups:
        comps[g["compression"]] = comps.get(g["compression"], 0) + 1
    print(f"compression : {comps}")
    print("\nFirst groups:")
    for g in layout.groups[:args.limit]:
        print(f"[{g['index']:04d}] {g['name']} files={g['file_count']} first={g['first_file']} comp={g['compression']} sector={g['sector']} packed={g['packed_size']} unpacked={g['unpacked_size']}")
    if args.dump_decoded:
        args.dump_decoded.write_bytes(layout.decoded_list)
        print(f"\nWrote decoded LIST to: {args.dump_decoded}")
    return 0


def command_extract(args: argparse.Namespace) -> int:
    layout = load_layout(args.list, args.format)
    data = args.data.read_bytes()
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = {"format": layout.format, "source_list": str(args.list), "source_data": str(args.data), "groups": []}
    extracted_groups = extracted_files = skipped_groups = 0
    only = args.only.lower() if args.only else None
    for group in layout.groups:
        if only and only not in group["name"].lower():
            continue
        try:
            payload = decode_group_payload(data, group)
        except (EOFError, zlib.error, ValueError) as exc:
            # EOFError: group lies outside the supplied DATA file.
            # zlib.error / ValueError: corrupt or empty (packed_size==0) zlib stream.
            # With --partial, skip the offending group instead of aborting the run.
            if args.partial:
                print(f"skip: {exc}", file=sys.stderr)
                skipped_groups += 1
                continue
            raise
        group_dir = args.out / group_to_path(group["name"])
        group_dir.mkdir(parents=True, exist_ok=True)
        group_record = {k: group[k] for k in ["index", "name", "file_count", "first_file", "compression", "sector", "packed_size", "unpacked_size", "unknown2"]}
        group_record["files"] = []
        for entry in layout.files_for_group(group):
            start = entry["offset"]
            end = start + entry["size"]
            if end > len(payload):
                raise ValueError(f"file entry outside payload: group={group['name']} file={entry['name']} range=0x{start:X}..0x{end:X}, payload=0x{len(payload):X}")
            out_path = group_dir / entry["name"]
            out_path.write_bytes(payload[start:end])
            group_record["files"].append({
                "index": entry["index"], "name": entry["name"], "size": entry["size"],
                "offset": entry["offset"], "unknown": entry["unknown"], "path": str(out_path.relative_to(args.out)),
            })
            extracted_files += 1
        manifest["groups"].append(group_record)
        extracted_groups += 1
        if args.verbose:
            print(f"extracted [{group['index']:04d}] {group['name']} ({group['file_count']} files)")
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Extracted groups: {extracted_groups}, files: {extracted_files}, skipped groups: {skipped_groups}")
    print(f"Output: {args.out}")
    return 0


def file_slot_capacity(entries: list[dict], group_unpacked_size: int, i: int) -> int:
    e = entries[i]
    start = e["offset"]
    later_starts = [x["offset"] for x in entries if x["offset"] > start]
    end = min(later_starts) if later_starts else group_unpacked_size
    return end - start


def _assert_no_overlap(entries: list[dict], group: dict) -> None:
    """Refuse in-place patching when two file slots in a group alias or overlap.

    file_slot_capacity derives a slot's capacity from the next file by offset.
    If two entries share an offset or interleave, an in-place grow would zero-fill
    and clobber the aliased range. Detect that up front and refuse rather than
    silently corrupt data.
    """
    ranges = sorted(((e["offset"], e["offset"] + e["size"], e["name"]) for e in entries))
    for (s0, e0, n0), (s1, e1, n1) in zip(ranges, ranges[1:]):
        if s1 < e0:
            raise ValueError(
                f"overlapping/aliased file ranges in group {group['name']}: "
                f"{n0} [0x{s0:X}..0x{e0:X}) overlaps {n1} [0x{s1:X}..0x{e1:X}). "
                f"In-place patch is unsafe here; use --allow-relayout."
            )


def patch_group_payload_in_place(src_root: Path, group: dict, entries: list[dict], original_payload: bytes, allow_relayout: bool) -> tuple[bytes, bool, list[dict]]:
    """Return (new_payload, changed, updated_entries). Conservative mode keeps original offsets."""
    # An empty group (zero files) may never have been extracted; short-circuit
    # before requiring the directory to exist.
    if not entries:
        return bytes(original_payload), False, [dict(e) for e in entries]

    group_dir = src_root / group_to_path(group["name"])
    if not group_dir.is_dir():
        raise FileNotFoundError(f"missing extracted group directory: {group_dir}")

    # Copy entries so layout object is not mutated until we know patch is possible.
    updated_entries = [dict(e) for e in entries]
    payload = bytearray(original_payload)
    changed = False

    if not allow_relayout:
        _assert_no_overlap(updated_entries, group)
        for i, entry in enumerate(updated_entries):
            path = group_dir / entry["name"]
            if not path.is_file():
                raise FileNotFoundError(f"missing extracted file: {path}")
            new_file = path.read_bytes()
            start = entry["offset"]
            cap = file_slot_capacity(updated_entries, len(original_payload), i)
            if len(new_file) > cap:
                raise ValueError(
                    f"file no longer fits original slot: {path} size={len(new_file)} capacity={cap}. "
                    f"Keep it <= {cap} bytes, or retry with --allow-relayout."
                )
            old_file = bytes(payload[start:start + entry["size"]])
            if new_file != old_file or len(new_file) != entry["size"]:
                changed = True
                # Clear original occupied bytes and available gap up to next file to avoid stale data.
                payload[start:start + cap] = b"\0" * cap
                payload[start:start + len(new_file)] = new_file
                entry["size"] = len(new_file)
        return bytes(payload), changed, updated_entries

    # Less safe fallback: rebuild the group tightly but keep decompressed size if it fits.
    new_payload = bytearray()
    for entry in updated_entries:
        path = group_dir / entry["name"]
        if not path.is_file():
            raise FileNotFoundError(f"missing extracted file: {path}")
        new_file = path.read_bytes()
        new_off = align(len(new_payload), DATA_ALIGN)
        if new_off > len(new_payload):
            new_payload.extend(b"\0" * (new_off - len(new_payload)))
        entry["offset"] = new_off
        entry["size"] = len(new_file)
        new_payload.extend(new_file)
    if len(new_payload) > len(original_payload):
        raise ValueError(
            f"rebuilt group is larger than original decompressed group: group={group['name']} "
            f"new={len(new_payload)} original={len(original_payload)}. This tool will not grow groups in patch mode."
        )
    new_payload.extend(b"\0" * (len(original_payload) - len(new_payload)))
    return bytes(new_payload), bytes(new_payload) != original_payload, updated_entries


def command_patch(args: argparse.Namespace) -> int:
    layout = load_layout(args.list, args.format)
    original_data = args.data.read_bytes()
    new_data = bytearray(original_data)
    new_list = bytearray(layout.decoded_list)
    only = args.only.lower() if args.only else None

    changed_groups = unchanged_groups = skipped_groups = 0
    # Sector positions are fixed in conservative patch mode (groups never move),
    # so compute the sorted start list once instead of re-sorting per group.
    group_starts = sorted_group_starts(layout)
    for group in layout.groups:
        if only and only not in group["name"].lower():
            skipped_groups += 1
            continue
        entries = layout.files_for_group(group)
        original_payload = decode_group_payload(original_data, group)
        new_payload, changed, updated_entries = patch_group_payload_in_place(
            args.src, group, entries, original_payload, args.allow_relayout
        )
        if not changed:
            unchanged_groups += 1
            continue

        encoded = encode_group_payload(new_payload, group["compression"])
        max_bytes = group_capacity_bytes(layout, len(original_data), group, group_starts)
        if len(encoded) > max_bytes:
            raise ValueError(
                f"compressed group does not fit original DATA sector range: group={group['name']} "
                f"encoded={len(encoded)} max_without_moving={max_bytes}. "
                f"Reduce the edited file size or use a full relayout repacker for experiments."
            )

        start = group["sector"] * SECTOR_SIZE
        # Clear previous occupied region and then write the new encoded bytes.
        clear_len = max(group["packed_size"], len(encoded))
        new_data[start:start + clear_len] = b"\0" * clear_len
        new_data[start:start + len(encoded)] = encoded

        # Keep sector and unpacked_size. Only packed_size and file sizes/offsets change.
        group_to_write = dict(group)
        group_to_write["packed_size"] = len(encoded)
        group_to_write["unpacked_size"] = len(new_payload)
        group_to_write["unknown2"] = compute_unknown2(new_payload)  # game-validated hash
        layout.write_group(new_list, group_to_write)
        for e in updated_entries:
            layout.write_file(new_list, e)
        changed_groups += 1
        if args.verbose:
            print(f"patched [{group['index']:04d}] {group['name']} packed {group['packed_size']} -> {len(encoded)}")

    args.out_data.write_bytes(bytes(new_data))
    args.out_list.write_bytes(crypt_list(bytes(new_list)))
    print(f"Changed groups  : {changed_groups}")
    print(f"Unchanged groups: {unchanged_groups}")
    print(f"Skipped groups  : {skipped_groups}")
    print(f"Wrote: {args.out_list}")
    print(f"Wrote: {args.out_data}")
    return 0


def build_payload_from_files(src_root: Path, group: dict, entries: list[dict]) -> bytes:
    payload = bytearray()
    group_dir = src_root / group_to_path(group["name"])
    for entry in entries:
        path = group_dir / entry["name"]
        if not path.is_file():
            raise FileNotFoundError(f"missing extracted file: {path}")
        file_data = path.read_bytes()
        new_off = align(len(payload), DATA_ALIGN)
        if new_off > len(payload):
            payload.extend(b"\0" * (new_off - len(payload)))
        entry["offset"] = new_off
        entry["size"] = len(file_data)
        payload.extend(file_data)
    min_len = align(len(payload) + PAYLOAD_TRAILER_SIZE, PAYLOAD_ALIGN)
    if min_len > len(payload):
        payload.extend(b"\0" * (min_len - len(payload)))
    return bytes(payload)


def command_repack_experimental(args: argparse.Namespace) -> int:
    """Original full-relayout style repack, kept for experiments. Patch is recommended for hardware."""
    layout = load_layout(args.list, args.format)
    new_list = bytearray(layout.decoded_list)
    new_data = bytearray()
    for group in layout.groups:
        entries = [dict(e) for e in layout.files_for_group(group)]
        payload = build_payload_from_files(args.src, group, entries)
        group["unpacked_size"] = len(payload)
        packed = encode_group_payload(payload, group["compression"])
        sector = align(len(new_data), SECTOR_SIZE) // SECTOR_SIZE
        if sector * SECTOR_SIZE > len(new_data):
            new_data.extend(b"\0" * (sector * SECTOR_SIZE - len(new_data)))
        group["sector"] = sector
        group["packed_size"] = len(packed)
        group["unknown2"] = compute_unknown2(payload)  # game-validated hash
        new_data.extend(packed)
        layout.write_group(new_list, group)
        for entry in entries:
            layout.write_file(new_list, entry)
        if args.verbose:
            print(f"packed [{group['index']:04d}] {group['name']} unpacked={group['unpacked_size']} packed={group['packed_size']} sector={group['sector']}")
    args.out_data.write_bytes(bytes(new_data))
    args.out_list.write_bytes(crypt_list(bytes(new_list)))
    print("WARNING: this is a full DATA relayout. For real hardware, prefer the patch command.", file=sys.stderr)
    print(f"Wrote: {args.out_list}")
    print(f"Wrote: {args.out_data}")
    return 0


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Extract / patch Taiko SYSTEM256 LIST.BIN + DATA.000 archives")
    sub = p.add_subparsers(dest="command", required=True)

    pp = sub.add_parser("parse", help="decode LIST.BIN and print archive summary")
    pp.add_argument("--list", type=Path, required=True)
    pp.add_argument("--format", type=int, default=2, choices=(1, 2), help="2 = PS2 6/7/Anime Special")
    pp.add_argument("--limit", type=int, default=20)
    pp.add_argument("--dump-decoded", type=Path)
    pp.set_defaults(func=command_parse)

    ex = sub.add_parser("extract", help="extract files from DATA.000")
    ex.add_argument("--list", type=Path, required=True)
    ex.add_argument("--data", type=Path, required=True)
    ex.add_argument("--out", type=Path, required=True)
    ex.add_argument("--format", type=int, default=2, choices=(1, 2))
    ex.add_argument("--partial", action="store_true", help="skip groups outside the supplied DATA file")
    ex.add_argument("--only", help="extract only groups whose group name contains this substring")
    ex.add_argument("--verbose", action="store_true")
    ex.set_defaults(func=command_extract)

    pa = sub.add_parser("patch", help="safer hardware-oriented patch: preserve original DATA sector layout")
    pa.add_argument("--list", type=Path, required=True, help="original LIST.BIN")
    pa.add_argument("--data", type=Path, required=True, help="original full DATA.000")
    pa.add_argument("--src", type=Path, required=True, help="extracted/edited file tree")
    pa.add_argument("--out-list", type=Path, required=True)
    pa.add_argument("--out-data", type=Path, required=True)
    pa.add_argument("--format", type=int, default=2, choices=(1, 2))
    pa.add_argument("--only", help="patch only matching groups; other groups are copied unchanged")
    pa.add_argument("--allow-relayout", action="store_true", help="rebuild changed groups internally while preserving group sector/size; less safe")
    pa.add_argument("--verbose", action="store_true")
    pa.set_defaults(func=command_patch)

    rp = sub.add_parser("repack-experimental", help="full relayout repack; not recommended for real hardware")
    rp.add_argument("--list", type=Path, required=True)
    rp.add_argument("--src", type=Path, required=True)
    rp.add_argument("--out-list", type=Path, required=True)
    rp.add_argument("--out-data", type=Path, required=True)
    rp.add_argument("--format", type=int, default=2, choices=(1, 2))
    rp.add_argument("--verbose", action="store_true")
    rp.set_defaults(func=command_repack_experimental)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

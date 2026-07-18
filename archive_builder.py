#!/usr/bin/env python3
"""
Archive builder — rebuild LIST.BIN + DATA.000 with brand-new groups added.

The base tool can only *replace* existing files. To truly add a new song we must
create new groups (textures, charts) and new files. That changes the group/file
counts, so the whole LIST.BIN (header counts, group table, file table, name pool)
and DATA.000 (group payloads at fresh sectors) must be rebuilt.

LIST.BIN layout (format 2, decrypted), confirmed empirically:
  0x00  u32 group_count
  0x04  u32 file_count
  0x08  char[24]  build-timestamp string (preserved verbatim)
  0x20  group records, 32 bytes = 8×u32:
        name_offset, file_count, first_file, compression, sector, packed_size,
        unpacked_size, unknown2(payload SHA1 content-hash; VALIDATED at load —
        a wrong value freezes the game, see core.compute_unknown2)
  ....  file records, 16 bytes = 4×u32: name_offset, size, in_group_offset, unknown(=0)
  ....  name table: NUL-terminated ASCII, name_offset is relative to its start

Existing groups are copied verbatim (their compressed block is moved, not
recompressed); edited groups (staged in archive.replacements) are rebuilt; new
groups are built + compressed. Verified by: no-op rebuild re-reads every file,
and an added group's files read back exactly.
"""
from __future__ import annotations

import gc
import mmap
import struct
import zlib

import numpy as np

import taiko256_archive_tool_v2 as core

SECTOR = core.SECTOR_SIZE
_KEY = np.frombuffer(bytes(core.KEY), dtype=np.uint8)   # 256-byte XOR key

# Groups the game loads by ABSOLUTE sector (not via the LIST.BIN sector field),
# so they MUST keep their original sector even when adding a song grows them.
# These are the song-database groups; relocating them makes the game read stale
# sectors and fail to load (while plain "replace" works because it never moves
# them). Non-pinned groups colliding with a grown pinned group are evicted.
PINNED_GROUPS = frozenset({"gamedata", "soundinfo"})


def crypt_list_fast(data: bytes) -> bytes:
    """Vectorised equivalent of core.crypt_list (XOR with the 256-byte key).

    np.tile repeats the 256-byte key (ceil(size/256) copies) and is sliced to
    `size`, reproducing KEY[i & 0xFF] exactly. (np.resize would also work but its
    repeat-vs-pad semantics are a footgun, so np.tile is used for clarity.)
    """
    arr = np.frombuffer(data, dtype=np.uint8).copy()
    if arr.size:
        reps = (arr.size + _KEY.size - 1) // _KEY.size
        arr ^= np.tile(_KEY, reps)[:arr.size]
    return arr.tobytes()


# Round-trip invariant: the vectorised crypt must match the byte-by-byte core
# implementation, otherwise every rebuilt LIST.BIN would silently desync.
assert crypt_list_fast(bytes(range(256)) * 3) == core.crypt_list(bytes(range(256)) * 3)


def _assemble_payload(blobs: list) -> tuple:
    """Pack `blobs` (list[bytes]) at DATA_ALIGN boundaries with the trailing
    PAYLOAD_TRAILER_SIZE/PAYLOAD_ALIGN padding. Return (payload_bytes, offsets).

    Single source of truth for the group payload alignment + trailer math, shared
    by _build_payload (new groups) and the edited-group path so the two cannot
    drift apart.
    """
    payload = bytearray()
    offsets = []
    for data in blobs:
        off = core.align(len(payload), core.DATA_ALIGN)
        if off > len(payload):
            payload.extend(b"\0" * (off - len(payload)))
        offsets.append(off)
        payload.extend(data)
    min_len = core.align(len(payload) + core.PAYLOAD_TRAILER_SIZE, core.PAYLOAD_ALIGN)
    if min_len > len(payload):
        payload.extend(b"\0" * (min_len - len(payload)))
    return bytes(payload), offsets


def _build_payload(files: list) -> tuple:
    """files: list[(name, bytes)]. Return (payload_bytes, file_records)."""
    payload, offsets = _assemble_payload([data for _, data in files])
    recs = [{"name": fname, "size": len(data), "offset": off, "unknown": 0}
            for (fname, data), off in zip(files, offsets)]
    return payload, recs


def build_archive(archive, new_group_specs=None, extra_files=None) -> tuple:
    """Rebuild (list_bytes, data_bytes) for `archive` with additions applied.

    new_group_specs: list of {"name": str, "files": [(fname, bytes)], "compression": 2}
    extra_files: {existing_group_name: [(fname, bytes), ...]} to append files into
                 an existing group (e.g. select_full_<id> into music_texture.music_select)
    Staged edits in archive.replacements are also applied.

    Return contract (relied on by song_builder): (list_bytes: bytes, data_bytes: bytearray).
    Note on memory: the source DATA.000 is mmap-ed (not fully copied), but the
    rebuilt DATA.000 is materialised in a single in-RAM bytearray of size ~= the
    full output. Peak RAM is therefore roughly the output DATA.000 size, which can
    be multiple GB. Returning the full buffer is part of the contract; callers that
    need to bound memory must stream the returned bytes to disk promptly.
    """
    new_group_specs = new_group_specs or []
    extra_files = extra_files or {}
    layout = archive.layout

    name_table = bytearray(layout.decoded_list[layout.name_base:])
    # Reuse existing name-table offsets so re-running a rebuild does not grow the
    # table unboundedly. Seed it from the names already present in the layout.
    name_index: dict[bytes, int] = {}
    for grp in layout.groups:
        name_index.setdefault(grp["name"].encode("latin-1"), grp["name_offset"])
        for e in layout.files_for_group(grp):
            name_index.setdefault(e["name"].encode("latin-1"), e["name_offset"])

    def add_name(s: str) -> int:
        # Encode as latin-1 (raw bytes) so names round-trip byte-exactly even when
        # they contain non-ASCII bytes (read_cstr decodes via latin-1 to match).
        raw = s.encode("latin-1")
        existing = name_index.get(raw)
        if existing is not None:
            return existing
        off = len(name_table)
        name_table.extend(raw + b"\0")
        name_index[raw] = off
        return off

    # mmap the source DATA.000 — unchanged groups are referenced (not copied) and
    # paged in lazily, so we never hold a second full copy of the file in RAM.
    # Wrapped in try/finally: a leaked mmap holds a lock on DATA.000 on Windows
    # and blocks the next run.
    fsrc = open(archive.data_path, "rb")
    src = None
    src_mv = None
    dst = None
    try:
        src = mmap.mmap(fsrc.fileno(), 0, access=mmap.ACCESS_READ)
        src_mv = memoryview(src)

        # Each assembled group records its output length and EITHER a source offset
        # (unchanged, copied straight from src) OR freshly-encoded bytes (edited/new).
        assembled = []
        orig_bytes = len(src)                        # whole original data, kept verbatim
        orig_starts = core.sorted_group_starts(layout)

        for grp in layout.groups:
            gi = grp["index"]
            entries = layout.files_for_group(grp)
            edited = any((gi, e["index"]) in archive.replacements for e in entries)
            appended = extra_files.get(grp["name"])

            if not edited and not appended:
                frecs = [{"name_offset": e["name_offset"], "size": e["size"],
                          "offset": e["offset"], "unknown": e["unknown"]} for e in entries]
                assembled.append({
                    "name_offset": grp["name_offset"], "compression": grp["compression"],
                    "unpacked": grp["unpacked_size"], "unknown2": grp["unknown2"],
                    "length": grp["packed_size"], "src_off": grp["sector"] * SECTOR,
                    "enc": None, "files": frecs, "_name": grp["name"],
                    "orig_sector": grp["sector"],
                    "orig_cap": core.group_capacity_bytes(layout, orig_bytes, grp, orig_starts)})
                continue

            orig = core.decode_group_payload(src, grp)
            files = []
            for e in entries:
                data = archive.replacements.get((gi, e["index"]))
                if data is None:
                    data = orig[e["offset"]:e["offset"] + e["size"]]
                # Preserve the retained file's name_offset and unknown field.
                files.append((e["name"], data, e["name_offset"], e["unknown"]))
            for fname, data in (appended or []):
                files.append((fname, data, None, 0))
            # The game binary-searches each group's file table by name, so the
            # file records MUST stay sorted by name (bytewise / strcmp order).
            # Appending new files (e.g. select_*_<id> into music_select) would
            # otherwise break the sort and make the lookup misresolve → the game
            # reads the wrong file and hangs. Re-sort the whole file list here.
            files.sort(key=lambda f: f[0].encode("latin-1"))

            payload, offsets = _assemble_payload([data for _, data, _, _ in files])
            frecs = [{"name_offset": noff if noff is not None else add_name(fname),
                      "size": len(data), "offset": off, "unknown": unk}
                     for (fname, data, noff, unk), off in zip(files, offsets)]
            enc = core.encode_group_payload(payload, grp["compression"])
            assembled.append({
                "name_offset": grp["name_offset"], "compression": grp["compression"],
                # Edited payload changed: recompute the game-validated content hash
                # (SHA1-based; a wrong value freezes the game at load — see core).
                "unpacked": len(payload), "unknown2": core.compute_unknown2(payload),
                "length": len(enc), "src_off": None, "enc": enc, "files": frecs,
                "_name": grp["name"], "orig_sector": grp["sector"],
                "orig_cap": core.group_capacity_bytes(layout, orig_bytes, grp, orig_starts)})

        for spec in new_group_specs:
            comp = spec.get("compression", 2)
            if not spec.get("files"):
                raise ValueError(f"new group spec {spec.get('name')!r} has no files")
            payload, recs = _build_payload(spec["files"])
            for r in recs:
                r["name_offset"] = add_name(r.pop("name"))
            enc = core.encode_group_payload(payload, comp)
            assembled.append({
                "name_offset": add_name(spec["name"]), "compression": comp,
                "unpacked": len(payload), "unknown2": core.compute_unknown2(payload),
                "length": len(enc), "src_off": None, "enc": enc, "files": recs,
                "_name": spec["name"], "orig_sector": None, "orig_cap": 0})

        # ---- sector layout ----
        # The game locates every group by BINARY-SEARCHING the LIST.BIN name table
        # and then reads it from the sector stored in that group's record, so a
        # group may live at ANY sector as long as its record's sector field is
        # correct. We therefore keep changes minimal and self-consistent:
        #   * unchanged groups            -> keep their original sector (verbatim)
        #   * edited groups that still fit their original slot -> overwrite in place
        #   * new groups / edited groups that outgrew their slot -> append past the
        #     end of the original DATA.000 (their record's sector points there)
        # No group is "pinned" or evicted: reads are table-driven, not absolute.
        append_pos = orig_bytes                    # dense archive => already aligned
        for g in assembled:
            if g["enc"] is None:                   # unchanged: keep original sector
                g["out_off"] = g["src_off"]
                g["sector"] = g["src_off"] // SECTOR
            elif g.get("orig_sector") is not None and g["length"] <= g["orig_cap"]:
                g["out_off"] = g["orig_sector"] * SECTOR   # edited, fits in place
                g["sector"] = g["orig_sector"]
            else:                                  # new / outgrew: append
                sector = core.align(append_pos, SECTOR) // SECTOR
                g["out_off"] = sector * SECTOR
                g["sector"] = sector
                append_pos = g["out_off"] + g["length"]
        total = max(append_pos, orig_bytes)

        # The game binary-searches the GROUP table by name, so the group records
        # MUST stay sorted by name (bytewise / strcmp order). New groups were
        # appended above; re-sort the whole table now so a new song's groups land
        # in their correct position (each record keeps its own sector, so sorting
        # the table does not move any DATA.000 payload). Without this the lookup
        # misresolves, the game reads the wrong sector and hangs in inflate.
        assembled.sort(key=lambda g: g["_name"].encode("latin-1"))

        # ---- LIST.BIN ----
        group_count = len(assembled)
        file_count = sum(len(g["files"]) for g in assembled)
        file_base = (group_count + 1) * 32
        name_base = file_base + file_count * 16
        # Invariant shared with the reader (ArchiveLayout): for format 2,
        # name_base == (group_count*2 + file_count + 2) * 16. One off-by-one here
        # would silently desync every name.
        assert name_base == (group_count * 2 + file_count + 2) * 16
        new_dl = bytearray(name_base + len(name_table))
        new_dl[:32] = layout.decoded_list[:32]               # preserve timestamp etc.
        struct.pack_into("<II", new_dl, 0, group_count, file_count)
        new_dl[name_base:] = name_table
        first_file = 0
        for gidx, g in enumerate(assembled):
            struct.pack_into(
                "<8I", new_dl, 0x20 + gidx * 32,
                g["name_offset"], len(g["files"]), first_file, g["compression"],
                g["sector"], g["length"], g["unpacked"], g["unknown2"])
            for fj, fr in enumerate(g["files"]):
                struct.pack_into(
                    "<4I", new_dl, file_base + (first_file + fj) * 16,
                    fr["name_offset"], fr["size"], fr["offset"], fr["unknown"])
            first_file += len(g["files"])

        # ---- DATA.000: preserve original region verbatim, append changes ----
        new_data = bytearray(total)                          # zero-filled tail
        dst = memoryview(new_data)
        # Copy the entire original data region in one shot: this keeps every
        # unchanged group byte-identical at its original sector (including any
        # inter-group alignment padding), which is what the absolute-sector boot
        # loader depends on.
        dst[:orig_bytes] = src_mv[:orig_bytes]
        for g in assembled:                                  # write appended blocks
            o, length = g["out_off"], g["length"]
            if g["enc"] is not None:                         # edited/new: encoded bytes
                dst[o:o + length] = g["enc"]
            elif o != g["src_off"]:                          # evicted unchanged group:
                # it moved out of the verbatim-copied region, so copy its original
                # (already-encoded) block from the source to the new location.
                dst[o:o + length] = src_mv[g["src_off"]:g["src_off"] + length]

        return crypt_list_fast(bytes(new_dl)), new_data
    finally:
        # Release every view/handle into DATA.000 before returning. On Windows a
        # lingering file-mapping "section" keeps the file locked (ERROR_USER_
        # MAPPED_FILE / access-denied) so the caller can't overwrite DATA.000 in
        # the same process — the mmap must be fully torn down first. Releasing the
        # views + closing the handles + a gc pass drops the section immediately.
        if dst is not None:
            dst.release()
        if src_mv is not None:
            src_mv.release()
        if src is not None:
            src.close()
        fsrc.close()
        gc.collect()

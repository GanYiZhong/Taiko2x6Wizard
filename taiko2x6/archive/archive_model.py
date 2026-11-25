"""LIST.BIN + DATA.000 archive model: mmap-backed reads, staged edits, and
crash-safe saves. No Qt dependency - used by the Explorer GUI, the HDD song
wizard, the Omnimix Maker and the command-line self-tests.
"""
from __future__ import annotations

import logging
import mmap
from collections import OrderedDict
from pathlib import Path
from threading import Lock

from taiko2x6.archive import taiko256_archive_tool_v2 as core
from taiko2x6.safeio import recover_pair, txn_path, write_pair

log = logging.getLogger("taiko")

SECTOR_SIZE = core.SECTOR_SIZE

__all__ = ["Archive"]


class Archive:
    """In-memory view over a LIST.BIN + DATA.000 pair, backed by mmap for reads."""

    CACHE_BYTES_CAP = 256 * 1024 * 1024  # decoded-group LRU cache cap

    def __init__(self, list_path: Path, data_path: Path, fmt: int = 2):
        self.list_path = Path(list_path)
        self.data_path = Path(data_path)
        self.fmt = fmt
        # finish/undo a DATA+LIST write that was interrupted last time
        recover_pair(txn_path(self.data_path),
                     log=lambda m: log.warning("%s", m))
        self.layout = core.ArchiveLayout(core.crypt_list(self.list_path.read_bytes()), fmt)

        self._fh = open(self.data_path, "rb")
        self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)

        self._cache: "OrderedDict[int, bytes]" = OrderedDict()
        self._cache_bytes = 0
        self._cache_lock = Lock()

        # pending edits: (group_index, file_index) -> new bytes
        self.replacements: dict[tuple[int, int], bytes] = {}

    # -- lifecycle ------------------------------------------------------------
    def close(self):
        try:
            self._mm.close()
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass

    @property
    def data_len(self) -> int:
        return len(self._mm)

    # -- decode / read --------------------------------------------------------
    def decode_group(self, group: dict, use_cache: bool = True) -> bytes:
        gi = group["index"]
        if use_cache:
            with self._cache_lock:
                hit = self._cache.get(gi)
                if hit is not None:
                    self._cache.move_to_end(gi)
                    return hit
        payload = core.decode_group_payload(self._mm, group)
        if use_cache:
            with self._cache_lock:
                self._cache[gi] = payload
                self._cache.move_to_end(gi)
                self._cache_bytes += len(payload)
                while self._cache_bytes > self.CACHE_BYTES_CAP and len(self._cache) > 1:
                    _, dropped = self._cache.popitem(last=False)
                    self._cache_bytes -= len(dropped)
        return payload

    def read_file(self, group: dict, entry: dict) -> bytes:
        key = (group["index"], entry["index"])
        if key in self.replacements:
            return self.replacements[key]
        payload = self.decode_group(group)
        start = entry["offset"]
        return payload[start:start + entry["size"]]

    # -- edit -----------------------------------------------------------------
    def stage_replace(self, group_index: int, file_index: int, data: bytes):
        self.replacements[(group_index, file_index)] = data

    def discard_edits(self):
        self.replacements.clear()

    @property
    def dirty(self) -> bool:
        return bool(self.replacements)

    def affected_groups(self) -> set[int]:
        return {gi for (gi, _fi) in self.replacements}

    # -- save (conservative in-place patch) -----------------------------------
    def save(self, out_list: Path, out_data: Path, allow_relayout: bool,
             progress=None, backup: bool = False) -> dict:
        """Apply staged replacements and write out_list / out_data.

        Returns a summary dict. Raises ValueError with a readable message on
        a hard conflict (file no longer fits) when allow_relayout is False.
        DATA and LIST are committed together (safeio.write_pair); backup=True
        keeps a rolling .bak of each existing output first.

        Memory note: a full writable copy of DATA is loaded here. When the GUI
        overwrites in place it closes self._mm first (so peak ≈ DATA size); the
        non-overwrite (.new) path keeps the mmap open, so peak is ≈ 2× DATA.
        """
        layout = self.layout
        # Need a writable full copy of DATA for patching.
        full = bytearray(self.data_path.read_bytes())
        new_list = bytearray(layout.decoded_list)

        affected = sorted(self.affected_groups())
        changed = 0
        # where every group currently sits: [start, end) byte ranges
        spans = {g["index"]: (g["sector"] * SECTOR_SIZE,
                              g["sector"] * SECTOR_SIZE + g["packed_size"])
                 for g in layout.groups}
        for n, gi in enumerate(affected):
            group = layout.groups[gi]
            if progress:
                progress(n, len(affected), group["name"])
            entries = layout.files_for_group(group)
            original_payload = core.decode_group_payload(full, group)
            new_payload, updated_entries = self._patch_group(
                group, entries, original_payload, allow_relayout
            )
            encoded = core.encode_group_payload(new_payload, group["compression"])
            max_bytes = core.group_capacity_bytes(layout, len(full), group)
            sector = group["sector"]
            if len(encoded) > max_bytes:
                hole = self._free_hole(spans, gi, len(encoded), len(full)) \
                    if allow_relayout else None
                if hole is None:
                    raise ValueError(
                        f"group '{group['name']}' no longer fits its DATA sector range "
                        f"(encoded={len(encoded)} > available={max_bytes}). "
                        f"Make the replacement smaller.")
                # Move it into unused space INSIDE the current DATA: growing
                # DATA.000 on an HDD image left the game on LOADING, while a
                # same-size DATA with the group moved into a hole boots.
                old = sector * SECTOR_SIZE
                shared = any(s == old for k, (s, _e) in spans.items() if k != gi)
                if not shared:      # another group may alias the same sectors
                    full[old:old + group["packed_size"]] = b"\0" * group["packed_size"]
                sector = hole // SECTOR_SIZE
            start = sector * SECTOR_SIZE
            clear_len = max(group["packed_size"], len(encoded)) \
                if sector == group["sector"] else len(encoded)
            full[start:start + clear_len] = b"\0" * clear_len
            full[start:start + len(encoded)] = encoded
            spans[gi] = (start, start + len(encoded))

            gw = dict(group)
            gw["sector"] = sector
            gw["packed_size"] = len(encoded)
            gw["unpacked_size"] = len(new_payload)
            # Content changed -> recompute the game-validated content hash, else
            # the game freezes on load (see core.compute_unknown2).
            gw["unknown2"] = core.compute_unknown2(new_payload)
            layout.write_group(new_list, gw)
            for e in updated_entries:
                layout.write_file(new_list, e)
            changed += 1

        write_pair([(out_data, bytes(full)),
                    (out_list, core.crypt_list(bytes(new_list)))], backup=backup)
        if progress:
            progress(len(affected), len(affected), "done")
        return {"changed_groups": changed, "files": len(self.replacements)}

    @staticmethod
    def _free_hole(spans: dict, gi: int, size: int, data_len: int):
        """Byte offset of the first sector-aligned gap between groups (other
        than group gi itself) that holds `size` bytes, or None. Gaps are the
        dead space earlier relocations left behind; DATA does not grow."""
        ranges = sorted(v for k, v in spans.items() if k != gi)
        for (_s, end), (nxt, _e) in zip(ranges, ranges[1:]):
            start = -(-end // SECTOR_SIZE) * SECTOR_SIZE
            if nxt - start >= size:
                return start
        return None

    def _patch_group(self, group, entries, original_payload, allow_relayout):
        """Mirror of core conservative patch but driven by self.replacements."""
        gi = group["index"]
        updated = [dict(e) for e in entries]
        payload = bytearray(original_payload)

        if not allow_relayout:
            for i, entry in enumerate(updated):
                key = (gi, entry["index"])
                if key not in self.replacements:
                    continue
                new_file = self.replacements[key]
                start = entry["offset"]
                cap = core.file_slot_capacity(updated, len(original_payload), i)
                if len(new_file) > cap:
                    raise ValueError(
                        f"'{group['name']}/{entry['name']}' is {len(new_file)} bytes "
                        f"but its in-place slot only holds {cap}. "
                        f"Enable 'allow relayout' to repack the group internally."
                    )
                payload[start:start + cap] = b"\0" * cap
                payload[start:start + len(new_file)] = new_file
                entry["size"] = len(new_file)
            return bytes(payload), updated

        # relayout: rebuild the group tightly, keeping the decompressed size.
        rebuilt = bytearray()
        for entry in updated:
            key = (gi, entry["index"])
            if key in self.replacements:
                file_data = self.replacements[key]
            else:
                file_data = bytes(original_payload[entry["offset"]:entry["offset"] + entry["size"]])
            new_off = core.align(len(rebuilt), core.DATA_ALIGN)
            if new_off > len(rebuilt):
                rebuilt.extend(b"\0" * (new_off - len(rebuilt)))
            entry["offset"] = new_off
            entry["size"] = len(file_data)
            rebuilt.extend(file_data)
        if len(rebuilt) > len(original_payload):
            raise ValueError(
                f"group '{group['name']}' grew past its decompressed size "
                f"({len(rebuilt)} > {len(original_payload)}); cannot patch in place."
            )
        rebuilt.extend(b"\0" * (len(original_payload) - len(rebuilt)))
        return bytes(rebuilt), updated

    # -- full rebuild (allows files of ANY size; layout-preserving) -----------
    def rebuild(self, out_list: Path, out_data: Path, progress=None,
                backup: bool = False) -> dict:
        """Repack the archive so replacements may be any size.

        LAYOUT-PRESERVING: SYSTEM256 loads some boot-critical data by ABSOLUTE
        sector, so unchanged groups MUST keep their original position — a dense
        re-pack that reorders them boots to a black screen even though LIST.BIN
        stays self-consistent. Unchanged groups therefore stay exactly where they
        were (the whole original DATA region is copied verbatim); only EDITED
        groups are recompressed and appended past the end of the original data,
        and their old sectors become harmless dead space.

        Memory note: reads the whole source DATA into memory and copies it into
        the output bytearray. In the overwrite path the GUI closes self._mm first
        (peak ≈ 2× DATA); the .new path keeps the mmap open (≈ 3×).
        """
        layout = self.layout
        src = self.data_path.read_bytes()          # read original fully into memory first
        new_list = bytearray(layout.decoded_list)
        # Preserve every unchanged group at its original sector by copying the
        # entire original data region verbatim; edited groups get appended.
        new_data = bytearray(src)
        total = len(layout.groups)
        changed = 0

        for n, group in enumerate(layout.groups):
            if progress and (n % 16 == 0):
                progress(n, total, group["name"])
            gi = group["index"]
            entries = layout.files_for_group(group)
            edited = any((gi, e["index"]) in self.replacements for e in entries)
            if not edited:
                continue                            # already in place, verbatim

            # rebuild + recompress the edited group, then append it
            original_payload = core.decode_group_payload(src, group)
            updated = [dict(e) for e in entries]
            payload = bytearray()
            for entry in updated:
                file_data = self.replacements.get((gi, entry["index"]))
                if file_data is None:
                    file_data = original_payload[entry["offset"]:entry["offset"] + entry["size"]]
                new_off = core.align(len(payload), core.DATA_ALIGN)
                if new_off > len(payload):
                    payload.extend(b"\0" * (new_off - len(payload)))
                entry["offset"] = new_off
                entry["size"] = len(file_data)
                payload.extend(file_data)
            # trailer pad, mirroring the CLI repacker
            min_len = core.align(len(payload) + core.PAYLOAD_TRAILER_SIZE, core.PAYLOAD_ALIGN)
            if min_len > len(payload):
                payload.extend(b"\0" * (min_len - len(payload)))
            payload = bytes(payload)
            packed = core.encode_group_payload(payload, group["compression"])

            # append at the next free sector past the current end
            sector = core.align(len(new_data), SECTOR_SIZE) // SECTOR_SIZE
            pad = sector * SECTOR_SIZE - len(new_data)
            if pad:
                new_data.extend(b"\0" * pad)
            gw = dict(group)
            gw["sector"] = sector
            gw["packed_size"] = len(packed)
            gw["unpacked_size"] = len(payload)
            gw["unknown2"] = core.compute_unknown2(payload)   # game-validated hash
            new_data.extend(packed)
            layout.write_group(new_list, gw)
            for entry in updated:
                layout.write_file(new_list, entry)
            changed += 1

        if progress:
            progress(total, total, "writing files")
        write_pair([(out_data, bytes(new_data)),
                    (out_list, core.crypt_list(bytes(new_list)))], backup=backup)
        return {"changed_groups": changed, "files": len(self.replacements),
                "data_size": len(new_data)}

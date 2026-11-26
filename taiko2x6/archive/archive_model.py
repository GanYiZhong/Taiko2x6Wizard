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

_CHUNK = 1024 * 1024


class _FileView:
    """Read-only sliceable view of a file (seek+read, no mmap, no full load),
    accepted by core.decode_group_payload like bytes/mmap."""

    def __init__(self, path: Path):
        self._f = open(path, "rb")
        self._f.seek(0, 2)
        self.size = self._f.tell()

    def __len__(self):
        return self.size

    def read(self, start: int, stop: int) -> bytes:
        stop = min(stop, self.size)
        if stop <= start:
            return b""
        self._f.seek(start)
        return self._f.read(stop - start)

    def __getitem__(self, sl):
        return self.read(sl.start or 0, self.size if sl.stop is None else sl.stop)

    def close(self):
        self._f.close()


class _Overlay:
    """A _FileView plus in-memory patches applied in order; reads see the
    patched bytes exactly as the old full bytearray copy did."""

    def __init__(self, base: _FileView):
        self.base = base
        self.size = base.size
        self.patches: list[tuple[int, bytes]] = []

    def __len__(self):
        return self.size

    def write(self, off: int, data: bytes):
        self.patches.append((off, bytes(data)))
        self.size = max(self.size, off + len(data))

    def __getitem__(self, sl):
        start = sl.start or 0
        stop = self.size if sl.stop is None else min(sl.stop, self.size)
        if stop <= start:
            return b""
        buf = bytearray(self.base.read(start, stop))
        buf.extend(b"\0" * (stop - start - len(buf)))
        for off, d in self.patches:
            lo, hi = max(off, start), min(off + len(d), stop)
            if lo < hi:
                buf[lo - start:hi - start] = d[lo - off:hi - off]
        return bytes(buf)

    def merged_ranges(self):
        """Sorted, non-overlapping [start, end) byte ranges touched by patches."""
        out: list[list[int]] = []
        for a, b in sorted((o, o + len(d)) for o, d in self.patches if d):
            if out and a <= out[-1][1]:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return out


def _copy_range(src, dst, start: int, stop: int):
    """Copy src[start:stop) to dst in chunks (src is a binary file object)."""
    src.seek(start)
    left = stop - start
    buf = bytearray(min(_CHUNK, max(left, 1)))
    mv = memoryview(buf)
    while left > 0:
        n = src.readinto(mv[:min(len(buf), left)])
        if not n:
            raise EOFError("DATA ended while copying")
        dst.write(mv[:n])
        left -= n


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
        # groups to rebuild tightly at their new (smaller) decompressed size
        self.compact: set[int] = set()

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

    def unstage(self, group_index: int, file_index: int) -> bool:
        """Drop one staged replacement. Returns True if there was one."""
        return self.replacements.pop((group_index, file_index), None) is not None

    def stage_compact(self, group_index: int):
        """Rebuild this group tightly on save and let its decompressed size
        shrink. The game allocates a group's decompressed size when it loads
        it, so shrinking files only frees game memory with this."""
        self.compact.add(group_index)

    def discard_edits(self):
        self.replacements.clear()
        self.compact.clear()

    @property
    def dirty(self) -> bool:
        return bool(self.replacements or self.compact)

    def affected_groups(self) -> set[int]:
        return {gi for (gi, _fi) in self.replacements} | self.compact

    # -- save (conservative in-place patch) -----------------------------------
    def save(self, out_list: Path, out_data: Path, allow_relayout: bool,
             progress=None, backup: bool = False) -> dict:
        """Apply staged replacements and write out_list / out_data.

        Returns a summary dict. Raises ValueError with a readable message on
        a hard conflict (file no longer fits) when allow_relayout is False.
        DATA and LIST are committed together (safeio.write_pair); backup=True
        keeps a rolling .bak of each existing output first.

        Memory note: DATA is never loaded whole. Edited groups are read through
        a patch overlay on the source file; the new DATA is streamed to a temp
        file in chunks (unchanged regions copied verbatim) and committed by
        write_pair. Peak is a few edited groups plus one copy chunk.
        """
        view = _FileView(self.data_path)
        try:
            return self._save(view, out_list, out_data, allow_relayout,
                              progress, backup)
        finally:
            view.close()

    def _save(self, view, out_list, out_data, allow_relayout, progress, backup):
        layout = self.layout
        full = _Overlay(view)
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
                    full.write(old, b"\0" * group["packed_size"])
                sector = hole // SECTOR_SIZE
            start = sector * SECTOR_SIZE
            clear_len = max(group["packed_size"], len(encoded)) \
                if sector == group["sector"] else len(encoded)
            full.write(start, b"\0" * clear_len)
            full.write(start, encoded)
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

        data_path, src_size = self.data_path, view.size
        # patched ranges (edited groups only) resolved now, while `view` is open
        blocks = [(a, full[a:b]) for a, b in full.merged_ranges()]

        def write_data(f):
            with open(data_path, "rb") as src:
                pos = 0
                for a, block in blocks:
                    _copy_range(src, f, pos, min(a, src_size))
                    f.write(block)
                    pos = a + len(block)
                if pos < src_size:
                    _copy_range(src, f, pos, src_size)

        # read phase is over: release our handle so an in-place commit is not
        # blocked by it (the writer opens its own)
        view.close()
        write_pair([(out_data, write_data),
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

        if not allow_relayout and gi not in self.compact:
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

        # relayout: rebuild the group tightly, keeping the decompressed size
        # (or, for a compacted group, at its new size with the usual trailer)
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
        if gi in self.compact:
            end = core.align(len(rebuilt) + core.PAYLOAD_TRAILER_SIZE, core.PAYLOAD_ALIGN)
            if end < len(original_payload):
                rebuilt.extend(b"\0" * (end - len(rebuilt)))
                return bytes(rebuilt), updated
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

        Memory note: DATA is never loaded whole: the original region is
        stream-copied to a temp file in chunks and only the recompressed groups
        (appended) are held in memory.
        """
        src = _FileView(self.data_path)
        try:
            return self._rebuild(src, out_list, out_data, progress, backup)
        finally:
            src.close()

    def _rebuild(self, src, out_list, out_data, progress, backup):
        layout = self.layout
        new_list = bytearray(layout.decoded_list)
        # Preserve every unchanged group at its original sector by copying the
        # entire original data region verbatim; edited groups get appended.
        appended: list[bytes] = []          # pieces written after the original
        new_len = src.size
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
            sector = core.align(new_len, SECTOR_SIZE) // SECTOR_SIZE
            pad = sector * SECTOR_SIZE - new_len
            if pad:
                appended.append(b"\0" * pad)
                new_len += pad
            gw = dict(group)
            gw["sector"] = sector
            gw["packed_size"] = len(packed)
            gw["unpacked_size"] = len(payload)
            gw["unknown2"] = core.compute_unknown2(payload)   # game-validated hash
            appended.append(packed)
            new_len += len(packed)
            layout.write_group(new_list, gw)
            for entry in updated:
                layout.write_file(new_list, entry)
            changed += 1

        if progress:
            progress(total, total, "writing files")
        data_path, src_size = self.data_path, src.size

        def write_data(f):
            with open(data_path, "rb") as s:
                _copy_range(s, f, 0, src_size)
            for piece in appended:
                f.write(piece)

        src.close()
        write_pair([(out_data, write_data),
                    (out_list, core.crypt_list(bytes(new_list)))], backup=backup)
        return {"changed_groups": changed, "files": len(self.replacements),
                "data_size": new_len}

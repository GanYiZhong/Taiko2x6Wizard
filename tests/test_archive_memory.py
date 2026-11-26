"""Archive.save / Archive.rebuild stream DATA instead of loading it whole.

The pre-streaming implementations are kept below as reference; the new ones
must produce byte-identical files, with peak Python memory far below the
DATA size.
"""
import random
import tracemalloc

import pytest

from taiko2x6.archive.archive_model import Archive
from taiko2x6.archive import taiko256_archive_tool_v2 as core
from taiko2x6 import safeio

from conftest import build_archive

SECTOR_SIZE = core.SECTOR_SIZE
N_GROUPS = 24
BLOB = 1024 * 1024          # 1 MiB per group -> ~25+ MB DATA


# --------------------------------------------------------------------------- #
#  reference: the old whole-file implementations (return bytes, write nothing)
# --------------------------------------------------------------------------- #
def _ref_save(self, allow_relayout):
    layout = self.layout
    full = bytearray(self.data_path.read_bytes())
    new_list = bytearray(layout.decoded_list)
    affected = sorted(self.affected_groups())
    spans = {g["index"]: (g["sector"] * SECTOR_SIZE,
                          g["sector"] * SECTOR_SIZE + g["packed_size"])
             for g in layout.groups}
    for gi in affected:
        group = layout.groups[gi]
        entries = layout.files_for_group(group)
        original_payload = core.decode_group_payload(full, group)
        new_payload, updated_entries = self._patch_group(
            group, entries, original_payload, allow_relayout)
        encoded = core.encode_group_payload(new_payload, group["compression"])
        max_bytes = core.group_capacity_bytes(layout, len(full), group)
        sector = group["sector"]
        if len(encoded) > max_bytes:
            hole = self._free_hole(spans, gi, len(encoded), len(full)) \
                if allow_relayout else None
            if hole is None:
                raise ValueError("does not fit")
            old = sector * SECTOR_SIZE
            shared = any(s == old for k, (s, _e) in spans.items() if k != gi)
            if not shared:
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
        gw["unknown2"] = core.compute_unknown2(new_payload)
        layout.write_group(new_list, gw)
        for e in updated_entries:
            layout.write_file(new_list, e)
    return bytes(full), core.crypt_list(bytes(new_list))


def _ref_rebuild(self):
    layout = self.layout
    src = self.data_path.read_bytes()
    new_list = bytearray(layout.decoded_list)
    new_data = bytearray(src)
    for group in layout.groups:
        gi = group["index"]
        entries = layout.files_for_group(group)
        if not any((gi, e["index"]) in self.replacements for e in entries):
            continue
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
        min_len = core.align(len(payload) + core.PAYLOAD_TRAILER_SIZE, core.PAYLOAD_ALIGN)
        if min_len > len(payload):
            payload.extend(b"\0" * (min_len - len(payload)))
        payload = bytes(payload)
        packed = core.encode_group_payload(payload, group["compression"])
        sector = core.align(len(new_data), SECTOR_SIZE) // SECTOR_SIZE
        pad = sector * SECTOR_SIZE - len(new_data)
        if pad:
            new_data.extend(b"\0" * pad)
        gw = dict(group)
        gw["sector"] = sector
        gw["packed_size"] = len(packed)
        gw["unpacked_size"] = len(payload)
        gw["unknown2"] = core.compute_unknown2(payload)
        new_data.extend(packed)
        layout.write_group(new_list, gw)
        for entry in updated:
            layout.write_file(new_list, entry)
    return bytes(new_data), core.crypt_list(bytes(new_list))


# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def big_pair(tmp_path_factory):
    rnd = random.Random(1234)
    groups = [(f"song.g{i:02d}",
               [(f"g{i:02d}_a.bin", rnd.randbytes(BLOB)),
                (f"g{i:02d}_b.bin", rnd.randbytes(4096))])
              for i in range(N_GROUPS)]
    lb, db = build_archive(groups, comp=6)
    d = tmp_path_factory.mktemp("bigarc")
    (d / "list.bin").write_bytes(lb)
    (d / "DATA.000").write_bytes(db)
    return d / "list.bin", d / "DATA.000", len(db)


def _open_staged(lp, dp, relayout_big=False):
    a = Archive(lp, dp)
    rnd = random.Random(99)
    for gi in (0, 7, N_GROUPS - 1):                  # first, middle, last
        g = a.layout.groups[gi]
        es = a.layout.files_for_group(g)
        a.stage_replace(gi, es[0]["index"], rnd.randbytes(BLOB - 5000))
        a.stage_replace(gi, es[1]["index"], rnd.randbytes(100))
    if relayout_big:
        a.stage_compact(7)
    a.close()      # save/rebuild must not need the mmap
    return a


def test_save_identical_to_old(big_pair, tmp_path):
    lp, dp, _ = big_pair
    for relayout in (False, True):
        a = _open_staged(lp, dp, relayout_big=relayout)
        ref_d, ref_l = _ref_save(a, relayout)
        od, ol = tmp_path / f"D{relayout}", tmp_path / f"L{relayout}"
        a.save(ol, od, allow_relayout=relayout)
        assert od.read_bytes() == ref_d
        assert ol.read_bytes() == ref_l
        assert sorted(p.name for p in tmp_path.iterdir() if "txn" in p.name or ".tmp" in p.name) == []


def test_save_in_place_with_backup_identical(big_pair, tmp_path):
    lp, dp, _ = big_pair
    l2, d2 = tmp_path / "list.bin", tmp_path / "DATA.000"
    l2.write_bytes(lp.read_bytes())
    d2.write_bytes(dp.read_bytes())
    a = _open_staged(l2, d2)
    ref_d, ref_l = _ref_save(a, False)
    a.save(l2, d2, allow_relayout=False, backup=True)
    assert d2.read_bytes() == ref_d and l2.read_bytes() == ref_l
    assert d2.with_name("DATA.000.bak").read_bytes() == dp.read_bytes()
    assert l2.with_name("list.bin.bak").read_bytes() == lp.read_bytes()
    assert not safeio.txn_path(d2).exists()


def test_rebuild_identical_to_old(big_pair, tmp_path):
    lp, dp, _ = big_pair
    a = _open_staged(lp, dp)
    ref_d, ref_l = _ref_rebuild(a)
    od, ol = tmp_path / "D", tmp_path / "L"
    summary = a.rebuild(ol, od)
    assert od.read_bytes() == ref_d
    assert ol.read_bytes() == ref_l
    assert summary["data_size"] == len(ref_d)


def test_failed_save_leaves_files_untouched(big_pair, tmp_path):
    lp, dp, _ = big_pair
    l2, d2 = tmp_path / "list.bin", tmp_path / "DATA.000"
    l2.write_bytes(lp.read_bytes())
    d2.write_bytes(dp.read_bytes())
    a = Archive(l2, d2)
    g = a.layout.groups[3]
    e = a.layout.files_for_group(g)[0]
    a.stage_replace(3, e["index"], b"x" * (BLOB * 2))
    a.close()
    with pytest.raises(ValueError):
        a.save(l2, d2, allow_relayout=False, backup=True)
    assert d2.read_bytes() == dp.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["DATA.000", "list.bin"]


def _peak(fn):
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_peak_memory_far_below_data_size(big_pair, tmp_path):
    lp, dp, data_len = big_pair
    a = _open_staged(lp, dp)
    ref_peak = _peak(lambda: _ref_save(a, False))
    save_peak = _peak(lambda: a.save(tmp_path / "l1", tmp_path / "d1", allow_relayout=False))
    rebuild_peak = _peak(lambda: a.rebuild(tmp_path / "l2", tmp_path / "d2"))
    print(f"DATA {data_len / 1e6:.1f} MB; old save peak {ref_peak / 1e6:.1f} MB, "
          f"new save {save_peak / 1e6:.1f} MB, new rebuild {rebuild_peak / 1e6:.1f} MB")
    assert ref_peak > data_len                       # the old way held all of it
    assert save_peak < data_len / 2
    assert rebuild_peak < data_len / 2

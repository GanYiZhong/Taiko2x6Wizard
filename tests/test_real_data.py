"""Tests against retail game files. Skipped unless $TAIKO_GAME_LIBRARY points
at a folder of game directories, e.g.::

    TAIKO_GAME_LIBRARY=E:/system2x6_template_gamelibrary/system2x6_template_library

Originals are only ever opened read-only; every write test works on a copy in
pytest's temp dir (set --basetemp to a roomy drive: the HDD test copies a
multi-GB image).
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest

from taiko2x6.archive import taiko256_archive_tool_v2 as core
from taiko2x6.archive.archive_model import Archive
from taiko2x6 import safeio

LIB = os.environ.get("TAIKO_GAME_LIBRARY")
pytestmark = [pytest.mark.realdata,
              pytest.mark.skipif(not LIB, reason="TAIKO_GAME_LIBRARY not set")]


def _ci(folder: Path, name: str):
    for p in folder.iterdir():
        if p.name.lower() == name.lower():
            return p
    return None


def _pairs():
    if not LIB:
        return []
    out = []
    for d in sorted(Path(LIB).iterdir()):
        if d.is_dir():
            lp, dp = _ci(d, "list.bin"), _ci(d, "DATA.000")
            if lp and dp:
                out.append(pytest.param(lp, dp, id=d.name))
    return out


def _images():
    if not LIB:
        return []
    return [pytest.param(p, id=p.name) for p in sorted(Path(LIB).glob("*/*.img"))
            if not p.name.endswith(".bak")]


def _sha_file(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 24), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.mark.parametrize("lp,dp", _pairs())
def test_every_group_decodes_and_hash_matches(lp, dp):
    a = Archive(lp, dp)
    try:
        bad = []
        for g in a.layout.groups:
            payload = a.decode_group(g, use_cache=False)
            if core.compute_unknown2(payload) != g["unknown2"]:
                bad.append(g["name"])
        assert not bad, f"{len(bad)} group hash mismatches, e.g. {bad[:5]}"
        assert len(a.layout.groups) > 100
    finally:
        a.close()


@pytest.mark.parametrize("lp,dp", _pairs()[:1])
def test_in_place_save_on_copy(lp, dp, tmp_path):
    cl, cd = tmp_path / "list.bin", tmp_path / "DATA.000"
    shutil.copy2(lp, cl)
    shutil.copy2(dp, cd)
    a = Archive(cl, cd)
    # first small, non-empty file in a compressed group
    g, e = next((g, e) for g in a.layout.groups if g["compression"] == 2
                for e in a.layout.files_for_group(g) if 64 <= e["size"] <= 4096)
    orig = a.read_file(g, e)
    new = bytes(b ^ 0x5A for b in orig)
    others = [(g2, a.read_file(g2, e2)) for g2 in a.layout.groups[:20] if g2 is not g
              for e2 in a.layout.files_for_group(g2)[:1]]
    a.stage_replace(g["index"], e["index"], new)
    a.close()
    a.save(cl, cd, allow_relayout=False, backup=True)
    assert _sha_file(cd.with_name("DATA.000.bak")) == _sha_file(dp)
    b = Archive(cl, cd)
    try:
        g2 = b.layout.groups[g["index"]]
        e2 = b.layout.files_for_group(g2)[e["index"] - g2["first_file"]]
        assert b.read_file(g2, e2) == new
        assert core.compute_unknown2(b.decode_group(g2, use_cache=False)) == g2["unknown2"]
        for og, data in others:
            ng = b.layout.groups[og["index"]]
            assert b.read_file(ng, b.layout.files_for_group(ng)[0]) == data
    finally:
        b.close()


@pytest.mark.parametrize("img", _images())
def test_hdd_image_lists_and_parses_game_list(img):
    from taiko2x6.ps2 import ps2hdd
    h = ps2hdd.Ps2Hdd(str(img))
    try:
        found = 0
        for p in h.partitions():
            if not p["is_pfs"]:
                continue
            try:
                names = {e["name"].lower(): e for e in h.pfs_listdir(p["name"], "/")}
            except Exception:
                continue
            if "list.bin" in names and "data.000" in names:
                lst = h.pfs_read(p["name"], "/" + names["list.bin"]["name"])
                layout = core.ArchiveLayout(core.crypt_list(lst), 2)
                assert len(layout.groups) > 100 and len(layout.files) > len(layout.groups)
                found += 1
        assert found >= 1, "no partition with list.bin + DATA.000"
    finally:
        h.close()
    assert not list(img.parent.glob(img.name + ".*.undo"))


@pytest.mark.skipif(not os.environ.get("TAIKO_HEAVY"),
                    reason="copies a multi-GB image; set TAIKO_HEAVY=1")
@pytest.mark.parametrize("img", _images()[:1])
def test_hdd_write_session_rollback_on_copy(img, tmp_path):
    from taiko2x6.ps2 import ps2hdd
    work = tmp_path / img.name
    shutil.copy2(img, work)
    before = _sha_file(work)
    h = ps2hdd.Ps2Hdd(str(work))
    part = None
    for p in h.partitions():
        if p["is_pfs"]:
            try:
                if any(e["name"].lower() == "list.bin" for e in h.pfs_listdir(p["name"], "/")):
                    part = p["name"]
                    break
            except Exception:
                pass
    lst = h.pfs_read(part, "/list.bin")
    h.close()
    changed = bytes(b ^ 0xFF for b in lst)

    # 1) an error inside the session rolls the image back by itself
    with pytest.raises(RuntimeError):
        with ps2hdd.write_session(str(work)) as w:
            w.pfs_write(part, "/list.bin", changed)
            raise RuntimeError("boom")
    assert _sha_file(work) == before

    # 2) a successful session keeps a journal that restores the image
    with ps2hdd.write_session(str(work)) as w:
        w.pfs_write(part, "/list.bin", changed)
    r = ps2hdd.Ps2Hdd(str(work))
    assert r.pfs_read(part, "/list.bin") == changed
    r.close()
    safeio.rollback(w.undo_path)
    assert _sha_file(work) == before

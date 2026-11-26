"""Batch add songs: scan, build chain, parity with the single-song path.

The three DB bins are tiny JSON stand-ins (the real parsers need retail-sized
files); everything else -- TJA conversion, WAV -> VAG, group creation, the
DATA.000 rebuild -- is the real code path.
"""
import json
import math
import shutil
import struct
import wave
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from conftest import build_archive                              # noqa: E402
from taiko2x6.archive.archive_model import Archive               # noqa: E402
from taiko2x6.songs import batch_add, song_builder as SB         # noqa: E402

TJA = """TITLE:{title}
BPM:150
OFFSET:-1.5
{wave}COURSE:Oni
LEVEL:8
#START
1020,
1111,
#END
COURSE:Easy
LEVEL:3
#START
1000,
#END
"""


def _wav(path, secs=1.0):
    n = int(44100 * secs)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"".join(
            struct.pack("<hh", *(int(8000 * math.sin(i / 20)),) * 2) for i in range(n)))


def _fake_db(monkeypatch):
    """JSON stand-ins for the DB bins' parse/serialize steps."""
    def ids(archive):
        return json.loads(SB._read_named(archive, "tuning.bin"))["ids"]

    def consistent(archive):
        n = len(ids(archive))
        counts = [n, json.loads(SB._read_named(archive, "musicinfo.bin"))["n"],
                  json.loads(SB._read_named(archive, "streaminfo.bin"))["n"]]
        return {"musicinfo": counts[1], "tuning_ids": n, "tuning_blocks": counts[2],
                "consistent": len(set(counts)) == 1}

    def compute(archive, new_id, k_src, stars):
        cur = ids(archive) + [new_id]
        enc = lambda d: json.dumps(d).encode().ljust(900, b" ")
        return {"tuning.bin": enc({"ids": cur, "stars": stars}),
                "musicinfo.bin": enc({"n": len(cur)}),
                "streaminfo.bin": enc({"n": len(cur)})}

    monkeypatch.setattr(SB, "song_ids", ids)
    monkeypatch.setattr(SB, "check_db_consistency", consistent)
    monkeypatch.setattr(SB, "compute_db_add", compute)
    monkeypatch.setattr(SB, "_template_stars", lambda a, k: [1, 2, 3, 4])


def _make_archive(tmp_path, name="base"):
    groups = [("bins", [
        ("tuning.bin", json.dumps({"ids": ["tmpl", "other"]}).encode().ljust(900, b" ")),
        ("musicinfo.bin", json.dumps({"n": 2}).encode().ljust(900, b" ")),
        ("streaminfo.bin", json.dumps({"n": 2}).encode().ljust(900, b" "))])]
    for p in ("1p", "2p"):
        for letter in "enhm":
            groups.append(("fumen.tmpl%s_%s" % (p, letter), [("sht", b"S" * 200)]))
    lb, db = build_archive(groups)
    d = tmp_path / name
    d.mkdir()
    (d / "LIST.BIN").write_bytes(lb)
    (d / "DATA.000").write_bytes(db)
    return d / "LIST.BIN", d / "DATA.000"


@pytest.fixture
def songs_dir(tmp_path):
    d = tmp_path / "songs"
    (d / "pack").mkdir(parents=True)
    (d / "a.tja").write_text(TJA.format(title="Alpha", wave="WAVE:a.wav\n"), encoding="utf-8")
    _wav(d / "a.wav")
    (d / "b.tja").write_text(TJA.format(title="Beta", wave=""), encoding="utf-8")
    _wav(d / "b.wav")                                  # same stem, no WAVE: header
    (d / "pack" / "c.tja").write_text(TJA.format(title="Gamma", wave="WAVE:snd.wav\n"),
                                      encoding="utf-8")
    _wav(d / "pack" / "snd.wav")
    (d / "broken.tja").write_text("TITLE:Broken\nBPM:120\n", encoding="utf-8")
    return d


@pytest.fixture
def arch(tmp_path, monkeypatch):
    _fake_db(monkeypatch)
    a = Archive(*_make_archive(tmp_path))
    yield a
    a.close()


def test_scan_finds_songs_and_problems(songs_dir):
    cands = batch_add.scan_sources([songs_dir], taken={"tmpl", "other"})
    by = {Path(c.path).name: c for c in cands}
    assert set(by) == {"a.tja", "b.tja", "c.tja", "broken.tja"}
    assert by["a.tja"].title == "Alpha" and Path(by["a.tja"].audio).name == "a.wav"
    assert Path(by["b.tja"].audio).name == "b.wav"
    assert Path(by["c.tja"].audio).name == "snd.wav"
    assert by["a.tja"].courses == [("easy", 3), ("oni", 8)]
    assert not by["broken.tja"].ok
    ids = [c.sid for c in cands]
    assert len(set(ids)) == len(ids) and not set(ids) & {"tmpl", "other"}


def test_scan_missing_audio_and_id_rules(tmp_path):
    (tmp_path / "x.tja").write_text(TJA.format(title="X", wave=""), encoding="utf-8")
    (c,) = batch_add.scan_sources([tmp_path / "x.tja"])
    assert c.ok and c.audio is None and "audio not found" in c.warnings
    assert not c.checked
    assert batch_add.unique_id("Hello World!", {"helloworld"}) == "helloworld2"
    assert batch_add.unique_id("日本語", set()) == "song"
    assert batch_add.id_problem("Bad Id", set())
    assert batch_add.id_problem("ok_1", {"ok_1"}) == "id already in use"
    assert batch_add.id_problem("ok_2", {"ok_1"}) is None


def test_song_limit():
    assert batch_add.check_song_limit(90, 10) is None
    assert batch_add.check_song_limit(205, 10)[0] == "warn"
    assert batch_add.check_song_limit(600, 100)[0] == "block"


def _items(songs_dir, arch, with_broken=True):
    cands = batch_add.scan_sources([songs_dir], taken=set(SB.song_ids(arch)))
    if not with_broken:
        cands = [c for c in cands if c.ok]
    cands.sort(key=lambda c: c.title)       # Alpha, Beta, Broken, Gamma
    return batch_add.items_from_candidates(cands)


def _read_all(archive):
    out = {}
    for g in archive.layout.groups:
        for e in archive.layout.files_for_group(g):
            out[(g["name"], e["name"])] = archive.read_file(g, e)
    return out


def test_batch_builds_three_and_reports_broken(songs_dir, arch, tmp_path):
    items = _items(songs_dir, arch)
    assert [i.title for i in items] == ["Alpha", "Beta", "Broken", "Gamma"]
    seen = []
    res = batch_add.BatchBuilder(arch, "tmpl").run_all(
        items, progress=lambda i, n, sid: seen.append((i, n, sid)))
    assert len(seen) == 4
    assert [r.ok for r in res.items] == [True, True, False, True]
    assert "no charts" in res.items[2].message
    assert res.built == 3 and res.failed == 1 and res.new_archive

    lp, dp = tmp_path / "out" / "LIST.BIN", tmp_path / "out" / "DATA.000"
    lp.parent.mkdir()
    lp.write_bytes(bytes(res.new_archive[0]))
    dp.write_bytes(bytes(res.new_archive[1]))
    out = Archive(lp, dp)
    try:
        ids = SB.song_ids(out)
        new = [i.sid for i in items if i.title != "Broken"]
        assert ids == ["tmpl", "other"] + new and len(set(ids)) == len(ids)
        con = SB.check_db_consistency(out)
        assert con["consistent"] and con["musicinfo"] == 5
        names = {g["name"] for g in out.layout.groups}
        for sid in new:
            assert "sound.stream.music_%s" % sid in names
            assert "fumen.%s1p_m" % sid in names
        assert not any("broken" in n for n in names)
    finally:
        out.close()
    assert not arch.replacements          # the caller's archive is untouched


def test_batch_identical_to_single_song_path(songs_dir, arch, tmp_path):
    items = _items(songs_dir, arch, with_broken=False)
    batch = batch_add.BatchBuilder(arch, "tmpl").run_all(items)

    # the same songs through the single-song path, re-opening between songs
    cur = Archive(*_make_archive(tmp_path, "single"))
    last = None
    try:
        for n, it in enumerate(items):
            db_pre, stars = SB.prepare_new_song_db(cur, it.sid, "tmpl", it.tja_text)
            last = SB.add_new_song(cur, it.sid, it.title, "tmpl", tja_text=it.tja_text,
                                   audio_path=it.audio, precomputed_db=db_pre,
                                   stars=stars)
            d = tmp_path / ("step%d" % n)
            d.mkdir()
            (d / "LIST.BIN").write_bytes(bytes(last[0]))
            (d / "DATA.000").write_bytes(bytes(last[1]))
            cur.close()
            cur = Archive(d / "LIST.BIN", d / "DATA.000")
    finally:
        cur.close()
    assert bytes(batch.new_archive[0]) == bytes(last[0])
    assert bytes(batch.new_archive[1]) == bytes(last[1])


def test_dialog_batch_flow(songs_dir, arch, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from taiko2x6.app import appconfig
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    app = QApplication.instance() or QApplication([])
    dlg = batch_add.BatchAddDialog(arch)
    try:
        dlg.add_paths([songs_dir])
        assert dlg.table.rowCount() == 4
        titles = [dlg.table.item(r, 0).text() for r in range(4)]
        rb = titles.index("Broken")
        assert dlg.table.item(rb, 0).checkState() == Qt.Unchecked
        assert dlg.table.item(rb, 4).text()
        assert len(dlg.selected()) == 3 and dlg.b_build.isEnabled()
        # untick one, then clash an id: blocked until fixed
        ra = titles.index("Alpha")
        dlg.table.item(ra, 0).setCheckState(Qt.Unchecked)
        assert len(dlg.selected()) == 2
        dlg.table.item(titles.index("Beta"), 1).setText("tmpl")
        assert len(dlg.selected()) == 1
        dlg.table.item(titles.index("Beta"), 1).setText("betasong")
        assert len(dlg.selected()) == 2

        dlg._build()
        import time
        end = time.time() + 120
        while dlg._busy and time.time() < end:
            app.processEvents()
            time.sleep(0.02)
        assert not dlg._busy and dlg.result.built == 2 and dlg.result.failed == 0
        assert dlg.new_archive
        assert not arch.replacements
    finally:
        dlg.close()
        dlg.deleteLater()

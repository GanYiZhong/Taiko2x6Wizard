"""gen3_song batch import: identical output, caching, malformed folders."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import gen3_fixture                                    # noqa: E402
from taiko2x6.charts import gen3_convert, gen3_song     # noqa: E402


@pytest.fixture
def root(tmp_path):
    gen3_song.clear_cache()
    gen3_fixture.build_root(tmp_path, 4)
    yield tmp_path
    gen3_song.clear_cache()


def test_load_song_values(root):
    s = gen3_song.load_song(root / "fumen" / "song0001")
    assert s["title"] == "Title 1" and s["known"]
    assert set(s["sht"]) == {"easy", "normal", "hard", "oni"}
    assert s["stars"]["oni"] == 4
    assert all("audio" in w for w in s["warnings"])
    plain = gen3_convert.convert_fumen(
        root / "fumen" / "song0001" / "song0001_m.bin")
    assert s["sht"]["oni"] == plain


def test_load_folder_deterministic(root):
    a, ea = gen3_song.load_folder(root / "fumen")
    gen3_song.clear_cache()
    b, eb = gen3_song.load_folder(root / "fumen")
    assert not ea and not eb
    assert list(a) == sorted(a) == list(b)
    assert all(a[k]["sht"] == b[k]["sht"] for k in a)


def test_datatables_decrypted_once(root, monkeypatch):
    calls = []
    real = gen3_convert.decrypt_fumen

    def spy(path, key=gen3_convert.FUMEN_KEY):
        if key == gen3_convert.DATATABLE_KEY:
            calls.append(os.path.basename(str(path)))
        return real(path, key=key)
    monkeypatch.setattr(gen3_convert, "decrypt_fumen", spy)
    songs, errs = gen3_song.load_folder(root / "fumen")
    assert len(songs) == 4 and not errs
    assert sorted(calls) == ["musicinfo.bin", "wordlist.bin"]


def test_table_cache_refreshes_when_file_changes(root):
    assert gen3_song.music_info(root, "song0000")["uniqueId"] == 0
    items = [{"id": "song0000", "uniqueId": 99}]
    gen3_fixture.write_table(root / "datatable" / "musicinfo.bin",
                             {"items": items * 3})   # different size
    assert gen3_song.music_info(root, "song0000")["uniqueId"] == 99


def test_bad_chart_does_not_abort_batch(root):
    d = root / "fumen" / "song0002"
    f = d / "song0002_m.bin"
    f.write_bytes(f.read_bytes()[:-20])                 # truncated
    (d / "song0002_h.bin").write_bytes(b"x" * 10)       # too small
    s = gen3_song.load_song(d)
    assert set(s["sht"]) == {"easy", "normal"}
    assert any("oni: chart failed" in w and "song0002_m.bin" in w
               for w in s["warnings"])


def test_folder_with_no_usable_chart_is_reported(root):
    for p in (root / "fumen" / "song0003").glob("*.bin"):
        p.write_bytes(b"\0" * 64)
    (root / "fumen" / "empty").mkdir()
    songs, errs = gen3_song.load_folder(root / "fumen")
    assert sorted(errs) == ["empty", "song0003"]
    assert "no convertible charts" in errs["song0003"]
    assert sorted(songs) == ["song0000", "song0001", "song0002"]


def test_corrupt_datatable_is_a_warning_not_a_crash(root):
    (root / "datatable" / "wordlist.bin").write_bytes(os.urandom(64))
    s = gen3_song.load_song(root / "fumen" / "song0000")
    assert s["title"] == "" and s["stars"]
    assert any("wordlist unreadable" in w for w in s["warnings"])
    with pytest.raises(ValueError):
        gen3_song.words(root, "song_song0000")


def test_missing_datatables(root):
    os.remove(root / "datatable" / "musicinfo.bin")
    s = gen3_song.load_song(root / "fumen" / "song0000")
    assert not s["known"] and s["sht"]
    assert any("musicinfo unreadable" in w for w in s["warnings"])


def test_decrypt_errors_name_the_file(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(os.urandom(16 + 33))                   # not multiple of 16
    with pytest.raises(ValueError, match="truncated"):
        gen3_convert.decrypt_fumen(p)
    p.write_bytes(os.urandom(16 + 48))                   # wrong key / garbage
    with pytest.raises(ValueError, match="a.bin"):
        gen3_convert.decrypt_fumen(p)

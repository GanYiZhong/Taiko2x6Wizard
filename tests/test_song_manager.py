"""Song Manager table: sort / filter / validation / bulk edit / revert.

The three bin parsers are replaced by tiny JSON-backed fakes so the real
SongManager._build_result path (value-only edits) runs on synthetic data.
"""
import json
from types import SimpleNamespace as NS

import pytest

from taiko2x6.songs import song_manager as sm_mod

N = 12
GENRES = [0, 1, 2, 3, 4, 5, 6, 8]


def _sec0(k):
    rec = [0] * 21
    rec[1] = k * 8                       # title offset into the pool
    rec[sm_mod.GENRE_COL] = GENRES[k % len(GENRES)]
    rec[sm_mod.SCORE_COL] = 1000 + k
    rec[17] = rec[19] = 1000 + k
    rec[sm_mod.SCORE3_COL] = 4000 + k
    rec[sm_mod.SCORE5_COL] = 6000 + k
    return rec


def _bins():
    sec0 = [_sec0(k) for k in range(N)]
    titles = b"".join(("title%02d" % k).encode() + b"\0" for k in range(N))
    mi = json.dumps({"sec0": sec0, "pool": titles.decode("latin1")}).encode()
    stars = [[1 + (k % 5), 2 + (k % 5), 3 + (k % 5), 4 + (k % 5)] for k in range(N)]
    tu = json.dumps({"ids": ["song%02d" % k for k in range(N)], "stars": stars}).encode()
    return mi, tu, b"streaminfo"


@pytest.fixture
def mgr_factory(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication
    from taiko2x6.app import appconfig
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    app = QApplication.instance() or QApplication([])
    made = []

    def mi_parse(data):
        d = json.loads(bytes(data))
        return NS(sec0=d["sec0"], pool=d["pool"].encode("latin1"), variant=mi_parse.variant,
                  counts=[len(d["sec0"])], pool_s=d["pool"])

    mi_parse.variant = "t8"
    monkeypatch.setattr(sm_mod.MI, "parse", mi_parse)
    monkeypatch.setattr(sm_mod.MI, "serialize", lambda m: json.dumps(
        {"sec0": m.sec0, "pool": m.pool_s}).encode())

    def tu_parse(data):
        d = json.loads(bytes(data))
        blocks = []
        for st in d["stars"]:
            for _ in (0, 1):                                  # 1P and 2P block
                blocks.append(NS(records=[NS(values=[0] * 5 + [v] + [0] * 21) for v in st]))
        return NS(blocks=blocks, strings=[NS(text="music_" + i) for i in d["ids"]],
                  ids=d["ids"])

    def tu_serialize(t):
        return json.dumps({"ids": t.ids, "stars": [
            [r.values[sm_mod.STAR_INDEX] for r in t.blocks[2 * k].records]
            for k in range(len(t.ids))]}).encode()

    monkeypatch.setattr(sm_mod.TU, "parse", tu_parse)
    monkeypatch.setattr(sm_mod.TU, "serialize", tu_serialize)
    monkeypatch.setattr(sm_mod.SI, "parse", lambda data: NS(records=[], count=0, raw=bytes(data)))
    monkeypatch.setattr(sm_mod.SI, "serialize", lambda s: s.raw)

    def make(variant="t8"):
        mi_parse.variant = variant
        m = sm_mod.SongManager(*_bins())
        made.append(m)
        return m

    yield make
    for m in made:
        m.close()
        m.deleteLater()


def _row(m, sid):
    for r in range(m.tbl.rowCount()):
        if m.tbl.item(r, sm_mod.COL_ID).text() == sid:
            return r
    raise KeyError(sid)


def _tu_stars(res):
    return json.loads(res[sm_mod.TUNING])["stars"]


def test_noop_is_byte_exact(mgr_factory):
    m = mgr_factory()
    assert m._build_result() == {}
    assert m.tbl.rowCount() == N


def test_cell_edit_and_bulk_edit_write_identical_bytes(mgr_factory):
    a = mgr_factory()
    for sid in ("song02", "song05"):
        r = _row(a, sid)
        a.tbl.item(r, sm_mod.COL_GENRE).setText("C-POP")
        a.tbl.item(r, sm_mod.COL_ONI).setText("9")
    a._harvest_table()
    res_a = a._build_result()

    b = mgr_factory()
    songs = [b._song_for_row(_row(b, sid)) for sid in ("song02", "song05")]
    assert b.bulk_set_genre(songs, 8) == 2
    assert b.bulk_set_stars(songs, {3: 9}) == 2
    res_b = b._build_result()

    assert res_a == res_b
    assert set(res_a) == {sm_mod.MUSICINFO, sm_mod.TUNING}
    sec0 = json.loads(res_a[sm_mod.MUSICINFO])["sec0"]
    assert sec0[2][sm_mod.GENRE_COL] == 8 and sec0[5][sm_mod.GENRE_COL] == 8
    stars = _tu_stars(res_a)
    assert stars[2][3] == 9 and stars[5][3] == 9
    assert stars[3] == [4, 5, 6, 7]                           # others untouched


def test_invalid_star_snaps_back_with_message(mgr_factory):
    m = mgr_factory()
    r = _row(m, "song03")
    before = m.tbl.item(r, sm_mod.COL_HARD).text()
    for bad in ("0", "11", "abc", ""):
        m.tbl.item(r, sm_mod.COL_HARD).setText(bad)
        assert m.tbl.item(r, sm_mod.COL_HARD).text() == before
        assert m.lbl_status.text()
    assert m._build_result() == {}
    m.tbl.item(r, sm_mod.COL_HARD).setText("10")
    assert m.lbl_status.text() == ""
    assert _tu_stars(m._build_result())[3][2] == 10


def test_parse_helpers():
    assert sm_mod.parse_genre("c-pop") == 8
    assert sm_mod.parse_genre("5") == 5
    assert sm_mod.parse_genre("genre 9") == 9
    with pytest.raises(ValueError):
        sm_mod.parse_genre("nope")
    assert sm_mod.parse_stars(" 10 ") == 10
    with pytest.raises(ValueError):
        sm_mod.parse_stars("11")


def test_sort_keeps_edits_on_the_right_song(mgr_factory):
    m = mgr_factory()
    m.sort_by(sm_mod.COL_ONI, sm_mod.Qt.DescendingOrder)
    onis = [int(m.tbl.item(r, sm_mod.COL_ONI).text()) for r in range(m.tbl.rowCount())]
    assert onis == sorted(onis, reverse=True)
    m.sort_by(sm_mod.COL_ID, sm_mod.Qt.DescendingOrder)
    assert m.tbl.item(0, sm_mod.COL_ID).text() == "song%02d" % (N - 1)
    r = _row(m, "song07")
    m.tbl.item(r, sm_mod.COL_EASY).setText("10")
    res = m._build_result()
    stars = _tu_stars(res)
    assert stars[7][0] == 10
    assert [s[0] for i, s in enumerate(stars) if i != 7] == \
        [1 + (k % 5) for k in range(N) if k != 7]
    # removing in a sorted view removes that song, not the row index in _songs
    m.tbl.selectRow(_row(m, "song01"))
    m._on_remove()
    ids = [s.id for s in m._songs]
    assert "song01" not in ids and len(ids) == N - 1
    assert m._order == [k for k in range(N) if k != 1]


def test_numeric_sort_is_not_textual(mgr_factory):
    m = mgr_factory()
    m.tbl.item(_row(m, "song00"), sm_mod.COL_SCORE).setText("99999")
    m.sort_by(sm_mod.COL_SCORE, sm_mod.Qt.AscendingOrder)
    assert m.tbl.item(m.tbl.rowCount() - 1, sm_mod.COL_ID).text() == "song00"


def test_search_genre_and_edited_filters(mgr_factory):
    m = mgr_factory()
    visible = lambda: [m.tbl.item(r, sm_mod.COL_ID).text()
                       for r in range(m.tbl.rowCount()) if not m.tbl.isRowHidden(r)]
    m.ed_filter.setText("title03")                           # by title
    assert visible() == ["song03"]
    m.ed_filter.setText("c-pop")                             # by genre name
    assert visible() == ["song07"]
    m.ed_filter.setText("")
    m.cb_genre.setCurrentIndex(m.cb_genre.findData(1))
    assert visible() == ["song01", "song09"]
    m.cb_genre.setCurrentIndex(0)
    assert len(visible()) == N
    m.tbl.item(_row(m, "song04"), sm_mod.COL_NORMAL).setText("7")
    m.chk_changed.setChecked(True)
    assert visible() == ["song04"]
    assert "1" in m.lbl_shown.text() and "12" in m.lbl_shown.text()


def test_changed_rows_marked_and_revert(mgr_factory):
    m = mgr_factory()
    r = _row(m, "song06")
    m.tbl.item(r, sm_mod.COL_ONI).setText("2")
    assert m.tbl.item(r, sm_mod.COL_ONI).data(sm_mod.Qt.BackgroundRole) is not None
    assert m.tbl.item(r, sm_mod.COL_EASY).data(sm_mod.Qt.BackgroundRole) is None
    assert m.b_revert_all.isEnabled()
    assert m._build_result() != {}
    m.tbl.selectRow(r)
    m.revert_selected()
    assert m.tbl.item(r, sm_mod.COL_ONI).data(sm_mod.Qt.BackgroundRole) is None
    assert m._build_result() == {}
    assert not m.b_revert_all.isEnabled()


def test_add_marks_row_and_remove_restores_identity(mgr_factory):
    m = mgr_factory()
    m.tbl.selectRow(_row(m, "song02"))
    m._on_add()
    r = _row(m, "song02_copy")
    assert m.tbl.item(r, sm_mod.COL_IDX).text() == "+"
    assert m.tbl.item(r, sm_mod.COL_TITLE).text() == "title02"
    assert m.tbl.item(r, sm_mod.COL_ID).flags() & sm_mod.Qt.ItemIsEditable
    m.tbl.selectRow(r)
    m._on_remove()
    assert m._build_result() == {}


def test_t14_value_columns_are_read_only(mgr_factory):
    m = mgr_factory("t14")
    flags = m.tbl.item(0, sm_mod.COL_GENRE).flags()
    assert not flags & sm_mod.Qt.ItemIsEditable
    assert not m.tbl.item(0, sm_mod.COL_ONI).flags() & sm_mod.Qt.ItemIsEditable
    assert m.tbl.item(0, sm_mod.COL_STREAM).flags() & sm_mod.Qt.ItemIsEditable
    assert m.bulk_set_genre(m._songs[:3], 8) == 0


def test_layout_is_remembered(mgr_factory):
    m = mgr_factory()
    m.sort_by(sm_mod.COL_ONI, sm_mod.Qt.DescendingOrder)
    m.tbl.setColumnWidth(sm_mod.COL_TITLE, 321)
    m.done(0)
    m2 = mgr_factory()
    assert m2.tbl.columnWidth(sm_mod.COL_TITLE) == 321
    onis = [int(m2.tbl.item(r, sm_mod.COL_ONI).text()) for r in range(m2.tbl.rowCount())]
    assert onis == sorted(onis, reverse=True)

"""Song Manager "Song assets" panel + explorer reverse lookup (synthetic archive)."""
import pytest

pytest.importorskip("PySide6")

from conftest import build_archive  # noqa: E402
from test_song_manager import _bins, mgr_factory  # noqa: E402,F401

from taiko2x6.app.i18n import tr  # noqa: E402

SID = "song03"


@pytest.fixture
def win(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from taiko2x6.app import appconfig
    from taiko2x6.charts import tja2sht
    from gen3_fixture import TJA
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    QApplication.instance() or QApplication([])
    from taiko2x6.app import taiko256_explorer_gui6 as g
    sht = tja2sht.convert_tja(TJA, "oni")
    groups = [
        ("fumen.song031p_e", [("sht", sht)]),
        ("fumen.song031p_m", [("sht", sht)]),
        ("fumen.song032p_m", [("sht", sht)]),
        ("fumen.song041p_m", [("sht", sht)]),
        ("sound.stream.music_song03", [("vag", b"VAGp" + bytes(60))]),
        ("music_texture.kenri_song_song03", [("nut", b"nut-a")]),
        ("music_texture.music_select", [("select_full_song03", b"nut-b"),
                                        ("select_full_song04", b"nut-c")]),
        ("music_texture.music_select_easy", [("select_non_song03", b"nut-d")]),
        ("other.group", [("misc.bin", b"zz")]),
    ]
    lb, db = build_archive(groups)
    lp, dp = tmp_path / "list.bin", tmp_path / "DATA.000"
    lp.write_bytes(lb)
    dp.write_bytes(db)
    w = g.MainWindow()
    w._load(lp, dp)
    yield w, g
    if w.archive:
        w.archive.close()
    w.close()
    w.deleteLater()


def _gi(w, name):
    return next(g["index"] for g in w.archive.layout.groups if g["name"] == name)


def _panel(w, mgr_factory):
    from taiko2x6.songs import song_manager as sm
    mgr_factory()                       # installs the fake bin parsers
    m = sm.SongManager(*_bins(), archive=w.archive, host=w)
    return m


def test_panel_lists_assets_and_reveals(win, mgr_factory):
    w, g = win
    m = _panel(w, mgr_factory)
    try:
        assert m.select_song(SID)
        p = m.assets_panel
        kinds = [(a.kind, a.label) for a in p.assets]
        assert [k for k, _ in kinds].count("chart") == 3          # 1P e, 1P m, 2P m
        assert [l for k, l in kinds if k == "chart"][:2] == ["1P " + tr("Easy"), "1P " + tr("Oni")]
        assert sum(1 for k, _ in kinds if k == "audio") == 1
        tex = [a for a in p.assets if a.kind == "texture"]
        assert len(tex) == 3                    # kenri_song, select_full, select_non
        chart = p.assets[0]
        assert chart.detail == ", ".join([tr("{n} notes").format(n=12), tr("{n} stars").format(n=4)])
        # Show in explorer -> the matching tree item is the current one
        audio_row = next(i for i, a in enumerate(p.assets) if a.kind == "audio")
        assert p.show_row(audio_row)
        cur = w._item_from_proxy(w.tree.currentIndex())
        a = p.assets[audio_row]
        assert (cur.data(g.ROLE_GROUP), cur.data(g.ROLE_FILE)) == (a.gi, a.fi)
        assert w.archive.layout.groups[a.gi]["name"] == "sound.stream.music_song03"
        # selecting a different song re-lists
        assert m.select_song("song04")
        assert {a.gi for a in p.assets} == {_gi(w, "fumen.song041p_m"), _gi(w, "music_texture.music_select")}
    finally:
        m.close()
        m.deleteLater()


def test_preview_opens_reused_widgets(win, mgr_factory):
    w, g = win
    m = _panel(w, mgr_factory)
    try:
        m.select_song(SID)
        p = m.assets_panel
        d = p.preview_row(0)                       # chart
        assert d.pane.currentWidget() is d.pane.chart_view
        assert "fumen.song031p_e" in d.pane.info.toPlainText()
        t = next(i for i, a in enumerate(p.assets) if a.kind == "texture")
        d2 = p.preview_row(t)
        assert d2.pane.currentWidget() is d2._image_tab if hasattr(d2, "_image_tab") \
            else d2.pane.currentWidget() is d2.pane._image_tab
        assert len(p._previews) == 2
        p.close_previews()
    finally:
        m.close()
        m.deleteLater()


def test_reverse_lookup(win):
    w, g = win
    from taiko2x6.songs import song_assets
    for gname, sid in [("fumen.song032p_m", SID), ("sound.stream.music_song03", SID),
                       ("music_texture.kenri_song_song03", SID),
                       ("fumen.song041p_m", "song04")]:
        gi = _gi(w, gname)
        fi = w.archive.layout.groups[gi]["first_file"]
        assert w._song_id_for_item(w._find_file_item(gi, fi)) == sid
    # shared select groups resolve by entry name
    gi = _gi(w, "music_texture.music_select")
    first = w.archive.layout.groups[gi]["first_file"]
    assert w._song_id_for_item(w._find_file_item(gi, first + 1)) == "song04"
    gi = _gi(w, "other.group")
    assert w._song_id_for_item(w._find_file_item(gi, w.archive.layout.groups[gi]["first_file"])) is None
    assert song_assets.song_id_of("music_texture.music_select", "other") is None


def test_menu_action_selects_song(win, mgr_factory, monkeypatch):
    w, g = win
    from taiko2x6.songs import song_manager as sm
    mgr_factory()
    seen = {}

    def fake_exec(self):
        seen["sid"] = self._selected_songs()[0].id
        seen["panel"] = self.assets_panel is not None
        return 0
    monkeypatch.setattr(sm.SongManager, "exec", fake_exec)
    # the fake bins are not in the archive: give it the three files
    monkeypatch.setattr(w, "_find_archive_file", lambda fn: (
        w.archive.layout.groups[0], w.archive.layout.files_for_group(w.archive.layout.groups[0])[0]))
    monkeypatch.setattr(w.archive, "read_file", lambda g_, e_: {
        0: _bins()[0]}.get(0, b""))
    monkeypatch.setattr(sm.SongManager, "__init__", _patched_init(sm.SongManager.__init__))
    w.open_song_manager(select_sid="song05")
    assert seen == {"sid": "song05", "panel": True}


def _patched_init(orig):
    def init(self, mi, tu, si, parent=None, archive=None, host=None):
        mi, tu, si = _bins()
        orig(self, mi, tu, si, parent, archive, host)
    return init

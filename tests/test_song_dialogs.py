"""Song Builder / Song Replacer dialogs construct offscreen, validate inline and
show the parsed TJA summary. Synthetic TJA text only."""
import pytest

pytest.importorskip("PySide6")

TJA = """TITLE:Synthetic
BPM:150
OFFSET:-1.5
COURSE:Oni
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


def test_summarize_tja():
    from taiko2x6.songs import song_dialog_ui as UI
    s = UI.summarize_tja(TJA)
    assert s["title"] == "Synthetic" and s["bpm"] == "150"
    assert s["rows"] == [("easy", "3", 1), ("oni", "8", 6)]
    assert UI.summarize_tja("junk")["warnings"]


@pytest.fixture
def app(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from taiko2x6.app import appconfig
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    from taiko2x6.songs import song_builder
    monkeypatch.setattr(song_builder, "song_ids", lambda a: ["alpha", "beta"])
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("which", ["builder", "replacer"])
def test_dialog_validation_and_summary(app, tmp_path, which):
    from taiko2x6.songs import song_builder, song_replacer
    cls = (song_builder.SongBuilderDialog if which == "builder"
           else song_replacer.SongReplacerDialog)
    dlg = cls(object())
    dlg.ed_tja.setText("")
    dlg.ed_audio.setText("")
    assert dlg.b_build.isEnabled()                     # nothing wrong yet
    dlg.ed_tja.setText(str(tmp_path / "missing.tja"))
    assert not dlg.b_build.isEnabled()                 # missing file blocks Build
    tja = tmp_path / "s.tja"
    tja.write_text(TJA, encoding="utf-8")
    dlg.ed_tja.setText(str(tja))
    assert dlg.b_build.isEnabled()
    assert dlg.tbl_summary.rowCount() == 2
    assert "Synthetic" in dlg.lbl_summary.text()
    dlg.ed_gap.setText("abc")
    assert not dlg.b_build.isEnabled()
    dlg.ed_gap.setText("0")
    if which == "builder":
        dlg.ck_new.setChecked(True)
        assert not dlg.b_build.isEnabled()             # new id required
        dlg.ed_newid.setText("alpha")
        assert not dlg.b_build.isEnabled()             # already exists
        dlg.ed_newid.setText("gamma")
        assert dlg.b_build.isEnabled()
    else:
        for c in (dlg.ck_tex, dlg.ck_chart, dlg.ck_audio, dlg.ck_stars):
            c.setChecked(False)
        assert not dlg.b_build.isEnabled()
    dlg.cb_mode.setCurrentIndex(1)                     # Gen3 mode needs a folder
    assert not dlg.b_build.isEnabled()
    dlg.log.clear()
    for i in range(3):
        dlg._log("line %d" % i)
    assert dlg.log.toPlainText().endswith("line 2")

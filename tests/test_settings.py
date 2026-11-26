"""Settings dialog: dependency status, remembered paths, reset, start-page checklist."""
import pytest

pytest.importorskip("PySide6")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from taiko2x6.app import appconfig
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    monkeypatch.delenv("T14LOAD_BIN", raising=False)
    monkeypatch.delenv("TAIKO_GAME_LIBRARY", raising=False)
    QApplication.instance() or QApplication([])
    return appconfig


def test_dep_strings_translated():
    from taiko2x6.app import i18n, settings_dialog as sd
    keys = [x for d in sd.DEPS for x in (d[1], d[2])]
    assert [k for k in keys if k not in i18n.ZH_TW] == []


def test_saved_t14load_is_found(cfg, tmp_path):
    from taiko2x6.app import settings_dialog as sd
    f = tmp_path / "mine.bin"
    f.write_bytes(b"x")
    cfg.remember("t14load", f)
    assert sd.resolve("t14load") == str(f)


def test_dialog_and_reset(cfg, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from taiko2x6.app import settings_dialog as sd
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg.remember("library", lib)
    cfg.set_value("recent", "folders", "x")
    dlg = sd.SettingsDialog()
    assert str(lib) in dlg._rows["library"].text()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    dlg._reset()
    assert cfg.last_path("library") == ""
    assert cfg.get("recent", "folders") == "x"       # other sections survive


def test_start_page_checklist(cfg, monkeypatch):
    from taiko2x6.app import settings_dialog as sd
    from taiko2x6.app import taiko256_explorer_gui6 as g
    monkeypatch.setattr(sd, "_detect", lambda key: "")
    w = g.MainWindow()
    assert not w.btn_setup.isHidden()
    monkeypatch.setattr(sd, "_detect", lambda key: "/ok")
    w._refresh_setup_box()
    assert w.btn_setup.isHidden() and w.setup_box.isHidden()
    w.close()

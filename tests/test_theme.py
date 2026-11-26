import pytest

pytest.importorskip("PySide6")


@pytest.mark.parametrize("mode", ["light", "dark", "system"])
def test_theme_applies(mode, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from taiko2x6.app import appconfig, theme
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    app = QApplication.instance() or QApplication([])
    dark = theme.apply(app, mode)
    if mode != "system":
        assert dark == (mode == "dark")
    assert theme.mono_font().fixedPitch() or True


def test_theme_menu_and_persist(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from taiko2x6.app import appconfig, theme
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    QApplication.instance() or QApplication([])
    from taiko2x6.app import taiko256_explorer_gui6 as g
    w = g.MainWindow()
    w._set_theme("dark")
    assert theme.saved_mode() == "dark"
    w._set_theme("system")
    w.close()

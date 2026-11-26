"""UI translation table: every tr() string in the translated windows has zh-TW."""
import ast
from pathlib import Path

import pytest

from taiko2x6.app import appconfig, i18n

ROOT = Path(__file__).resolve().parents[1]
TRANSLATED = ["taiko2x6/app/taiko256_explorer_gui6.py", "taiko2x6/songs/omnimix_gui.py",
              "taiko2x6/exe/taiko_exe.py", "taiko2x6/exe/usb_keyboard.py",
              "taiko2x6/ps2/hdd_song_wizard.py", "taiko2x6/ps2/hdd_browser.py",
              "taiko2x6/songs/song_builder.py", "taiko2x6/songs/song_replacer.py",
              "taiko2x6/songs/song_dialog_ui.py", "taiko2x6/app/settings_dialog.py"]


def _tr_keys(path):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "tr" and n.args \
                and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
            yield n.args[0].value


@pytest.mark.parametrize("path", TRANSLATED)
def test_every_tr_string_has_zh_tw(path):
    missing = sorted({k for k in _tr_keys(path) if k not in i18n.ZH_TW})
    assert not missing, missing


def test_dynamic_strings_have_zh_tw():
    from taiko2x6.exe import taiko_exe as T
    keys = [v[0] for v in T.BOOT_PATCHES.values()]
    if hasattr(T, "TaikoBootDialog"):
        keys += list(T.TaikoBootDialog.TIPS.values())
    assert [k for k in keys if k not in i18n.ZH_TW] == []


def test_placeholders_match():
    import string
    fields = lambda s: sorted(f for _, f, _, _ in string.Formatter().parse(s) if f)
    bad = [k for k, v in i18n.ZH_TW.items() if fields(k) != fields(v)]
    assert bad == []


def test_lang_setting_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    monkeypatch.setattr(i18n, "_lang", None)
    i18n.set_lang("zh-TW")
    assert i18n.lang() == "zh-TW"
    assert i18n.tr("Save") == "儲存"
    assert i18n.tr("not in the table") == "not in the table"
    monkeypatch.setattr(i18n, "_lang", None)
    i18n.set_lang("en")
    assert i18n.lang() == "en" and i18n.tr("Save") == "Save"
    with pytest.raises(ValueError):
        i18n.set_lang("fr")


def test_genre_summary_order():
    pytest.importorskip("PySide6")
    from types import SimpleNamespace as NS
    from taiko2x6.songs import omnimix_gui
    items = [NS(genre=g) for g in (8, 0, 6, 0, 4, 8, 8)]
    assert omnimix_gui.genre_summary(items) == [
        ("J-POP", 2), ("ナムコオリジナル", 1), ("ゲームミュージック", 1), ("C-POP", 3)]

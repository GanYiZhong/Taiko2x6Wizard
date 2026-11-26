"""Patch dialogs: state shown before changes, Apply gating, friendly lock error."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from taiko2x6.app import appconfig  # noqa: E402
from taiko2x6.exe import taiko_exe as T, usb_keyboard as U  # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    return QApplication.instance() or QApplication([])


def test_friendly_error_locked():
    assert "PCSX2" in T.friendly_error(PermissionError(13, "denied"))
    assert T.friendly_error(ValueError("x")) == "x"


def test_boot_dialog_apply_disabled_until_changed(app):
    d = T.TaikoBootDialog()
    assert not d.b_patch.isEnabled()
    d._show_state({k: False for k in T.BOOT_PATCHES})
    assert not d.b_patch.isEnabled()
    next(iter(d.cbs.values())).setChecked(True)
    assert d.b_patch.isEnabled()
    assert next(iter(d.state_lbl.values())).text() == T.tr("will turn on")


def test_usb_dialog_buttons_follow_state(app):
    d = U.UsbKeyboardDialog()
    assert not d.b_install.isEnabled() and not d.b_remove.isEnabled()
    d._st = {"installed": False}
    d._update_buttons()
    assert d.b_install.isEnabled() and not d.b_remove.isEnabled()
    d._st = {"installed": True, "build": "full", "files": list(U.HDD_NAMES)}
    d._update_buttons()
    assert not d.b_install.isEnabled() and d.b_remove.isEnabled()
    d.rb_kbdonly.setChecked(True)
    assert d.b_install.isEnabled()

"""Shared fixtures: tiny synthetic game data so the suite runs anywhere (CI
included) without retail files.

Real-data tests (tests/test_real_data.py) look for a game library through
``$TAIKO_GAME_LIBRARY`` and skip when it is not set.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from taiko2x6.archive import taiko256_archive_tool_v2 as core  # noqa: E402


def build_archive(groups, comp: int = 2):
    """Build a format-2 (list.bin, DATA.000) pair in memory.

    ``groups`` is ``[(group_name, [(file_name, bytes), ...]), ...]``. Each group
    gets ``spare`` free sectors after it so in-place patches have room.
    Returns (encrypted_list_bytes, data_bytes)."""
    gc = len(groups)
    fc = sum(len(files) for _, files in groups)
    file_base = (gc + 1) * 32
    name_base = (gc * 2 + fc + 2) * 16
    assert file_base + fc * 16 == name_base

    names = bytearray()

    def add_name(s):
        off = len(names)
        names.extend(s.encode("ascii") + b"\0")
        return off

    lst = bytearray(name_base)
    struct.pack_into("<II", lst, 0, gc, fc)
    data = bytearray()
    fi = 0
    for gi, (gname, files) in enumerate(groups):
        payload = bytearray()
        first = fi
        for fname, blob in files:
            off = core.align(len(payload), core.DATA_ALIGN)
            payload.extend(b"\0" * (off - len(payload)))
            struct.pack_into("<4I", lst, file_base + fi * 16,
                             add_name(fname), len(blob), off, 0)
            payload.extend(blob)
            fi += 1
        # slack so a replacement may grow a little in place
        payload.extend(b"\0" * 256)
        end = core.align(len(payload) + core.PAYLOAD_TRAILER_SIZE, core.PAYLOAD_ALIGN)
        payload.extend(b"\0" * (end - len(payload)))
        packed = core.encode_group_payload(bytes(payload), comp)
        sector = core.align(len(data), core.SECTOR_SIZE) // core.SECTOR_SIZE
        data.extend(b"\0" * (sector * core.SECTOR_SIZE - len(data)))
        struct.pack_into("<8I", lst, 32 + gi * 32, add_name(gname), len(files),
                         first, comp, sector, len(packed), len(payload),
                         core.compute_unknown2(bytes(payload)))
        data.extend(packed)
        # spare sectors between groups
        data.extend(b"\0" * (core.SECTOR_SIZE * 4))
    lst.extend(names)
    lst.extend(b"\0" * (core.align(len(lst), 16) - len(lst)))
    return core.crypt_list(bytes(lst)), bytes(data)


SAMPLE_GROUPS = [
    ("song.abc", [("abc_m.sht", b"M" * 300), ("abc.vag", bytes(range(256)) * 4)]),
    ("song.xyz", [("xyz_m.sht", b"X" * 120)]),
    ("bins", [("musicinfo.bin", b"\x01\x02\x03\x04" * 64)]),
]


@pytest.fixture
def archive_pair(tmp_path):
    """(list_path, data_path) of a small synthetic archive."""
    lb, db = build_archive(SAMPLE_GROUPS)
    lp, dp = tmp_path / "list.bin", tmp_path / "DATA.000"
    lp.write_bytes(lb)
    dp.write_bytes(db)
    return lp, dp


@pytest.fixture
def pfs_image(tmp_path):
    """(img_path, partition, file_path) of a tiny synthetic APA/PFS image."""
    from taiko2x6.ps2 import ps2hdd
    img = tmp_path / "synthetic.img"
    pname, fpath = ps2hdd._build_synthetic_pfs(str(img), part_zones=4096)
    return img, pname, fpath


@pytest.fixture(autouse=True)
def _close_leftover_windows():
    """Close and delete every top-level widget a test leaves open.

    Dialogs a test never closes outlive it; later app-wide operations (a
    theme's setStyleSheet repolishes every widget) or interpreter shutdown,
    with widgets outliving the QApplication, then crashed the run."""
    yield
    if "PySide6.QtWidgets" not in sys.modules:
        return
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        return
    for w in app.topLevelWidgets():
        w.close()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


@pytest.fixture(autouse=True)
def _isolated_appconfig(tmp_path, monkeypatch):
    """Never let a test write the real config.ini (dialog geometry is saved
    there every time a dialog is hidden)."""
    from taiko2x6.app import appconfig
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config_auto.ini")
    yield


@pytest.fixture(autouse=True)
def _isolated_qsettings(tmp_path, monkeypatch):
    """Point every taiko2x6 module's QSettings at a throwaway INI file.

    QSettings("TaikoTools", ...) writes the Windows registry; dialogs under
    test (HDD browser / wizard) stored their synthetic test image there as
    the user's real "recent HDD image"."""
    if "PySide6.QtCore" not in sys.modules:
        yield
        return
    from PySide6.QtCore import QSettings as _Real
    ini = str(tmp_path / "qsettings.ini")

    def _fake(*_args, **_kw):
        return _Real(ini, _Real.IniFormat)

    for name, mod in list(sys.modules.items()):
        if name.startswith("taiko2x6") and getattr(mod, "QSettings", None) is _Real:
            monkeypatch.setattr(mod, "QSettings", _fake)
    yield

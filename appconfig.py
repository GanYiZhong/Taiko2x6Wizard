#!/usr/bin/env python3
"""
Tiny shared settings store (config.ini) for the Taiko tools.

Remembers the last-used path per picker ("tja", "audio", "archive_dir", …) so
every QFileDialog opens where the user last was instead of the CWD. The ini
lives next to the scripts:  E:\\Taiko No Tatsujin 8\\config.ini

    [paths]
    tja = G:/Downloads/songs/BUTTERFLY.tja
    audio = G:/Downloads/songs/BUTTERFLY.ogg

Use the pick_* wrappers where possible — they open the dialog at the
remembered location AND store the choice in one call:

    p = appconfig.pick_open(self, "tja", "TJA chart", "TJA (*.tja)")

All functions swallow I/O errors: a broken/readonly config.ini must never
crash a tool, it just loses the convenience.
"""
from __future__ import annotations

import configparser
from pathlib import Path

import apppaths
INI_PATH = apppaths.resource_dir() / "config.ini"
_SECTION = "paths"


def _load() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    if INI_PATH.exists():
        try:
            cp.read(INI_PATH, encoding="utf-8")
        except Exception:
            pass                                   # corrupt ini -> start fresh
    return cp


def last_path(key: str, default: str = "") -> str:
    """The exact path last remembered for `key` ('' if none)."""
    return _load().get(_SECTION, key, fallback=default)


def last_dir(key: str, default: str = "") -> str:
    """Directory of the last path for `key`, if it still exists."""
    p = last_path(key)
    if p:
        d = Path(p)
        if not d.is_dir():
            d = d.parent
        if d.is_dir():
            return str(d)
    return default


def remember(key: str, path) -> None:
    """Persist `path` as the last-used value for `key` (immediate write)."""
    if not path:
        return
    cp = _load()
    if not cp.has_section(_SECTION):
        cp.add_section(_SECTION)
    cp.set(_SECTION, key, str(path))
    try:
        with open(INI_PATH, "w", encoding="utf-8") as f:
            cp.write(f)
    except OSError:
        pass                                       # readonly dir -> just skip


def last_existing(key: str) -> str:
    """The last path for `key` only if it still exists on disk ('' otherwise).
    Use to pre-fill line edits without resurrecting deleted files."""
    p = last_path(key)
    return p if p and Path(p).exists() else ""


def key_for_filter(filt: str) -> str:
    """Derive a stable settings key from a QFileDialog filter string.

    "TJA charts (*.tja)" -> "tja",  "Audio (*.wav *.ogg)" -> "wav".
    Lets shared _pick(edit, filt) helpers remember per-file-type paths
    without changing every caller to pass an explicit key.
    """
    import re
    m = re.search(r"\*\.(\w+)", filt or "")
    return m.group(1).lower() if m else "file"


def _start_for(key: str) -> str:
    """Best starting point for a dialog: the last file if it still exists,
    else its directory, else ''. Qt pre-selects the file when given one."""
    p = last_path(key)
    if p and Path(p).exists():
        return p
    return last_dir(key)


# --------------------------------------------------------------------------- #
#  Qt convenience wrappers (import Qt lazily so headless use stays Qt-free)
# --------------------------------------------------------------------------- #
def pick_open(parent, key: str, title: str = "Choose file",
              filt: str = "All (*)") -> str:
    """getOpenFileName starting at the remembered spot; remembers the pick."""
    from PySide6.QtWidgets import QFileDialog
    p, _ = QFileDialog.getOpenFileName(parent, title, _start_for(key), filt)
    if p:
        remember(key, p)
    return p


def pick_open_many(parent, key: str, title: str = "Choose files",
                   filt: str = "All (*)") -> list:
    """getOpenFileNames variant; remembers the first pick."""
    from PySide6.QtWidgets import QFileDialog
    paths, _ = QFileDialog.getOpenFileNames(parent, title, _start_for(key), filt)
    if paths:
        remember(key, paths[0])
    return paths


def pick_save(parent, key: str, title: str = "Save file",
              default_name: str = "", filt: str = "All (*)") -> str:
    """getSaveFileName starting at the remembered dir; remembers the pick.

    `default_name` (just a file name) is joined onto the remembered directory
    so 'Export WAV' style dialogs keep their suggested name.
    """
    from PySide6.QtWidgets import QFileDialog
    start = last_dir(key)
    if default_name:
        start = str(Path(start) / default_name) if start else default_name
    p, _ = QFileDialog.getSaveFileName(parent, title, start, filt)
    if p:
        remember(key, p)
    return p


def pick_dir(parent, key: str, title: str = "Choose folder") -> str:
    """getExistingDirectory starting at the remembered dir; remembers it."""
    from PySide6.QtWidgets import QFileDialog
    p = QFileDialog.getExistingDirectory(parent, title, last_dir(key))
    if p:
        remember(key, p)
    return p


if __name__ == "__main__":
    import tempfile, os
    # self-test with a throwaway ini
    INI_PATH = Path(tempfile.mkdtemp()) / "config.ini"   # type: ignore
    remember("tja", r"C:\Windows\notepad.exe")
    assert last_path("tja") == r"C:\Windows\notepad.exe"
    assert last_dir("tja").lower() == r"c:\windows"
    assert _start_for("tja") == r"C:\Windows\notepad.exe"
    remember("tja", r"C:\definitely\missing\file.tja")
    assert _start_for("tja") == ""                       # gone -> no start dir
    print("appconfig self-test OK ->", INI_PATH)

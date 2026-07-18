#!/usr/bin/env python3
"""Where user-supplied assets live, whether run from source or a frozen exe.

The toolkit loads a few files relative to itself — the 勘亭流 font (``Font.ttf``),
``T14LOAD.bin``, the ``vgmstream-win64`` folder, ``pfsshell.exe`` and
``config.ini``. When running from source those sit next to the .py modules.
When bundled into a single-file PyInstaller ``.exe`` the modules are unpacked to
a throwaway temp dir, so instead we look **next to the .exe** — that is where a
player drops their own copies and where ``config.ini`` should be written.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Directory of the .py modules (or the PyInstaller temp unpack dir when frozen).
MODULE_DIR = Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Folder to search for user-supplied assets and to store config.ini.

    Frozen (single-file exe): the folder containing the running .exe.
    From source: the folder containing the toolkit modules.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return MODULE_DIR


def is_frozen() -> bool:
    """True when running inside a PyInstaller/py2exe bundle."""
    return bool(getattr(sys, "frozen", False))

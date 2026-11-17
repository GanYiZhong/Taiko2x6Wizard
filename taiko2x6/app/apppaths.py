#!/usr/bin/env python3
"""Where user-supplied assets live, whether run from source or a frozen exe.

The toolkit loads a few files relative to itself — the 勘亭流 font (``Font.ttf``),
``T14LOAD.bin``, the ``vgmstream-win64`` folder, ``pfsshell.exe`` and
``config.ini``. When running from source those sit in the project root (the
folder that holds ``main.py`` and the ``taiko2x6`` package).
When bundled into a single-file PyInstaller ``.exe`` the modules are unpacked to
a throwaway temp dir, so instead we look **next to the .exe** — that is where a
player drops their own copies and where ``config.ini`` should be written.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Project root: taiko2x6/app/apppaths.py -> parents[2]
# (or the PyInstaller temp unpack dir when frozen).
PROJECT_DIR = Path(__file__).resolve().parents[2]


def resource_dir() -> Path:
    """Folder to search for user-supplied assets and to store config.ini.

    Frozen (single-file exe): the folder containing the running .exe.
    From source: the project root (next to main.py).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return PROJECT_DIR


def samples_dir() -> Path:
    """Folder of developer sample files used by the modules' ``__main__``
    self-tests (e.g. ``bins/musicinfo.bin``) and where they write output.

    ``$TAIKO_SAMPLES`` if set, else ``<project root>/samples`` (git-ignored).
    """
    env = os.environ.get("TAIKO_SAMPLES")
    return Path(env) if env else resource_dir() / "samples"


def is_frozen() -> bool:
    """True when running inside a PyInstaller/py2exe bundle."""
    return bool(getattr(sys, "frozen", False))

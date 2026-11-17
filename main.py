#!/usr/bin/env python3
"""Taiko2x6Wizard launcher - opens the Explorer GUI.

    python main.py                      # open the Explorer
    python main.py "D:/Taiko/extract"   # open a folder holding DATA.000 + list.bin

Individual tools run as modules, e.g. ``python -m taiko2x6.exe.taiko_exe``.
"""
from taiko2x6.app.taiko256_explorer_gui6 import main

if __name__ == "__main__":
    main()

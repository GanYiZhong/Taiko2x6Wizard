================================================================================
  Taiko2x6Wizard  —  arcade Taiko no Tatsujin modding toolkit  (Linux build)
================================================================================

WHAT THIS IS
------------
A single-file 64-bit Linux program (built on Ubuntu 22.04, needs glibc 2.35 or
newer). Python and every library are inside it. Run:

    ./Taiko2x6Wizard

The first launch takes a few seconds while it unpacks itself.

If it fails with a Qt "xcb" platform plugin error, install the X11 libraries
Qt needs, for example on Debian / Ubuntu:

    sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1


FILES YOU ADD YOURSELF  (put them NEXT TO the program)
------------------------------------------------------
  1. Font.ttf      — the 勘亭流 brush font (DFPKanTeiRyu-XB), your own licensed
                     copy renamed to "Font.ttf". Needed to draw song-title art.
  2. T14LOAD.bin   — from your own Taiko 14 game. Needed by the taiko exe
                     patches (song limit, timers, boot & select patches) and
                     the Omnimix song-limit step.


WINDOWS-ONLY PARTS
------------------
  - "Slim PS2 HDD image" uses NTFS sparse files and is disabled on Linux.
  - pfsshell.exe and vgmstream-win64 are Windows programs. The native HDD
    reader/writer (used by Omnimix and the exe patches) works on Linux without
    pfsshell. For Gen3 (Nijiiro) audio import, put a Linux vgmstream-cli on
    your PATH.


Work on COPIES of game files and HDD images; keep backups.

================================================================================
  Taiko2x6Wizard  —  arcade Taiko no Tatsujin modding toolkit
================================================================================

WHAT THIS IS
------------
A single-file Windows program. Just run:

    Taiko2x6Wizard.exe

Python and every library are already inside the .exe — you do NOT need to
install Python or run pip. The program opens the Explorer GUI; all the other
tools (song add/replace, .bin editors, chart converters, HDD tools, exe
patcher) are launched from its menus.


FOUR FILES YOU MUST ADD YOURSELF  (put them NEXT TO the .exe)
------------------------------------------------------------
These are NOT included, because they are either a commercial font, third-party
tools, or data that can only come from your own game. The .exe automatically
looks for them in its own folder.

  1. Font.ttf
       The 勘亭流 brush font (DFPKanTeiRyu-XB) used to draw song-title art.
       Supply your own licensed copy and rename it exactly "Font.ttf".
       (Without it, title-texture rendering is disabled; everything else works.)

  2. vgmstream-win64\   (a folder)
       Needed only to import Gen3 / Nijiiro songs (decodes their .nus3bank
       audio). Download the win64 build from https://vgmstream.org/ and put the
       extracted "vgmstream-win64" folder here so that
       vgmstream-win64\vgmstream-cli.exe exists.

  3. pfsshell.exe
       Optional. An alternative PS2 PFS file tool. The built-in ps2hdd engine
       already handles HDD images, so this is only a fallback.
       Source: https://github.com/uyjulian/pfsshell

  4. T14LOAD.bin
       Needed only for the Taiko 14+ executable patcher (e.g. raising the
       song-count ceiling). It carries the cipher key material and can only be
       extracted from your own game/loader. Put it here if you use that tool.

So a fully-loaded folder looks like:

    Taiko2x6Wizard.exe
    Font.ttf
    T14LOAD.bin
    pfsshell.exe
    vgmstream-win64\vgmstream-cli.exe
    config.ini            (created automatically; remembers your last paths)


IMPORTANT SAFETY NOTES
----------------------
* Always work on COPIES of your game files, and keep backups. Several
  operations (writing DATA.000, patching the exe, writing an HDD image) are
  not reversible.
* When writing to a PS2 HDD .img, CLOSE PCSX2 first — it locks the image for
  writing. (Reading while it runs is fine.)
* This toolkit contains NO game content. "Taiko no Tatsujin" and related marks
  belong to Bandai Namco. Use only with files you are legally entitled to.


Source code, docs, and updates:
    https://github.com/GanYiZhong/Taiko2x6Wizard

Licensed under the MIT License (tooling only) — see the repository.
================================================================================

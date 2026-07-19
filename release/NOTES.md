## Taiko2x6Wizard v1.0.0 — first release

A **single-file Windows program** for modding arcade _Taiko no Tatsujin_
(SYSTEM256 / SYSTEM2x6). Python and every library are bundled inside the
`.exe` — **no Python or pip install needed**. Just download and run.

### Download
- **`Taiko2x6Wizard-v1.0.0-win64.zip`** — the exe + a README_FIRST (recommended)
- `Taiko2x6Wizard.exe` — the bare executable

Run `Taiko2x6Wizard.exe`; it opens the Explorer GUI and every tool is launched
from its menus (song add/replace, per-song `.bin` editors, TJA & Gen3/Nijiiro
chart conversion, VAG audio, TIM2 textures, native PS2 HDD / memory-card I/O,
and the T14+ exe patcher).

### Four files you add yourself (put them next to the .exe)
Not bundled — a commercial font, third-party tools, or your own game data. The
exe auto-detects them in its folder. See **README_FIRST.txt** in the zip.

| File | Needed for | Where |
|------|-----------|-------|
| `Font.ttf` (勘亭流 / DFPKanTeiRyu-XB) | song-title art | your own licensed copy |
| `vgmstream-win64/` | Gen3 (Nijiiro) audio import | https://vgmstream.org/ |
| `pfsshell.exe` | optional PFS fallback | https://github.com/uyjulian/pfsshell |
| `T14LOAD.bin` | T14+ exe patcher | extracted from your own game |

### Notes
- 64-bit Windows. First launch may take a moment (onefile self-extracts).
- Work on **copies** of game files; keep backups. Writing `DATA.000`, patching
  the exe, or writing an HDD image is not reversible.
- **Close PCSX2** before writing to an HDD `.img` (it locks the file).
- Contains **no game content**. Not affiliated with Bandai Namco. MIT-licensed
  tooling — source at https://github.com/GanYiZhong/Taiko2x6Wizard

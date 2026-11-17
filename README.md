# Taiko2x6Wizard

**English** | [繁體中文](README.zh-TW.md)

A Python + Qt (PySide6) toolkit for **reverse-engineering and modding arcade
_Taiko no Tatsujin_** — the SYSTEM256 / SYSTEM2x6 (PS2-based) generations. Browse
the game's archives, add or replace songs (charts, audio, textures, database
entries), edit the per-song `.bin` tables, convert charts from TJA and from
Gen3 (Nijiiro) fumen, and read/write PS2 HDD images and memory cards — all from
one Explorer GUI.

> **Independent fan project — no game content included.** This repository ships
> only original tooling. It contains no game code, audio, textures, fonts, or
> data. You must supply your own legally obtained files (see
> [What you must supply](#what-you-must-supply)). Not affiliated with or
> endorsed by Bandai Namco.

---

## What it does

- **Archive Explorer** — open a game's `DATA.000` + `list.bin` container, browse
  every group/file, preview textures, play audio, and edit in place.
- **Add / replace songs** end-to-end: title textures, charts (fumen), audio
  (VAG), and all the per-song database rows, then rebuild the archive.
- **Chart conversion** — TJA → SHT, and encrypted **Gen3 (Nijiiro)** fumen → SHT.
- **Per-song `.bin` editors** — `musicinfo`, `tuning`, `streaminfo`, `rank`,
  `lamp`, `fname`, `hdbdinfo`, `enso_parts` — auto-discovered by the Explorer.
- **Audio** — VAG (PS2 ADPCM) encode/decode, OGG/WAV import, `hd/bd` sound-bank
  playback.
- **Graphics** — TIM2 image encode/decode, 勘亭流 song-title texture generation,
  SWG sprite/animation editing, flipbook animation playback.
- **PS2 storage I/O** — native APA-partition + PFS filesystem read/write
  (`ps2hdd.py`), memory-card read/write, ISO packing, multi-game HDD merge, and
  image slimming.
- **Executable patching** — decrypt and patch the T14+ `taiko` ELF (e.g. raise
  the on-disc song-count ceiling).

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the module map and
[`docs/FILE_FORMATS.md`](docs/FILE_FORMATS.md) for the reverse-engineered
formats.

## Download (no Python needed)

Grab the latest **`Taiko2x6Wizard.exe`** from the
[Releases page](https://github.com/GanYiZhong/Taiko2x6Wizard/releases) — a
single Windows file with Python and all libraries bundled in. Run it; no
install required.

Four files are **not** bundled (commercial font / third-party tools / your own
game data) — drop them next to the `.exe` if you need the features that use
them: `Font.ttf`, `vgmstream-win64/`, `pfsshell.exe`, `T14LOAD.bin`. See
[What you must supply](#what-you-must-supply). Developers who want to run from
source can follow the sections below instead.

---

## Requirements

- **Python 3.10+** (uses `sys.stdlib_module_names`, `str | None` syntax).
- Windows is the primary/tested platform (the PS2 tooling and native binaries
  are Windows-oriented), but the pure-Python parsers are cross-platform.

```bash
pip install -r requirements.txt
```

Core deps: `PySide6`, `numpy`, `Pillow`, `pycryptodome`. Audio import also wants
`soundfile` (and optionally `librosa`). See [`requirements.txt`](requirements.txt).

## What you must supply

These are **not** in the repo (git-ignored) because they are third-party or
game-derived. Place each in the project root (next to `main.py`):

| File / folder      | What it is                                         | Where to get it |
|--------------------|----------------------------------------------------|-----------------|
| `Font.ttf`         | 勘亭流 brush font (DFPKanTeiRyu-XB) for title art   | Supply your own licensed copy, renamed `Font.ttf` |
| `T14LOAD.bin`      | Loader blob used to derive the T14 `taiko` key      | Extract from your own game/dongle |
| `pfsshell.exe`     | Reference PFS tool (optional alt to `ps2hdd.py`)    | [uyjulian/pfsshell](https://github.com/uyjulian/pfsshell) releases |
| `vgmstream-win64/` | vgmstream CLI, for decoding Gen3 `.nus3bank` audio  | [vgmstream.org](https://vgmstream.org/) — extract the win64 build here |

Game data itself (`DATA.000`, `list.bin`, `.img`, `.iso`, `.ps2`, fumen, audio)
is yours to provide and is git-ignored so it never lands in the repo.

---

## Quick start

Run everything from the project root:

```bash
cd Taiko2x6Wizard
python main.py                       # main Explorer GUI
# optionally pass a folder that holds DATA.000 + list.bin:
python main.py "D:/Taiko/extract"
```

The Explorer is the hub — the per-song `.bin` editors and most tools are opened
from its menus. Several modules also run standalone with `python -m`, e.g.:

```bash
python -m taiko2x6.exe.taiko_exe                 # T14 exe patcher — GUI
python -m taiko2x6.bineditors.bineditor_musicinfo  # edit musicinfo.bin — GUI
python -m taiko2x6.charts.gen3_convert --help    # Gen3 (Nijiiro) fumen -> SHT — CLI
```

From your own scripts, import from the package (run them from the project root,
or put it on `PYTHONPATH`):

```python
from taiko2x6.ps2 import ps2hdd
from taiko2x6.songs.song_manager import SongManager
```

`config.ini` (git-ignored) just remembers your last-used paths. Copy
[`config.ini.example`](config.ini.example) to `config.ini` to pre-seed it, or
let the GUI fill it in as you browse.

---

## Common workflows

- **Add a song to a T14+ HDD image** — extract `list.bin`/`DATA.000` from the
  image's game partition (`ps2/ps2hdd`), add the song with `songs/song_builder`
  (textures + charts + audio + DB rows), rebuild with `archive/archive_builder`,
  write back with `ps2/ps2hdd`. If you exceed the on-disc song ceiling, patch it
  first with `exe/taiko_exe`.
- **Import a TJA chart** — `charts/tja2sht` converts the chart; import the
  OGG/WAV as VAG with `audio/vagtool`; render the title texture with
  `graphics/songtex_all`.
- **Import a Gen3 (Nijiiro) song** — `charts/gen3_song` / `charts/gen3_convert`
  decrypt and convert the fumen and decode the audio (via `vgmstream-win64/`).
- **Fuse several games into one (Omnimix)** — Tools → *Omnimix Maker*
  (`songs/omnimix_maker`).

> ⚠️ **Safety.** When writing to a PS2 HDD `.img`, **close PCSX2 first** — it
> locks the image for writing (reads are fine while it runs). Always work on
> **copies** of your game files and keep backups; several operations are
> not reversible.

---

## Repository layout

```
Taiko2x6Wizard/
├── README.md                  this file
├── README.zh-TW.md            Traditional Chinese README
├── LICENSE                    MIT (tooling only; no game content)
├── requirements.txt           Python dependencies
├── .gitignore                 excludes game data + supplied binaries
├── config.ini.example         template for local path memory
├── main.py                    launcher — opens the Explorer GUI
├── Taiko2x6Wizard.spec        PyInstaller build (single-file exe)
├── taiko2x6/                  the toolkit package
│   ├── app/                    Explorer GUI (hub), config + asset paths
│   ├── archive/                DATA.000 + list.bin read / rebuild
│   ├── songs/                  song add / replace / DB manager, Omnimix
│   ├── bineditors/             per-song .bin table editors (auto-discovered)
│   ├── charts/                 TJA / Gen3 -> SHT, SHT validator
│   ├── audio/                  VAG, hd/bd sound banks, playback
│   ├── graphics/               TIM2, title art, SWG sprites, flipbooks
│   ├── ps2/                    PS2 HDD (APA/PFS), memory card, ISO, image tools
│   └── exe/                    T14+ taiko executable patcher
├── release/                   release notes
└── docs/
    ├── ARCHITECTURE.md         module map + how the pieces fit
    ├── FILE_FORMATS.md         reverse-engineered on-disc formats
    ├── notecolor_report.md     RE note: note-color encoding
    ├── unknown2_report.md      RE note: LIST.BIN group hash
    ├── PFSSHELL_README.md      upstream pfsshell notes
    └── reviews/                internal design/code-review notes
```

> `Font.ttf`, `T14LOAD.bin`, `pfsshell.exe`, `vgmstream-win64/` and `config.ini`
> are looked up in the project root (or next to the `.exe` in the frozen build)
> via `taiko2x6/app/apppaths.py`. New `bineditor_*.py` files dropped into
> `taiko2x6/bineditors/` are picked up by the Explorer automatically.

---

## License

MIT — see [`LICENSE`](LICENSE). The MIT grant covers **this tooling only**; it
does not grant any rights to game content, and the toolkit ships none.

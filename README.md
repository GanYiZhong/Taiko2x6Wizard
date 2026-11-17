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
- **Omnimix** — fuse the songs of several Taiko 8…14 images into one: dedup by
  song id, auto song-limit patch, and partition grow when the data outgrows it.
- **Chart conversion** — TJA → SHT, and encrypted **Gen3 (Nijiiro)** fumen → SHT
  (keeps diverge branches, bpm-aware measure timing).
- **Per-song `.bin` editors** — `musicinfo`, `tuning`, `streaminfo`, `rank`,
  `lamp`, `fname`, `hdbdinfo`, `enso_parts` — auto-discovered by the Explorer.
- **Audio** — VAG (PS2 ADPCM) encode/decode, OGG/WAV import, `hd/bd` sound-bank
  playback.
- **Graphics** — TIM2 image encode/decode, 勘亭流 song-title texture generation,
  SWG sprite/animation editing, flipbook animation playback.
- **PS2 storage I/O** — native APA-partition + PFS filesystem read/write
  (`ps2/ps2hdd`), memory-card read/write, ISO packing, multi-game HDD merge, and
  image slimming.
- **Executable patching** — decrypt and patch the T14+ `taiko` ELF: raise the
  song-count ceiling, change the song-select timer, skip the boot Notice screen.
- **USB keyboard patch (Taiko 14)** — play with a plain USB keyboard and no JVS
  I/O board ([KBD2EE](https://github.com/GanYiZhong/Taiko14-USB-Keyboard-Patch)),
  installed onto the HDD image (card untouched) or onto the dongle card image.

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

- **Python 3.10+**.
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
python -m taiko2x6.exe.taiko_exe TK14.img -n 214 --backup taiko.bak  # raise the song limit — CLI
python -m taiko2x6.exe.taiko_exe TK14.img --select-timer 999          # song-select timer — CLI
python -m taiko2x6.exe.taiko_exe --help                               # all exe-patch options
python -m taiko2x6.exe.usb_keyboard install TK14.img --card NM00057.ps2  # USB keyboard patch
python -m taiko2x6.charts.gen3_convert --help    # Gen3 (Nijiiro) fumen -> SHT — CLI
```

(The exe patches also have GUI dialogs in the Explorer's Tools menu.)

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
- **Play Taiko 14 with a USB keyboard** — Tools → *USB keyboard patch (T14)*
  (`exe/usb_keyboard`). Keys: 1P <kbd>D</kbd> <kbd>F</kbd> <kbd>J</kbd> <kbd>K</kbd>,
  2P <kbd>Z</kbd> <kbd>X</kbd> <kbd>C</kbd> <kbd>V</kbd>, COIN <kbd>5</kbd>,
  SERVICE <kbd>9</kbd>, TEST <kbd>F1</kbd>, SELECT <kbd>↑</kbd> <kbd>↓</kbd>
  <kbd>Enter</kbd>; unplug and replug the keyboard once after boot.
  - *HDD image* (default): `taiko` loads `pfs0:IRXARC.BIN;1` from its own
    partition before falling back to the card's `mc0:IRXARC.BIN`, so the tool
    writes "stock card IRXARC + KBD2EE" into `t14jp1400.0001`. The first
    install needs the stock archive once (the dongle card image or a dumped
    `IRXARC.BIN`); the card itself is not changed. **Not yet tested on
    hardware.** *Remove* deletes the files and the game uses the card again.
  - *Card image*: the original, hardware-tested method — patches the card's
    `IRXARC.BIN` and `T14LOAD` (a `.bak` of the card is made first).
  - Before installing, the image's `taiko` is decrypted and checked: KBD2EE
    overwrites seven fixed code blocks and uses a fixed memory area, so on a
    different `taiko` build it would corrupt live code. A mismatch refuses
    the install. The key comes from the card image, `--t14load`, or
    `T14LOAD.bin`; in card mode give the HDD image (`--img`) to check it.
    The song-limit and Omnimix patches do not touch those blocks.
    `python -m taiko2x6.exe.usb_keyboard check TK14.img --card NM00057.ps2`
    runs the check alone.
- **Fuse several games into one (Omnimix)** — Tools → *Omnimix Maker*
  (`songs/omnimix_maker`). Up to 214 songs is proven in-game; above that the
  data is written correctly but whether the select wheel shows and plays every
  song is untested — try it on a copy first.

> ⚠️ **Safety.** When writing to a PS2 HDD `.img`, **close PCSX2 first** — it
> locks the image for writing (reads are fine while it runs). Working on
> **copies** of your game files is still the safest habit.

### How writes are protected

Every tool that changes game files goes through `taiko2x6/safeio.py`:

| What is written | Protection |
|---|---|
| `DATA.000` + `list.bin` (Explorer save, Song Builder, archive CLI, Omnimix to a folder) | Both files are committed together or not at all. Overwriting keeps a rolling `.bak`, `.bak.1`, …. If the app dies mid-save, the next open of the archive finishes or undoes the save automatically. |
| PS2 HDD `.img` (HDD Browser, HDD Song Wizard, Omnimix Maker, `taiko_exe`, USB keyboard patch) | Before any sector is overwritten, its old contents go to an **undo journal** `<img>.<date-time>.undo` (about the size of the data written, not the whole image). A failed write is rolled back on the spot; the journal is kept as the backup of a successful write. |
| Memory card `.ps2` | A `.bak` copy is made before the card is opened for writing. |
| ISO UDF repair | The old header is saved to an undo journal. |

To undo an image write later (or after a crash/power loss):

```bash
python -m taiko2x6.safeio info     TK14.img.20261118-101500.undo
python -m taiko2x6.safeio rollback TK14.img.20261118-101500.undo
```

Roll journals back newest first, and delete them when you no longer need them.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `T14LOAD.bin not found` | Put `T14LOAD.bin` in the project root (or next to the `.exe`), or set `T14LOAD_BIN` to its path. |
| `vgmstream-cli not found` | Extract the vgmstream win64 build so `vgmstream-win64/vgmstream-cli.exe` exists, or pass `--vgmstream`. |
| `pfsshell.exe not found` | Only needed for the pfsshell path; the built-in `ps2/ps2hdd` engine works without it. |
| Song add fails mentioning `Font.ttf` | Supply the 勘亭流 font as `Font.ttf` (see above). |
| Can't write the `.img` / permission error | Close PCSX2 — it locks the image for writing. |
| A song freezes when picked (Taiko 8) | The group's `unknown2` hash is wrong; rebuild with `archive_builder` (see `docs/unknown2_report.md`). |
| Song added but not visible (T14) | musicinfo self-index columns must point at the new row — use Song Manager / Song Builder, not a raw row copy. |
| Image broken after a write / want the old one back | `python -m taiko2x6.safeio rollback <img>.<date-time>.undo` (newest journal first). |
| `.txn` / `.txn-old` files next to `DATA.000` | Left by an interrupted save; opening the archive cleans them up. |
| Need more detail | Start with `python main.py --debug` (or set `TAIKO_LOG=DEBUG`). |

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite builds tiny synthetic archives and PFS images, so it needs no game
files; GitHub Actions runs it on Windows and Linux. Tests against your own
game files run only when you point `TAIKO_GAME_LIBRARY` at a folder of game
directories (originals are opened read-only; writes happen on copies in the
pytest temp dir). `TAIKO_HEAVY=1` adds a test that copies a multi-GB image:

```bash
TAIKO_GAME_LIBRARY=E:/games/system2x6 TAIKO_HEAVY=1 python -m pytest --basetemp=E:/pytest-tmp
```

Per-module self-tests (`python -m taiko2x6.<sub>.<module>`) look for sample
files in `samples/` (git-ignored), or wherever `TAIKO_SAMPLES` points.

---

## Repository layout

```
Taiko2x6Wizard/
├── README.md                  this file
├── README.zh-TW.md            Traditional Chinese README
├── LICENSE                    MIT (tooling only; no game content)
├── requirements.txt           Python dependencies
├── requirements-dev.txt       test dependencies (pytest)
├── pytest.ini                 test settings
├── .gitignore                 excludes game data + supplied binaries
├── config.ini.example         template for local path memory
├── main.py                    launcher — opens the Explorer GUI
├── Taiko2x6Wizard.spec        PyInstaller build (single-file exe)
├── tests/                     pytest suite (synthetic data; real-data tests opt-in)
├── .github/workflows/         CI (pytest on Windows + Linux)
├── taiko2x6/                  the toolkit package
│   ├── safeio.py               crash-safe writes: atomic, DATA+LIST transaction, undo journal
│   ├── app/                    Explorer GUI (hub), config + asset paths
│   ├── archive/                DATA.000 + list.bin model, read / save / rebuild
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

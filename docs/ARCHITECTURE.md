# Architecture

Taiko2x6Wizard is a Python package (`taiko2x6/`) of PySide6/CLI modules that
all operate on the arcade _Taiko no Tatsujin_ data formats. The **Explorer GUI**
(`taiko2x6/app/taiko256_explorer_gui6.py`, started by `main.py`) is the hub: it
opens a game archive and launches the rest. Modules import each other with
absolute package imports (`from taiko2x6.ps2 import ps2hdd`) and find
user-supplied assets through `taiko2x6/app/apppaths.py`.

```
main.py                 launcher -> taiko2x6.app.taiko256_explorer_gui6.main()
taiko2x6/
  app/        archive/    songs/      bineditors/   charts/
  audio/      graphics/   ps2/        exe/
```

## Module map

### `app/` — application hub
| Module | Role |
|--------|------|
| `taiko256_explorer_gui6.py` | **Main GUI.** Opens `DATA.000` + `list.bin`, browses groups/files, previews textures/audio, and launches every other tool. Auto-discovers `bineditor_*.py` by globbing the `taiko2x6/bineditors/` package. |
| `appconfig.py` | `config.ini` last-used-path store used by every file dialog. |
| `apppaths.py` | Where user-supplied assets and `config.ini` live: the project root from source, or the `.exe`'s folder when frozen. |

### `archive/` — container
| Module | Role |
|--------|------|
| `archive_model.py` | `Archive`: the Qt-free LIST.BIN + DATA.000 model (mmap reads, staged edits, in-place `save`, layout-preserving `rebuild`). Used by the Explorer, HDD Song Wizard, Omnimix Maker and self-tests. |
| `taiko256_archive_tool_v2.py` | Backend/CLI for the SYSTEM256/2x6 archive container (group table + file table + payload). |
| `archive_builder.py` | Rebuild `DATA.000` + `list.bin` with new/replaced groups (keeps the name-sorted group order the game binary-searches). Append mode by default; `compact=True` re-lays groups densely so the archive can shrink. |

### `safeio.py` — crash-safe writes (package root)
| Function | Role |
|----------|------|
| `atomic_write` / `make_backup` | Temp file + fsync + `os.replace`; rolling `.bak`, `.bak.1`, … |
| `write_pair` / `recover_pair` | Commit `DATA.000` + `list.bin` as one transaction with a `.txn` journal; `Archive()` recovers an interrupted one on open. |
| `UndoJournal` / `rollback` | Write-ahead undo log for in-place edits. `ps2hdd.BlockDevice` journals every overwritten range by default; `ps2hdd.write_session()` rolls back on error and keeps the journal on success. CLI: `python -m taiko2x6.safeio info` / `rollback <journal>`. |

### `songs/` — song assembly & databases
| Module | Role |
|--------|------|
| `song_builder.py` | High-level "add/replace a full song": title textures + charts + audio + DB rows in one call. |
| `song_manager.py` | Cross-`.bin` database editor — coordinated add/edit/remove across `musicinfo` / `tuning` / `streaminfo`. |
| `song_replacer.py` | Swap one existing song's parts (chart/audio/texture) in place; can source from a Gen3 folder. |
| `omnimix_maker.py` | Fuse songs from several Taiko 8…14 images into one target (dedup by song id, verbatim asset copy, auto song-limit patch, partition grow). |
| `omnimix_gui.py` | GUI for the Omnimix Maker (Tools menu). |
| `generators.py` | Placeholder chart/audio generators (uses `tja2sht`, `vagtool`). |

### `bineditors/` — per-song database `.bin` editors (auto-discovered)
| Module | Table |
|--------|-------|
| `bineditor_musicinfo.py` | `musicinfo.bin` — song metadata, genre, star ratings, display indices |
| `bineditor_tuning.py` | `tuning.bin` — per-difficulty tuning / star grid |
| `bineditor_streaminfo.py` | `streaminfo.bin` — audio stream / volume info |
| `bineditor_rank.py` | `rank.bin` — ranking thresholds |
| `bineditor_lamp.py` | `lamp.bin` — clear-lamp / crown state |
| `bineditor_fname.py` | `fname.bin` — file-name table |
| `bineditor_hdbdinfo.py` | `hdbdinfo.bin` — hd/bd sound-bank index |
| `bineditor_enso_parts.py` | `enso_parts.bin` — performance/"enso" part asset table |

Each exposes `FILENAME` and `Editor`; dropping a new `bineditor_*.py` into this
folder registers it with the Explorer.

### `charts/` — fumen
| Module | Role |
|--------|------|
| `tja2sht.py` | Convert TJA → SHT (SYSTEM256 chart). Position grid is 1/48 of a measure (`POS_DIV=48`, an engine limit). |
| `sht_validator.py` | Validate / sanity-check SHT charts. |
| `gen3_convert.py` | Decrypt Gen3 (Nijiiro) fumen (AES-256-CBC + gzip) and convert to SHT; decodes audio via `vgmstream`. |
| `gen3_song.py` | Load a Gen3 song directory and map it to `song_builder` kwargs (title, charts, stars, audio). |

### `audio/`
| Module | Role |
|--------|------|
| `vagtool.py` | VAG (PS2 ADPCM) encode/decode; OGG/WAV ↔ VAG (bit-exact with vgmstream's IIR). |
| `hdbd.py` | hd/bd sound-bank (hddbd) parse + playback. |
| `audioplayer.py` | In-GUI audio playback widget. |

### `graphics/`
| Module | Role |
|--------|------|
| `tim2.py` | TIM2 image encode/decode (incl. 4-bit indexed templates used for titles). |
| `songtex.py` | Basic song-title texture rendering. |
| `songtex_all.py` | Full song-title texture set generator using the 勘亭流 brush font (`Font.ttf`). |
| `swg.py` | SWG sprite/animation container parser (round-trip verified). |
| `swg_editor.py` | SWG visual editor (canvas + save-back). |
| `flipbook_player.py` | Flipbook frame-animation player. |

### `ps2/` — PS2 storage I/O
| Module | Role |
|--------|------|
| `ps2hdd.py` | **Native** PS2 APA-partition + PFS filesystem read/write. The core HDD engine, incl. `grow_partition` / `grow_partition_reclaim`. Writable opens keep an undo journal; use `write_session()` for all-or-nothing edits. |
| `pfsshell_tool.py` | Drive the native `pfsshell.exe` as an alternative PFS path. |
| `apa_merge.py` | Merge multiple games' APA partitions onto one HDD image (swap-card-per-game setup). |
| `img_slim.py` | Trim / slim a PS2 HDD image. |
| `iso_packer.py` | Build/pack the SYSTEM256 disc ISO. |
| `ps2mc_card.py` | Native PS2 memory-card (arcade COH) read/write, 528-byte-page ECC format. |
| `hdd_browser.py` | GUI HDD/partition browser. |
| `hdd_song_wizard.py` | One-click "add a song to a T14 image" flow. |

### `exe/` — executable patching
| Module | Role |
|--------|------|
| `taiko_exe.py` | Decrypt/patch the T14+ `taiko` ELF (MIPS, EE). Needs `T14LOAD.bin` to derive the key. Song-count ceiling, select timer, boot Notice skip. |

## Dependency shape

- **`app/taiko256_explorer_gui6`** imports most of the package — it is the top
  of the graph and the single place most tools are wired together.
- **`ps2/ps2hdd`** is the heaviest standalone engine (APA + PFS); `apa_merge`,
  `img_slim`, `taiko_exe`, `omnimix_maker` and the HDD GUIs build on it.
- **`songs/song_manager`** ties the three primary `.bin` editors together;
  `song_builder` sits above it for full-song assembly, and `omnimix_maker`
  above that for multi-game merges.

## Running

- GUI: `python main.py [folder]`
- A single tool: `python -m taiko2x6.<subpackage>.<module>` from the project root.
- Frozen build: `pyinstaller Taiko2x6Wizard.spec` (entry `main.py`;
  `collect_submodules('taiko2x6')` lists every module because several are
  imported lazily).
- Tests: `python -m pytest` (see README → Tests).

## User-supplied assets

Resolved by `apppaths.resource_dir()` — the project root from source, the
`.exe`'s folder when frozen (all git-ignored — you supply them):

- `Font.ttf` — `graphics/songtex`, `graphics/songtex_all`
- `vgmstream-win64/` — `charts/gen3_convert`
- `pfsshell.exe` — `ps2/pfsshell_tool`
- `T14LOAD.bin` — `exe/taiko_exe`
- `config.ini` — `app/appconfig`

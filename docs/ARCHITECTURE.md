# Architecture

Taiko2x6Wizard is a flat collection of PySide6/CLI modules that all operate on
the arcade _Taiko no Tatsujin_ data formats. The **Explorer GUI**
(`taiko256_explorer_gui6.py`) is the hub: it opens a game archive and launches
the rest. Modules use flat imports (`import ps2hdd`) and load bundled assets
relative to their own directory, so they must stay in one folder.

## Module map

### Application / hub
| Module | Role |
|--------|------|
| `taiko256_explorer_gui6.py` | **Main GUI.** Opens `DATA.000` + `list.bin`, browses groups/files, previews textures/audio, and launches every other tool. Auto-discovers `bineditor_*.py` by globbing its own directory. |
| `taiko256_archive_tool_v2.py` | Backend/CLI for the SYSTEM256/2x6 archive container (group table + file table + payload). |

### Archive build & song assembly
| Module | Role |
|--------|------|
| `archive_builder.py` | Rebuild `DATA.000` + `list.bin` with new/replaced groups (keeps the name-sorted group order the game binary-searches). |
| `song_builder.py` | High-level "add/replace a full song": title textures + charts + audio + DB rows in one call. |
| `song_manager.py` | Cross-`.bin` database editor — coordinated add/edit/remove across `musicinfo` / `tuning` / `streaminfo`. |
| `song_replacer.py` | Swap one existing song's parts (chart/audio/texture) in place. |

### Per-song database `.bin` editors (auto-discovered by the Explorer)
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

### Charts (fumen)
| Module | Role |
|--------|------|
| `tja2sht.py` | Convert TJA → SHT (SYSTEM256 chart). Position grid is 1/48 of a measure (`POS_DIV=48`, an engine limit). |
| `sht_validator.py` | Validate / sanity-check SHT charts. |
| `gen3_convert.py` | Decrypt Gen3 (Nijiiro) fumen (AES-256-CBC + gzip) and convert to SHT; decodes audio via `vgmstream`. |
| `gen3_song.py` | Load a Gen3 song directory and map it to `song_builder` kwargs (title, charts, stars, audio). |

### Textures / graphics
| Module | Role |
|--------|------|
| `tim2.py` | TIM2 image encode/decode (incl. 4-bit indexed templates used for titles). |
| `songtex.py` | Basic song-title texture rendering. |
| `songtex_all.py` | Full song-title texture set generator using the 勘亭流 brush font (`Font.ttf`). |
| `swg.py` | SWG sprite/animation container parser (round-trip verified). |
| `swg_editor.py` | SWG visual editor (canvas + save-back). |
| `flipbook_player.py` | Flipbook frame-animation player. |

### Audio
| Module | Role |
|--------|------|
| `vagtool.py` | VAG (PS2 ADPCM) encode/decode; OGG/WAV ↔ VAG (bit-exact with vgmstream's IIR). |
| `audioplayer.py` | In-GUI audio playback widget. |
| `hdbd.py` | hd/bd sound-bank (hddbd) parse + playback. |
| `generators.py` | Placeholder chart/audio generators (uses `tja2sht`, `vagtool`). |

### PS2 storage I/O
| Module | Role |
|--------|------|
| `ps2hdd.py` | **Native** PS2 APA-partition + PFS filesystem read/write. The core HDD engine. |
| `pfsshell_tool.py` | Drive the native `pfsshell.exe` as an alternative PFS path. |
| `apa_merge.py` | Merge multiple games' APA partitions onto one HDD image (swap-card-per-game setup). |
| `img_slim.py` | Trim / slim a PS2 HDD image. |
| `iso_packer.py` | Build/pack the SYSTEM256 disc ISO. |
| `ps2mc_card.py` | Native PS2 memory-card (arcade COH) read/write, 528-byte-page ECC format. |
| `hdd_browser.py` | GUI HDD/partition browser. |
| `hdd_song_wizard.py` | One-click "add a song to a T14 image" flow. |

### Executable patching
| Module | Role |
|--------|------|
| `taiko_exe.py` | Decrypt/patch the T14+ `taiko` ELF (MIPS, EE). Needs `T14LOAD.bin` to derive the key. Includes the song-count ceiling patch. |

### Shared utilities
| Module | Role |
|--------|------|
| `appconfig.py` | `config.ini` last-used-path store used by every file dialog. |

## Dependency shape

- **`taiko256_explorer_gui6.py`** imports ~22 of the modules — it is the top of
  the graph and the single place most tools are wired together.
- **`ps2hdd.py`** is the heaviest standalone engine (APA + PFS); `apa_merge`,
  `img_slim`, `taiko_exe`, and the HDD GUIs build on it.
- **`song_manager.py`** ties the three primary `.bin` editors together;
  `song_builder.py` sits above it for full-song assembly.
- The eight `bineditor_*.py` tools are independent standalone GUIs **and** are
  discovered dynamically by the Explorer (directory glob) — hence they live
  beside it.

## Assets loaded relative to the module directory

Keep these next to the `.py` files (all git-ignored — you supply them):

- `Font.ttf` — `songtex.py`, `songtex_all.py`
- `vgmstream-win64/` — `gen3_convert.py`
- `pfsshell.exe` — `pfsshell_tool.py`
- `T14LOAD.bin` — `taiko_exe.py`
- `config.ini` — `appconfig.py`

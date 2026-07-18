# File formats (reverse-engineered)

Notes on the on-disc formats this toolkit reads and writes. These were derived
by reverse-engineering for interoperability; no game code or data is reproduced
here. Details are accurate to the tooling in this repo but may vary across
game revisions — always round-trip-verify against your own files.

## Archive container — `DATA.000` + `list.bin`

The SYSTEM256 / SYSTEM2x6 games store assets in a big blob (`DATA.000`) indexed
by a table (`list.bin`). Content is organised into **groups**, each holding one
or more named files (textures, charts, audio, database `.bin`s).

- The game **binary-searches** the group table *and* the files within a group
  **by name**, so both must stay byte-wise name-sorted. Appending new entries
  without re-sorting breaks lookup (hangs/garbage). `archive_builder.py`
  preserves the sort on rebuild.
- Some groups are pinned (e.g. `gamedata`, `soundinfo`).
- On Taiko 8 (older LIST.BIN), each group carries an `unknown2` value =
  `LE_u32( SHA1(b"nULIb" + payload)[:4] )`. The game validates it; a wrong value
  freezes the song. See [`unknown2_report.md`](unknown2_report.md). Handled by
  `compute_unknown2()`.

## Per-song database `.bin` tables

Section-structured binaries (header + fixed-size record sections + string pool).
Editors: `bineditor_*.py`; cross-table coordination: `song_manager.py`.

- **`musicinfo.bin`** — one record per song: id, genre, star ratings, and
  **self-referential display indices**. On T14 the SEC0 record's columns 13/14
  (self-index), 15/16 (display-permutation indices the song wheel iterates), and
  col 10 (ura pairing) must equal the *new* row's own index — not a cloned
  template's — or the song is added but silently invisible.
- **`tuning.bin`** — per-difficulty tuning / star grid, stored in paired blocks.
- **`streaminfo.bin`** — audio stream descriptors (includes volume fields; note
  the length-looking fields are volume, not duration).
- **`rank.bin`, `lamp.bin`, `fname.bin`, `hdbdinfo.bin`, `enso_parts.bin`** —
  ranking thresholds, clear-lamp state, file names, sound-bank index, and the
  performance/"enso" part asset table respectively. `enso_parts.bin` is a
  multi-section asset table, **not** indexed 1:1 by song.

## Charts — SHT (and TJA / Gen3 sources)

- **SHT** (SYSTEM256 chart): per-track records of
  `float time, float bpm, int trackLine, int gogo, [int unk], int bunki[6],
  float scrollSpeeds[6]` plus sub-tracks. There is **no** time-signature field;
  measure span is derived from the time gap between consecutive track times.
- **Note position grid is 1/48 of a measure** (`POS_DIV = 48`). This is an
  engine limit — finer grids were tried and broke playback. Triplets inside
  short (e.g. 3-beat) measures are the main source of small (<16 ms) rounding.
- **TJA → SHT** (`tja2sht.py`): standard TJA parse with collision-nudging.
- **Gen3 (Nijiiro) fumen** (`gen3_convert.py`): **AES-256-CBC** (IV = first 16
  bytes) then **gzip**. Layout: 40-byte measure header + 3 branch streams +
  24-byte notes (type 6 is 32 bytes). **Note positions are absolute
  milliseconds — not quantized**; conversion maps them onto the 1/48 grid.
  See [`notecolor_report.md`](notecolor_report.md) for note-color encoding.

## Audio — VAG / hd·bd

- **VAG** — PS2 ADPCM. `vagtool.py` encodes/decodes and matches vgmstream's IIR
  bit-for-bit (samples are clamped to int16 before feedback; `shift > 12` is
  forced to 9, as in hardware).
- OGG/WAV import uses `soundfile` (fallback `librosa`).
- **hd/bd** sound banks: `hdbd.py` parses and plays back the `sound.hdbd.*`
  groups.

## Textures — TIM2

- `tim2.py` encodes/decodes TIM2 images. Song-title textures require a template
  whose picture is **image_type 4 (4-bit indexed)**; check with
  `tim2.first_picture_layout(t)['image_type']` before encoding into it. Namco
  templates (e.g. `genpe`) are all type-4; some others are type-5 and will fail.
- Title art is rendered with the 勘亭流 brush font (`Font.ttf`) by `songtex*`.

## PS2 storage

- **APA + PFS** (`ps2hdd.py`): native reader/writer for the PlayStation 2 HDD
  Apa partition scheme and the PFS filesystem inside game partitions.
  `pfs_write` reallocates, so files may grow/shrink. Partition names are built
  by the on-card loader as `hdd0:%s%04d.%04d,%s` and opened by name, so names
  must not collide — which is what makes the "one HDD, swap memory-card per
  game" multi-game merge (`apa_merge.py`) work.
- **Memory card** (`ps2mc_card.py`): native arcade/COH card image I/O. 528-byte
  pages (512 data + 16 spare) with ECC; spare = `0xFF` means an erased page, not
  a bad one. `write_file` reallocates so files can grow.

## T14+ `taiko` executable

- The game partition holds `taiko`, an **encrypted ELF32 (MIPS III / EE,
  64-bit)**. Decryption (`taiko_exe.py`) is a modified Blowfish + CBC scheme
  whose key is derived from `T14LOAD.bin`; decrypted virtual address =
  `file_offset + 0xFF000`. Disassemble with MIPS64 or 64-bit instructions
  (`sd`/`ld`/`daddu`) decode as `.word`.
- **Song-count ceiling**: the per-credit "already-picked" map is indexed by
  music id and is immediately followed by other fields, capping the usable song
  count. `taiko_exe.py` relocates that array past the object so the ceiling can
  be raised well beyond the stock limit; the select-screen filter reads that map
  as the single display gate.

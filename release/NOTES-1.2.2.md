## Taiko2x6Wizard v1.2.2

Single-file builds for **Windows** and, new in this release, **Linux**
(x86-64, glibc 2.35+) — no Python install needed. See `README_FIRST` in the
archive for the files you place next to the program (`Font.ttf`, `T14LOAD.bin`).

| File | Platform |
|------|----------|
| `Taiko2x6Wizard-v1.2.2-win64.zip` | 64-bit Windows: unzip and run `Taiko2x6Wizard.exe` |
| `Taiko2x6Wizard.exe` | the same Windows program on its own |
| `Taiko2x6Wizard-v1.2.2-linux-x86_64.tar.gz` | 64-bit Linux: extract and run `./Taiko2x6Wizard` |

### New — Omnimix Maker (Tools → Omnimix)
Harvest every song from several Taiko images and fuse the ones a target lacks
into one image — charts, name textures, audio and difficulty stars, copied
verbatim. Sources can be an HDD `.img`, a game ISO, or a loose
`DATA.000` + `list.bin` folder, from **Taiko 7 to 14**.

- **Taiko 7 sources**: T7's older chart layout is converted to the Gen2 one
  (notes unchanged) and T7 star ratings are read, so T7 songs play in T14.
- **600+ songs on Taiko 14**: the song limit, the per-song save records
  (scores, crowns, ranking), the texture manager and the texture heap all grow
  with the song count, fixing crashes and select-screen stalls past ~210 / ~500
  songs. Very large builds can re-encode audio at a lower rate to stay under
  the 2 GB `DATA.000` limit, and the PFS partition grows when needed.
- **Song select order**: genres, stars and folder order come from trusted
  releases (stock T14, then T13 down to T7); songs from earlier mods are not
  used as evidence. Chinese songs get a C-POP folder. Folder headers are shown
  as divider cards.
- **Window**: patch checkboxes, a progress bar and a per-folder summary.
- ⚠ Big merges are experimental — always work on a copy of the image.

### New — Taiko 14 boot & select patches (Tools → Boot & select patches)
One dialog with a checkbox per patch, read from the image so you see what is
already applied; each can be turned on **and off again** byte-exactly:
- skip the boot Notice screen
- skip the attract credits (協力) screen
- freeze the song-select timer
- disable the difficulty-select timer

### New — Taiko 14 USB keyboard patch
Ports the Taiko14 USB keyboard patch (MIT): install to the HDD partition that
holds `taiko`, or to a memory-card image. HDD mode is not yet hardware-tested.

### New — Song Replacer: Gen3 (Nijiiro) source
Replace a slot from a Nijiiro `…/fumen/<id>` folder — charts, stars and music
taken from the game's own data.

### New — Interface
- Traditional Chinese (繁體中文) interface, switchable with English.
- The Tools menu is grouped into submenus; a start page replaces the empty
  main window.

### Fixed
- **Chart positions in odd-time measures** (Gen3 → sht and TJA → sht). Gen2
  note positions are 1/48 of a 4/4 bar, not of the measure, so a 3/4 measure
  uses positions 0..35. Both converters stretched odd-time measures across
  the whole bar, placing their notes late. Against the 390 charts shipped by
  both T14 and Nijiiro, position matches go from **94.43% to 99.96%**. TJA
  measures longer than 4/4 are now split into one-bar sections.
  (This replaces the v1.2.1 "gap within a constant tempo" rule, which was
  still wrong for 3/4, 2/4 and 7/8 measures.)
- Skipping the attract credits screen no longer crashes the game.
- Safer writes (temporary file + rename) and many smaller fixes from a code
  review.

Full diff: https://github.com/GanYiZhong/Taiko2x6Wizard/compare/v1.2.1...v1.2.2

## Taiko2x6Wizard v1.2.2

Single-file Windows build — no Python install needed. See the
[v1.0.0 notes](https://github.com/GanYiZhong/Taiko2x6Wizard/releases/tag/v1.0.0)
for the optional files you place next to the `.exe`.

### New — Omnimix Maker (Tools → “Omnimix Maker (fuse many games)…”)
Harvest every song from several Taiko images and fuse the ones a target lacks
into one image — charts, name textures, audio and difficulty stars, copied
verbatim (no re-encode, no lost art). Dedup is by song id; the target is the
base, so its songs are kept and only what it's missing is added.

- **Sources**: HDD `.img` (pick a partition), game **ISO** (`DATA.000`+`LIST.BIN`
  read straight out of the ISO9660 root), or a loose `DATA.000`+`list.bin` folder.
  Taiko 8…14 share the container and the sht/TIM2/VAG formats, so songs port
  across versions; only the song-DB bins differ and those are normalised.
- **Auto song-limit patch**: after merging, the `taiko` executable's song
  ceiling is lifted to the new count (arrayB ≤214 proven; arrayA relocation
  above that). ⚠ Past 214 is experimental — the data holds, but whether the
  select wheel renders/plays them all in-game is untested; test on a copy.
- **Auto partition-grow** (`ps2hdd.grow_partition`): if the merged `DATA.000`
  outgrows the target's PFS partition, an APA sub-partition is appended (the
  image file is enlarged) and the write retried — so very large merges fit.
  Verified end-to-end: an 8-game merge (255 songs added → 568 total, 3.35 GB
  `DATA.000`) grew a 2 GB sub-partition and wrote back with the DB fully
  consistent and charts intact.

### New — Song Replacer: Gen3 (Nijiiro) source mode
Song Replacer can now replace a slot from a Nijiiro `…/fumen/<id>` folder, using
the same branch-preserving, bpm-aware converter as the Custom Song Builder —
charts, stars and music decoded from the game's own data, no sync applied.

Full diff: https://github.com/GanYiZhong/Taiko2x6Wizard/compare/v1.2.1...v1.2.2

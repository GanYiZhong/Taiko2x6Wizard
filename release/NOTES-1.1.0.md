## Taiko2x6Wizard v1.1.0

Single-file Windows build — no Python install needed. See the
[v1.0.0 notes](https://github.com/GanYiZhong/Taiko2x6Wizard/releases/tag/v1.0.0)
for the optional files you place next to the `.exe`.

### New
- **Gen3 (Nijiiro) charts with diverge paths now keep their branches.**
  Previously only the 普通 (normal) route survived conversion; charts that
  branch now convert to Gen2's official three-route layout (普通/玄人/達人
  with per-route scoring countdowns and the diverge thresholds carried over
  verbatim — the thresholds turn out to be value-identical between the two
  generations).
  - Validated against the 14 retail branching songs present in both game
    generations: 9 convert to a structural value-for-value match with the
    retail Gen2 chart; the remainder differ only where the two generations'
    charts themselves were revised.
  - Non-branching charts convert byte-identically to before.
  - API: `gen3_song.load_song(branch=None)` keeps branches by default; pass
    `0/1/2` to flatten to a single route like before.

Credits: branch/header field semantics cross-checked against
[DonDonLibrary](https://github.com/mrcloverthecoder/DonDonLibrary) by
mrcloverthecoder.

Full diff: https://github.com/GanYiZhong/Taiko2x6Wizard/compare/v1.0.3...v1.1.0

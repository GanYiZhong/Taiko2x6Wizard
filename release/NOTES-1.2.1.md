## Taiko2x6Wizard v1.2.1

Single-file Windows build — no Python install needed. See the
[v1.0.0 notes](https://github.com/GanYiZhong/Taiko2x6Wizard/releases/tag/v1.0.0)
for the optional files you place next to the `.exe`.

### Fixed
- **Gen3 → Gen2 chart conversion: correct note timing at tempo changes.**
  The converter derived each measure's length from the raw Gen3 offset gap,
  which is anomalous (negative / tiny / inflated) at BPM changes — so notes in
  those measures landed on the wrong beats. It now uses the 4/4 bar
  (`240000/bpm`) across a tempo change and the gap within a constant-tempo run
  (which keeps genuine odd-time measures, like 6/4 songs, correct).
  - Verified against songs that ship in both game generations: **"Hatara" now
    converts note-for-note identical to the retail Gen2 chart on every
    difficulty** (previously ~90% of notes on oni were mis-placed). Odd-time
    songs (Vertex, Vixtor) stay 100%; validated across all 398 charts present
    in both generations with zero regressions.

Full diff: https://github.com/GanYiZhong/Taiko2x6Wizard/compare/v1.2.0...v1.2.1

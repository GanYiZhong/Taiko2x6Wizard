## Taiko2x6Wizard v1.0.2

Single-file Windows build — no Python install needed. See the
[v1.0.0 notes](https://github.com/GanYiZhong/Taiko2x6Wizard/releases/tag/v1.0.0)
for the optional files you place next to the `.exe`.

### Fixed
- **Vertical select-plate titles no longer balloon.** Short titles on the
  tate-gaki song-select plates were rendered up to the full column width,
  making them look far larger than retail plates in-game. The renderer now
  measures the template's own character size (ink width minus outline stroke)
  and draws new titles at that size — long titles still shrink to fit, short
  titles keep the retail proportions.

Full diff: https://github.com/GanYiZhong/Taiko2x6Wizard/compare/v1.0.1...v1.0.2

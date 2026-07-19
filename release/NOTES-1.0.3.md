## Taiko2x6Wizard v1.0.3

Single-file Windows build — no Python install needed. See the
[v1.0.0 notes](https://github.com/GanYiZhong/Taiko2x6Wizard/releases/tag/v1.0.0)
for the optional files you place next to the `.exe`.

### Fixed
- **Select-plate title size now matches retail exactly.** v1.0.1 drew short
  titles far too large; v1.0.2 over-corrected and drew them too small. The
  renderer now uses the retail cell model — every character gets a
  fixed-height cell (calibrated against a retail plate with a known
  character count), the glyph fills its cell, and only long titles shrink.
  A rendered "KAGEKIYO" measures within 3% of the retail KAGEKIYO plate.

Full diff: https://github.com/GanYiZhong/Taiko2x6Wizard/compare/v1.0.2...v1.0.3

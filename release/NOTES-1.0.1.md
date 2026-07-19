## Taiko2x6Wizard v1.0.1

Single-file Windows build — no Python install needed. See the
[v1.0.0 notes](https://github.com/GanYiZhong/Taiko2x6Wizard/releases/tag/v1.0.0)
for the four optional files you place next to the `.exe`
(`Font.ttf`, `vgmstream-win64/`, `pfsshell.exe`, `T14LOAD.bin`).

### Changed
- **Song-title texture generation now matches the actual template.** The
  generator decodes the template texture taken from your `DATA.000` and
  measures how that plate is drawn, instead of using a fixed style table:
  - black outline is added **only if the template has one**, with the
    template's measured stroke width;
  - fill colour follows the template (gold `result` plates stay gold);
  - flat plates keep the template's ink colour (coloured per-difficulty
    `select_non` plates keep their colour instead of being forced white);
  - text height ratio, side margin and alignment follow the template.
  Verified against 21 real templates from Taiko 8 and Taiko 14+ archives.

Full diff: https://github.com/GanYiZhong/Taiko2x6Wizard/compare/v1.0.0...v1.0.1

## Taiko2x6Wizard v1.2.0

Single-file Windows build — no Python install needed. See the
[v1.0.0 notes](https://github.com/GanYiZhong/Taiko2x6Wizard/releases/tag/v1.0.0)
for the optional files you place next to the `.exe`.

### New
- **Customizable song-select timer (T14+).** The song-select screen's
  inactivity countdown (stock **120 s**, after which the game returns to the
  attract/demo loop) can now be set to anything from **1 to 999 seconds**.
  - New Tools entry: **Song-select timer (T14+)…** — pick your HDD image,
    choose the seconds (quick buttons for 120 / 300 / 999), Apply. Includes
    Check / Back-up / Restore, and the write is round-trip-verified before the
    image is touched.
  - CLI: `python taiko_exe.py <img> --select-timer 999`.
  - Reverse-engineered from the encrypted EE executable: the timer lives in
    three `.sbss` globals all initialised to 120 and counting down to the same
    attract-mode call, so the patch sets all three to keep the whole
    select flow on one limit.

> As always: close PCSX2 first (it locks the image), and back up `taiko`
> before your first patch.

Full diff: https://github.com/GanYiZhong/Taiko2x6Wizard/compare/v1.1.0...v1.2.0

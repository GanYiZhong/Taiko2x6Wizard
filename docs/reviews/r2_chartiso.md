SCORE: 91

# Round 2 review — tja2sht.py, iso_packer.py, flipbook_player.py

Re-scored after the round-1 fixes. All prior correctness findings appear addressed:
truncation/overrun bounds checks in `parse_sht`, both-endian ISO record + PVD patching,
adopt-actual-DATA.000-LBA logic, 0x0-pixmap divide-by-zero guard, non-chart-line filtering
in the TJA parser, and roll/balloon start→8 back-patching. Empirically verified below.

## Verification performed (all PASS)

- **sht round-trip**: byte-exact for synthetic NEW (136B) and OLD (140B) track variants in the
  realistic no-gap layout (`note_offset == track_end`). `serialize_sht(parse_sht(d)) == d`.
- **TJA→sht convert + re-parse**: sample TJA produces monotonic-time tracks, correct note
  duplication (sub-track 0 + sub-track 3), and re-parses/round-trips cleanly.
- **ISO packing**: both-endian extent LBA (off+2 LE / off+6 BE) and data length (off+10 LE /
  off+14 BE) patched correctly; PVD volume-space-size both-endian (pvd+80/+84); sector math
  (`total_sectors`, `list_lba`) correct; payload physically lands on the declared LBA; output
  file size matches. Directory-record offset math matches the ISO 9660 fixed layout.
- **flipbook `detect_clips`**: empty/single/same/diff/run cases all correct; clips give complete
  contiguous coverage with no gaps or overlaps.

## Findings

### Medium — dense-measure note stacking contradicts the code's own claim (fidelity)
`convert_tja` maps in-measure position to `int(round(i*48/n))` then does
`while pos in used_pos and pos < POS_DIV-1: pos += 1`. The comment asserts notes "never silently
stack." They do: for any measure with **more than 48 notes**, the collision loop saturates at 47
and every excess note is written at position 47. Verified with a 96-note measure — 48 notes all
land at `measure==47`. `POS_DIV=48` covers up to 48th-note / 16th-triplet resolution (fine for the
vast majority of charts), but 64th/96th streams that occur in some Oni/Ura charts get mis-timed
(note count is preserved, no crash). This is the single most impactful correctness gap and the
reason the score sits below a clean 95. Recommend either bumping `POS_DIV` (e.g. to 96 or 192) or
at minimum correcting the comment so the limitation is honestly documented.

### Low — OLD/NEW variant detection is fragile against a non-empty `_gap`
`_detect_is_old` decides the track record size solely from `(noteOffset-16)/trackCount`. If a file
has any bytes between the track table and `noteOffset` (`_gap`), the region no longer divides as
136/140 and detection raises `ValueError` (confirmed: gap=4, tc=2 → region 276 fails). The parser
otherwise goes to lengths to preserve `_gap`/`_tail` for exactness, so this is an internal
inconsistency. Harmless for the real corpus (all 720 charts are gapless NEW-variant), but it means
"preserve unusual files so they round-trip" isn't fully true — a gapped file can't even be parsed.
Acceptable given the corpus, worth a comment noting the assumption.

### Low — `serialize_sht` bunkis/scrolls/subtracks padding can silently change bytes
When a model has fewer than 6 `bunkis`/`scrollSpeeds`/`subtracks`, serialize pads with
`-1`/`1.0`/zeros. This only matters for hand-built models (parse always yields exactly 6), so it
can't break round-trip of parsed files, but a caller mutating a model could get a silently
different byte layout than intended. Minor robustness note, not a bug on the round-trip path.

### Low — TJA `#MEASURE` with non-numeric or zero denominator
`state["measure_den"] = int(_to_float(b, 4))` — a `#MEASURE 4/0` yields `den=0`, later guarded by
`den = m["measure_den"] or 4`, so it recovers to 4. Fine. `measure_num` is `max(0, num)`; a `0`
numerator produces a zero-length measure (measure_ms 0) which is tolerated. No crash; behavior is
reasonable.

### Nitpick — ISO packer assumes exactly two files, root single-extent in header
`pack_iso` requires DATA.000 and LIST.BIN in a single-extent root fully inside the first 268
sectors, and rejects DATA.000 starting past the header buffer with a clear error. This is correct
and defensive for the known disc; it is not a general ISO 9660 writer (multi-extent roots,
relocated DATA.000 beyond the header, added/removed files are all rejected loudly rather than
mishandled). Documented behavior, appropriate scope.

### Positive notes
- Bounds checks in `parse_sht` (`region<0`, `track_end>note_offset`, `note_count<0`,
  `note_end>len(data)`) turn every malformed layout into a clear `ValueError` instead of a
  struct.error or silent garbage read.
- ISO packer's "adopt the actual DATA.000 LBA from the record" step is the right robustness fix and
  is correctly paired with the `data_lba > DATA_LBA` guard so payload can never overwrite the header.
- Roll/balloon start→8 pairing back-patches the pass-1 `rec` before pass-2 copies it, so both
  duplicated copies carry the same `longNoteLength`; `bal_iter` is consumed once (pass 1), so
  duplication doesn't double-consume balloon counts. Correct.
- flipbook divide-by-zero guards (0x0 pixmap in `paintEvent`, FPS>=1 in `_apply_fps`) and the
  no-frames disabled-controls path are all handled.

## Why 91 (not 90+ clean)
No crash-level or data-corruption correctness bugs remain on the real corpus / realistic inputs;
robustness and error handling are solid (clear ValueErrors, defensive ISO logic, guarded UI edge
cases). Held just above the 90 bar by the honest presence of the dense-measure stacking limitation
(a genuine fidelity loss on >48-note measures that the comment wrongly claims is prevented) and the
minor gap-detection fragility. Fixing `POS_DIV` / the misleading comment would justify 95+.

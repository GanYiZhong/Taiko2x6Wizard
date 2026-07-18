# Code Review: TJA→sht converter / ISO packer / Flipbook player

**Files reviewed (read-only):**
- `E:\Taiko No Tatsujin 8\tja2sht.py`
- `E:\Taiko No Tatsujin 8\iso_packer.py`
- `E:\Taiko No Tatsujin 8\flipbook_player.py`

**SCORE: 58 / 100**

The `parse_sht`/`serialize_sht` round-trip path and the ISO packer's both-endian + PVD
patching are correct for the documented layout. The entire score gap is the TJA→sht
converter, which mishandles branches, roll/balloon durations, note positions, and the
file format's own documented note-duplication invariant.

---

## CRITICAL

### CRITICAL — tja2sht.py:328-343 (and convert loop 450-511) — TJA branches produce corrupted, overlong charts
**Problem:** `convert_tja` walks `cdata["measures"]` linearly and advances `cur_time_ms`
for every measure. But TJA branch notation (`#BRANCHSTART`, `#N`, `#E`, `#M`,
`#BRANCHEND`) emits **three parallel measure streams covering the same time span**. The
parser only sets `state["branch"]` and appends N, E and M measures consecutively, so a
branched section's measures are laid end-to-end. Result: the chart becomes 2-3x too long
and every note after the first branch is mistimed. The docstring claims branches "collapse
to master path (N) for simplicity" but no collapsing happens — E and M measures are still
emitted as real measures, and `bunkis` is always `[-1]*6` so no branch info reaches the sht.
**Exact fix:** Before the time-advancing loop in `convert_tja`, filter
`cdata["measures"]` to keep only the chosen branch path, e.g.:
```python
SELECTED_BRANCH = 2  # master/clear path
measures = [m for m in cdata["measures"] if m["branch"] in (-1, SELECTED_BRANCH)]
```
Or, properly: model branches by setting per-measure `bunkis` and emitting all three paths
into separate sub-tracks instead of advancing time per path. At minimum, drop non-selected
branch measures so time stays correct.

### CRITICAL — tja2sht.py:462-489 — emitted notes are NOT duplicated, contradicting the documented format invariant
**Problem:** The module header (lines 62-64) states every logical note is written **twice
consecutively** in real Gen2 charts, that the parser preserves this for all 720 corpus
charts, and that "the TJA converter reproduces it." It does not — each note is appended
once. If the engine relies on the documented duplication, converter output is structurally
unlike every real chart and may render/score incorrectly.
**Exact fix:** Either emit each note twice and adjust the counters, e.g. inside each note
branch replace the single `notes.append(rec); emitted += 1` with:
```python
notes.append(dict(rec)); notes.append(dict(rec)); emitted += 2
```
…and ensure sub-track index math accounts for the doubled count — OR remove the false
"reproduces it" claim from the docstring and validate in-game that single-emission charts
load correctly. This must be resolved against the actual game, not assumed.

---

## HIGH

### HIGH — tja2sht.py:465 — position quantization collides notes
**Problem:** `pos = int(round(i * POS_DIV / n)) % POS_DIV`. When `n` does not divide 48
(n=5,7,9,…), distinct consecutive notes round to the same `measure` slot, silently
stacking notes. The trailing `% POS_DIV` is dead (the value can't reach 48 for n≥1). Tuplet
rhythms (5/7-tuplets) cannot be represented exactly in 1/48.
**Exact fix:** Detect collisions and reject/warn, or place notes on the measure's true
subdivision. If the sht position field is wider than 1/48, use full resolution. Minimal
guard:
```python
pos = int(round(i * POS_DIV / n))
if pos >= POS_DIV: pos = POS_DIV - 1
# track used positions per measure; warn if a slot is reused
```

### HIGH — tja2sht.py:475-484 — drumroll / balloon / kusudama never get `longNoteLength`, and rolls aren't paired
**Problem:** Types 5/6/9 and 7 all get `longNoteLength=-1`. Per the format doc, big
drumroll (6), kusudama (9) and balloon-with-count (10/12) require a duration in
`longNoteLength`; rolls/balloons are a start marker plus an `8` end marker whose span
defines length. The converter never computes the start→end duration, so every roll/balloon
has length -1 (no duration). Balloons always emit `NOTE_BALLOON_CNT(10)` for `7` even with
no count (defaults to 5) rather than `NOTE_BALLOON(7)`.
**Exact fix:** Track the open roll/balloon: record its note index and start time when
`5/6/7/9` is seen; when the matching `8` is seen, compute elapsed time (ms) across measures
and back-patch the start note's `longNoteLength`. Choose type 7 (no count) vs 10 (with
count) based on whether a balloon count is available from `bal_iter`.

---

## MEDIUM

### MED — tja2sht.py:101-114 — `_detect_is_old` dead fallback + silent mis-detection
**Problem:** Lines 112-114 are unreachable: if `region % track_count == 0 and rec ==
TRACK_OLD_SIZE`, line 107 already returned True. When `region` divides evenly by neither
136 nor 140 (corrupt/unknown variant), the function silently returns NEW and `parse_sht`
reads garbage offsets without raising. No validation that `note_offset` is within
`len(data)`.
**Exact fix:** Delete lines 112-114. Raise `ValueError("unrecognized track record size")`
when neither variant divides evenly. In `parse_sht`, validate
`HEADER_SIZE + track_count*trec <= note_offset` and
`note_offset + note_count*NOTE_SIZE <= len(data)` before reading.

### MED — tja2sht.py:155-159 — truncated note table raises opaque `struct.error`
**Problem:** `struct.unpack_from` on a truncated note table throws `struct.error` with no
context; no bounds check on `note_offset`/`note_count`.
**Exact fix:** Add the `note_offset + note_count*NOTE_SIZE <= len(data)` check above and
raise a `ValueError` with the offending values.

### MED — tja2sht.py:345-379 — `KEY:value` lines inside a chart silently corrupt the measure buffer
**Problem:** Header parsing only runs when `not in_chart`. A `KEY:value` line appearing
between `#START` and `#END` falls through to the note-data branch, so its `:` and text
accumulate into `state["buffer"]` and corrupt the measure.
**Exact fix:** In the `in_chart` note branch, validate that the segment contains only legal
chart characters (`0-9`, `,`, whitespace); skip/warn otherwise:
```python
if in_chart and state is not None:
    if any(c not in "0123456789, \t" for c in line):
        continue  # or warn
```

### MED — tja2sht.py:336-341 — branch condition args ignored; partial buffer leaks across paths
**Problem:** `#BRANCHSTART p,x,y` condition data is discarded, and there is no measure
flush at branch boundaries, so a partial buffer before a branch keyword leaks across paths.
Also `SCROLL` is listed here but already handled at line 317 — the second occurrence is
dead. `DELAY` is silently ignored though it shifts timing.
**Exact fix:** Flush any pending `state["buffer"]` at each `#N/#E/#M/#BRANCHSTART/
#BRANCHEND` boundary; remove the duplicate `SCROLL` from the line-336 tuple; either honor
`#DELAY` (add ms to `cur_time_ms`) or document that it is intentionally ignored.

### MED — tja2sht.py:451-454 — BPM/measure values not validated → non-monotonic time
**Problem:** `m["measure_den"] or 4` guards den=0, but `#MEASURE 0/4` gives `beats=0`
(zero-length measure, duplicate `time`s). A negative `#BPMCHANGE -5` yields negative
`measure_ms` and non-monotonic time. `m["bpm"] if m["bpm"] else global_bpm` only catches
falsy 0.0.
**Exact fix:**
```python
bpm = m["bpm"] if m["bpm"] and m["bpm"] > 0 else global_bpm
num = max(0, m["measure_num"]); den = m["measure_den"] or 4
beats = 4.0 * (num / float(den))
if beats <= 0: continue  # or warn and skip
```

### MED — iso_packer.py:78 — root directory assumed single-extent within first 268 sectors
**Problem:** `_find_root_records` walks `length` bytes from `root_lba*SEC`, assuming the
root directory is contiguous from that LBA. `header` is only `DATA_LBA*SEC` (268 sectors)
long, so if `root_lba >= 268` the read indexes past the buffer; multi-extent roots aren't
handled. The docstring implies generality the code lacks.
**Exact fix:** After reading `root_lba`, assert `root_lba < DATA_LBA`; or read enough of
the image to cover the full root extent before walking. Document the single-extent
assumption explicitly.

### MED — iso_packer.py:26,64-66,82 — DATA.000 start LBA hard-coded to 268, never verified
**Problem:** `DATA_LBA = 268` is trusted blindly. If the supplied original ISO places
DATA.000 at a different LBA, the packer still writes the file at sector 268 and patches the
record to 268, which may not match the disc the user provided.
**Exact fix:** Read the original DATA.000 directory record's extent LBA via
`struct.unpack_from("<I", header, recs["DATA.000"] + 2)[0]` and assert it equals
`DATA_LBA` (or adopt the read value as the start LBA) before patching.

---

## LOW

### LOW — iso_packer.py:35-46 — directory walk doesn't bound-check `rec_len`/`nlen`
**Problem:** A malformed record with tiny nonzero `rec_len`, or `nlen` running past the
sector, could loop or read across the record boundary. Only `rec_len == 0` is handled.
**Exact fix:** Guard `if rec_len < 33 or pos + 33 + nlen > length: break` (or jump to next
sector) before reading the name.

### LOW — flipbook_player.py:43-44 — divide-by-zero on non-null 0×0 pixmaps
**Problem:** `scale = min(self.width()/pw, self.height()/ph)` divides by `pw`/`ph`. A
non-null but 0×0 QPixmap (failed TIM2 decode) raises `ZeroDivisionError` in `paintEvent`;
`isNull()` is checked but zero dimensions are not. `detect_clips`'s `dim()` returns `(0,0)`
and groups all broken frames together.
**Exact fix:**
```python
if self._pix is None or self._pix.isNull() or pw == 0 or ph == 0:
    return
```
and skip/replace broken frames upstream.

### LOW — flipbook_player.py:69-83,148-159 — empty `frames` list yields a degenerate blank dialog
**Problem:** `clip_range=(0,0)`, `_show_frame` early-returns, slider range becomes `(0,0)`.
No crash, but the window is blank with no message.
**Exact fix:** When `not frames`, set the label to "no frames" and disable playback
controls.

### LOW — flipbook_player.py:179-180 — FPS interval truncates to int
**Problem:** `int(1000/60)=16` causes minor timing drift at high FPS. FPS≥1 so no
divide-by-zero. Cosmetic.
**Exact fix:** Acceptable as-is; could use `round()` instead of `int()`.

---

## Clarity notes
- tja2sht.py:336 — duplicate `SCROLL` entry in the `BRANCHEND/...` tuple is dead (handled
  at line 317); misleading.
- iso_packer.py docstring claims "works even when the files grow" (true for size) but the
  LBA-268 hard-coding and single-extent-root assumption undercut the generality the prose
  implies.

## What must change to reach 90+
1. Fix branch handling (CRITICAL, tja2sht.py:328-343) — collapse to one path before
   advancing time, or fully model branches.
2. Resolve note-duplication vs. documented invariant (CRITICAL, tja2sht.py:462-489) —
   reproduce the double-write or prove single-emission loads in-game.
3. Compute roll/balloon `longNoteLength` and pair start→`8` (HIGH).
4. Fix position-quantization collisions (HIGH).

The sht round-trip parser/serializer is byte-exact (~95, validated by the 720-chart corpus
harness) and the ISO packer is correct for the documented 2-file/LBA-268 layout; the score
is held down entirely by the converter's fidelity gaps.

**SCORE: 58**

SCORE: 91

# r2 review — song_builder.py (custom-song builder)

Re-review after the fixes. Verified the correctness claims against the actual
dependency modules (`tja2sht.py`, `bineditor_tuning.py`, `song_manager.py`,
`archive_builder.py`), not just the docstrings. No correctness bugs found; the
threading split is safe; robustness is reasonable. Score reflects a solid pass
with a few minor, non-blocking robustness gaps.

## Correctness — verified against dependencies (all pass)

- **TJA course→course mapping.** `parse_tja` keys `courses` by the integer
  course index from `_COURSE_ALIASES` (0=easy…3=oni, 4=ura). `_course_levels`
  (idxmap {0:easy,1:normal,2:hard,3:oni,4:oni}) with the `sorted(...)` +
  `oni_from_3` guard correctly resolves the Oni/Ura collision to the single
  "oni" slot, preferring cidx 3 and only letting cidx 4 fill it when Oni is
  absent — deterministic, not iteration-order dependent. Correct.
- **course name vs index for convert_tja.** `convert_tja(tja_text, course,
  player)` expects a course *name* ("easy"/"normal"/"hard"/"oni"); `build_song`
  and `add_new_song` both pass `_DIFF_LETTER[letter]` (a name), matching. Correct.
- **Star writes.** `STAR_INDEX = 5` in song_manager matches the hardcoded
  `values[5]` here, and both 1P and 2P blocks (`2*k`, `2*k+1`) are written —
  mirroring `_serialize_tuning`'s value-only path exactly. Level clamp to
  [1,10] + warning is correct. Correct.
- **find_textures / find_charts.** Group-name discriminators match the archive's
  conventions: `music_texture.{t}_{sid}` with a lone `nut`; `music_select` →
  `select_full_{sid}`; numeric-suffixed `music_select_<NN>` → `select_non_{sid}`
  (the `elif` + non-collision guard prevents select_full/select_non crosstalk).
  `find_charts` stem `{sid}{player}_{letter}` with entry name `sht`. Correct.
- **precomputed_db threading split.** This is the load-bearing fix and it holds
  up. `compute_db_add` constructs the `SongManager` QWidget and is guarded by
  `_assert_gui_thread`; `prepare_new_song_db` runs it GUI-side and passes
  `precomputed_db` + `stars` into the worker via `add_new_song(precomputed_db=…)`,
  so no QWidget is built off-thread. Verified `_db_add_song` mutates the
  SongManager consistently with the official `add_song`: appends to `_new_songs`
  (with `stars`), `_order`, and `_songs`, sets `_added_anything`. All three
  structural serializers (`_serialize_musicinfo/_tuning/_streaminfo`) key off
  `ref` from `_order` and read `_new_songs[ref]` + `_songs[i]`, never `song.k`,
  so the fake `k=len(sm._songs)` (vs the official `k=None`) is inert on the add
  path. Confirmed by song_manager's own self-test (lines ~969–977: "builder-style
  add (no _token) removes byte-exact"). Correct and safe.
- **Audio as sound.stream comp6.** Replace path uses `stage_replace` (preserves
  the slot's existing per-entry compression) and now *surfaces* that comp with a
  warning when it isn't 6 — good diagnosability. Add path creates the new group
  with `"compression": 6`, matching `build_archive`'s spec contract. Correct.

## Threading — safe

- `_BuildWorker` runs `fn` off the UI thread; failures are marshalled as
  `(_BUILD_ERROR, exc, traceback)` and detected via the identity sentinel
  `_BUILD_ERROR is` — structurally unambiguous vs the success 3-tuple
  `(list_bytes, data_bytes, summary)`. Good.
- `_on_built` calls `worker.deleteLater()` and drops the ref, so a second build
  gets a fresh QThread and the old one isn't GC'd while alive. Correct.
- All QMessageBox/validation happens in `_build` on the GUI thread before the
  worker starts; the worker only touches pure data + archive staging. No QWidget
  is constructed off-thread anywhere on either path.

## Robustness / error handling — reasonable, minor gaps

- Per-asset try/except with `summary["errors"]`/`["warnings"]` means one failing
  texture/chart/star/audio doesn't abort the rest. Good partial-failure story.
- Missing TJA / audio / template-not-found are all handled: file-existence checks
  in `_build` before dispatch; `prepare_new_song_db`/`add_new_song` raise
  `ValueError` for duplicate/missing ids, surfaced via QMessageBox.
- TJA encoding fallback (`_read_tja_text`) tries real encodings before lossy
  UTF-8 and warns — avoids silent U+FFFD corruption.

Minor (non-blocking):

1. **Short-tuning star write is less guarded than the reader.** In `build_song`
   (lines ~208–210) the write guards only `blk < len(tu.blocks)` but then indexes
   `.records[di].values[5]` with no `di < len(records)` / `len(values) > 5`
   guard, unlike `_template_stars` which wraps `records[di].values[5]` in
   try/except. On a corrupt/short tuning this raises IndexError — but it is
   inside the stars try/except, so it degrades to a recorded error rather than a
   crash. Cosmetic asymmetry, not a bug.
2. **`add_new_song` re-validates ids** (dup/missing) even though
   `prepare_new_song_db` already did, and re-derives `stars` if not passed. This
   is defensive duplication, not a defect; it keeps `add_new_song` correct when
   called without the GUI prep. Fine.
3. **`do_stars` with no charts staged** still writes tuning if `tja_levels` is
   non-empty (independent of `do_charts`), which is intended per the option
   checkboxes. No issue, just noting the coupling is deliberate.
4. **`_db_add_song` coupling** to SongManager internals is documented in the
   docstring and asserted by song_manager's self-test; acceptable but remains a
   maintenance tripwire if song_manager refactors `_Song`/`_new_songs`.

## Verdict

No correctness bugs; threading is safe (QWidget strictly GUI-thread, sentinel
error channel, worker lifecycle correct); robustness handles missing inputs and
partial failures cleanly. Held just below the mid-90s only by the minor
guard-asymmetry (#1) and the tight SongManager coupling (#4). 91/100.

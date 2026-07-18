# Code Review: `song_builder.py`

**File:** `E:\Taiko No Tatsujin 8\song_builder.py`
**Reviewer verdict:** production quality is held back by 2 CRITICAL and 4 HIGH issues.

## SCORE: 78/100

Core logic is correct (verified against sibling modules `tja2sht.py`, `bineditor_tuning.py`):
TJA course-index→course-name mapping matches `_COURSE_ALIASES`; the `2*k` / `2*k+1` block pair correctly
addresses 1P/2P in the 180-block tuning pool; `find_charts`/`find_textures` group-name patterns and the
new-group compression values (textures/charts comp 2, audio comp 6) are consistent across the replace and
add paths; the GUI-thread DB split is honored by the actual dialog caller. Deductions are for robustness,
misleading docs/dead parameter, encoding handling, and an unguarded star read.

---

## CRITICAL

### C1 — `_template_stars` unguarded index read; inconsistent with the writer's bounds check
- **Location:** `song_builder.py:362-364` (read) vs `:171-173` (write)
- **Problem:** `_template_stars` returns `tu.blocks[2*k].records[di].values[5]` with no bounds check. The
  star *writer* in `build_song` guards every access with `if blk < len(tu.blocks)`. The reader does not. On a
  short/corrupt tuning file, or a slot beyond the parsed block count, this raises `IndexError`, which is **not**
  caught inside `_template_stars` → propagates through `resolve_new_stars` (no try) → `prepare_new_song_db`,
  surfacing as a confusing hard dialog error. Inconsistent with the writer.
- **Fix:**
  ```python
  def _template_stars(archive, k: int) -> list:
      tu = TU.parse(_read_named(archive, "tuning.bin"))
      blk = 2 * k
      if blk >= len(tu.blocks):
          return [1, 1, 1, 1]  # sane default
      return [tu.blocks[blk].records[di].values[5] for di in range(4)]
  ```

### C2 — `_BuildWorker` error sentinel is fragile
- **Location:** `song_builder.py:415-418` (emit) vs `:552` (detect)
- **Problem:** The worker emits `("ERROR", exc, traceback)` on failure and the success path of `add_new_song`
  returns `(list_bytes, data_bytes, summary)` — both 3-tuples. Disambiguation relies solely on
  `res[0] == "ERROR"` being false because `list_bytes` is `bytes`, not the string `"ERROR"`. This works today
  but is fragile: any change to the success-tuple shape risks a silent misclassification of a failed build as
  success (or vice versa).
- **Fix:** Use an unambiguous marker, e.g. emit `("__BUILD_ERROR__", exc, tb)` and key detection on that
  marker, or wrap results in a small dataclass:
  ```python
  # worker:
  self.done_sig.emit(("__BUILD_ERROR__", exc, traceback.format_exc()))
  # _on_built:
  if isinstance(res, tuple) and res and res[0] == "__BUILD_ERROR__":
      ...
  ```

---

## HIGH

### H1 — Docstrings and `stream_root` param lie about disk write + `.bak` backup
- **Location:** module docstring `song_builder.py:14,18`; `build_song` docstring `:115-116`; impl `:183-196`;
  unused param `:111`
- **Problem:** The docs claim audio is "written to disk under test/sound/stream … with a .bak backup". The
  implementation instead calls `archive.stage_replace(...)` to stage the VAG into DATA.000 — it never touches
  `stream_root`, never writes a `.bak`, and `stream_root` is an entirely unused parameter. For an asset-safety
  tool this is actively misleading.
- **Fix:** Delete the dead `stream_root` parameter from `build_song` (the dialog passes nothing for it) and
  rewrite both docstrings to state audio is staged into `sound.stream.music_<id>/vag` inside DATA.000. If the
  disk+`.bak` behavior is actually still desired, implement it; otherwise drop the claim.

### H2 — Replace-mode audio compression is implicit and unverified
- **Location:** `song_builder.py:189` vs `:350-351`
- **Problem:** `add_new_song` creates the new audio group with explicit `"compression": 6` (the spec for
  `sound.stream`). `build_song`'s replace path calls `stage_replace(...)` with raw VAG and relies on whatever
  compression the existing entry already had — never asserted, never logged. If a slot's existing entry was
  stored with a different compression than the game expects for that group, the result is silently wrong and
  undiagnosable.
- **Fix:** Confirm (and document) that `stage_replace` preserves per-entry compression, and log the
  compression used for the staged audio so a bad slot is diagnosable. Ideally assert it equals the comp-6
  contract used by the add path.

### H3 — Audio is excluded from the `changed` flag; audio-only replaces look like no-ops
- **Location:** `song_builder.py:567` (and `:118`, `:570`)
- **Problem:** `self.changed = self.changed or summ["textures"] or summ["charts"] or summ["stars"]` omits
  `summ["audio"]`. A build that replaces *only* audio stages real edits into the archive but leaves
  `self.changed == False`, so the user/main window may not flag unsaved staged edits.
- **Fix:**
  ```python
  self.changed = (self.changed or summ["textures"] or summ["charts"]
                  or summ["stars"] or bool(summ["audio"]))
  ```

### H4 — Off-GUI-thread QWidget construction is guarded only by caller discipline
- **Location:** `song_builder.py:253-261` (`compute_db_add` builds a `SongManager` QWidget) and `:305-306`
  (called inside `add_new_song`, which runs in `_BuildWorker`, a `QThread`)
- **Problem:** `compute_db_add` instantiates `SongManager` (a QWidget). If `add_new_song` is called from a
  worker thread with `precomputed_db is None`, it constructs that QWidget off the GUI thread → undefined
  behavior / crash. The dialog always passes `precomputed_db` (`:512,523`), so it is safe in practice, but the
  safety depends entirely on caller discipline; any other caller breaks it.
- **Fix:** Assert GUI-thread affinity in `compute_db_add`
  (`QThread.currentThread() == QApplication.instance().thread()`), or refactor `SongManager`'s DB-build logic
  out of the QWidget so it has no thread affinity.

---

## MEDIUM

### M1 — Hard-coded UTF-8 read with `errors="replace"` silently corrupts Shift-JIS TJAs
- **Location:** `song_builder.py:501-502`
- **Problem:** Many real-world TJAs are Shift-JIS. Reading as UTF-8 with `errors="replace"` never raises but
  turns title/metadata (and possibly note data) into U+FFFD, producing wrong textures and possibly malformed
  charts — with **no error surfaced**. The tool appears to succeed while writing garbage.
- **Fix:** Try UTF-8, fall back to Shift-JIS (and/or detect via BOM/`chardet`); surface a warning if
  replacement characters were produced:
  ```python
  raw = Path(p).read_bytes()
  for enc in ("utf-8-sig", "utf-8", "shift_jis", "cp932"):
      try:
          tja_text = raw.decode(enc); break
      except UnicodeDecodeError:
          continue
  else:
      tja_text = raw.decode("utf-8", errors="replace")  # last resort, warn
  ```

### M2 — Ura(4) and Oni(3) collapse to one `"oni"` star slot with silent last-wins
- **Location:** `song_builder.py:377-385`
- **Problem:** `idxmap = {..., 3: "oni", 4: "oni"}`. If a TJA defines both Oni and Ura with different LEVELs,
  whichever course is iterated last overwrites the single `"oni"` star slot (typically Ura, parsed later). The
  engine has no 5th slot so collapsing is necessary, but the silent last-wins is a latent surprise.
- **Fix:** Deliberately prefer index 3 (Oni); only take index 4 (Ura) when 3 is absent:
  ```python
  # set "oni" from cidx 3 first; only fall back to 4 if 3 produced no level
  ```

### M3 — Star levels are silently clamped with no trace
- **Location:** `song_builder.py:173,248`
- **Problem:** `max(1, min(10, lvl))` silently clamps out-of-range TJA levels (e.g. an experimental "12"
  becomes 10) with no log/warning and no entry in `summary`. The user has no way to know a value was altered.
- **Fix:** When a clamp occurs, append an informational note (not an error) to `summary` so the change is
  visible.

### M4 — `find_textures` `select_non` uses an over-broad, order-dependent prefix match
- **Location:** `song_builder.py:79-82`
- **Problem:** `select_full` requires group `== "music_texture.music_select"` (exact), but `select_non` matches
  any group `startswith("music_texture.music_select_")`. They don't collide today (the select_full group name
  lacks the trailing underscore), but the asymmetry is fragile: a future group like
  `music_texture.music_select_extra` would be misclassified as `select_non`.
- **Fix:** Anchor `select_non` to the exact known group name(s), or document why the prefix is required.

### M5 — DB-build threading contract is conventional, not enforced
- **Location:** `song_builder.py:253-261` (`compute_db_add`), `:305-306`
- **Problem:** Same root as H4 from the contract angle: the "MUST run on the GUI thread" rule for
  `compute_db_add` is only honored because the dialog precomputes the DB. Nothing enforces it.
- **Fix:** Add the GUI-thread assertion described in H4, or remove the QWidget dependency from the DB-build
  path.

---

## LOW

### L1 — Duplicate `import songtex_all`
- **Location:** `song_builder.py:30` (module) and `:288` (inside `add_new_song`)
- **Problem:** The local re-import is redundant; the module-level import already covers it.
- **Fix:** Remove the local `import songtex_all` at `:288`.

### L2 — `copyright` shadows the Python builtin
- **Location:** `song_builder.py:110,277` (and call sites)
- **Problem:** Cosmetic; flagged by linters.
- **Fix:** Rename the parameter to `copyright_` or `copyright_text`.

### L3 — Tight coupling to `SongManager` private internals
- **Location:** `song_builder.py:221-236` (`_db_add_song`)
- **Problem:** Reaches into `_songs, _new_counter, _new_songs, _order, _added_anything, _build_result` — any
  refactor in `song_manager` silently breaks this.
- **Fix:** Add a public API on `SongManager` for programmatic add, or at minimum pin the dependency with a
  comment naming the exact internals relied on.

### L4 — Repeated O(groups × files) scans and redundant `tuning.bin` parses
- **Location:** `song_builder.py:44`, `:160-164`, `:363` (tuning re-parsed 3+ times per new-song build);
  `find_textures`/`find_charts`/`find_named_entry`/`find_group_file`/`song_ids` each re-walk `archive.layout`
- **Problem:** Correctness is fine; for a 90-song archive it is tolerable but wasteful.
- **Fix:** Parse `tuning.bin` once and thread the model through; cache the layout walk where practical.

### L5 — Worker/progress lifecycle smell
- **Location:** `song_builder.py:544-547`, `:550`
- **Problem:** `self._worker` is never cleared after completion; a second build reuses the attribute, allowing
  a prior `QThread` to be GC'd while possibly not fully finished. (`_on_built` accessing `self._prog` is safe
  because the worker only starts after `_prog` is created.)
- **Fix:** Disconnect/clear `self._worker` in `_on_built` (e.g. `self._worker.deleteLater(); self._worker = None`).

---

## What must change to reach 90+

1. **C1** — guard `_template_stars` against short tuning blocks (real crash path).
2. **C2** — make the worker error sentinel unambiguous.
3. **H1** — fix the misleading docstrings and remove the dead `stream_root` parameter.
4. **H2** — make replace-mode audio compression explicit/verified to match the comp-6 add path.
5. **H3** — include audio in the `changed` flag so audio-only replaces register.
6. **M1** — handle Shift-JIS TJA encoding (most likely real-world failure).

Addressing the two CRITICAL and four HIGH items, plus M1, is the path to a 90+ score.

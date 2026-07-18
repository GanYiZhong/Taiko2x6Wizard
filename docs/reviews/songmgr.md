# Code Review — `song_manager.py`

**File:** `E:\Taiko No Tatsujin 8\song_manager.py`
**Reviewed against dependencies:** `bineditor_musicinfo.py`, `bineditor_tuning.py`, `bineditor_streaminfo.py` (all read).
**Method:** static review + targeted runtime probes (the built-in 26-case self-test passes, but it only ever removes the *last-added* new song or an original song, so the bugs below sit in untested paths).

**SCORE: 58 / 100**

---

## Findings

### CRITICAL — `song_manager.py:598-613` (`_on_remove`, new-song branch)
**Problem:** Removing any new (added) song other than the most-recently-added removes the WRONG token and silently corrupts all three bins.
The match loop uses `spec["id"] == song.id and spec["template_k"] == song.k`. For a new `_Song`, `song.k` is set at creation to `len(self._songs)` (e.g. 90, 91, 92), while `spec["template_k"]` is the *source* row index (e.g. 5, 6, 40). These can never be equal, so `tok` stays `None` and the fallback

```python
for key in reversed(list(self._new_songs.keys())):
    tok = key
    break
```

always pops the **last-created** token regardless of which row the user selected.

**Proven impact (runtime probe):** add `bou_copy` (source row 5) then `konan3_copy` (source row 40), then select and remove `bou_copy`. The saved `musicinfo.sec0[-1]` becomes `[145, 151, 4, 1, 1, -1, 0, 1673, 36, 50, 28, 47]`, which matches **neither** source row:
- cols 0,1,8,9,10,11 (`145,151,36,50,28,47`) are cloned from **row 5 (bou)** via the stale `template_k=5`;
- cols 2 (genre=4) and 7 (score=1673) are overlaid from **row 40 (konan3)** by the positional `zip(self._songs, self._order)` in `_serialize_musicinfo`.

The tuning chart block is cloned from bou but stamped with konan3's id/stars. Counts stay numerically consistent (91/91/91) so shallow checks pass, but the retained record is a hybrid of two different songs — the "structural add/remove keeping all 3 bins consistent" guarantee is violated.

**Exact fix:** give each new song a stable identity tied to its token. In `_on_add`, after creating `new_song`, set `new_song._token = token`. In `_on_remove`, replace the entire id/template_k match block (lines 599-613) with:

```python
tok = getattr(song, "_token", None)
if tok in self._order:
    self._order.remove(tok)
self._new_songs.pop(tok, None)
```

Delete the `spec["id"]/template_k` loop and the reversed-keys fallback entirely.

---

### HIGH — `song_manager.py:330-331, 564-574` (`_Song.k` overloaded)
**Problem:** `_Song.k` means two different things. For original songs it is the original index (0..89); for new songs `_on_add` sets `k=len(self._songs)` (a row counter ≥ 90). But other code indexes original tables with it, e.g. `_serialize_streaminfo` value-only path:

```python
orig_name = f"music_{ids[song.k]}" if song.k < len(ids) else None
```

For a new song `k ≥ 90`, so this silently no-ops — safe only by luck because new songs currently only reach the structural path. This conflation is the root enabler of the CRITICAL bug.

**Exact fix:** never store a row counter in `k`. Keep `k` = original index, and use `None` (or a sentinel) for new songs. Carry the template/source index exclusively in `_template_k`. Audit every `ids[song.k]` and `self._orig_songs[...]` access (lines 405, 739, 761) to handle `k is None` for new songs explicitly.

---

### MED — `song_manager.py:744` (`_serialize_streaminfo`, value-only path)
**Problem:** A stream rename longer than its pool slot is silently fatal to the whole save. `SI._apply_name` raises `ValueError` when `len(name)+1 > capacity` (slots can be as small as 11 bytes). `_on_save` (line 631) catches it and shows "Save error", aborting the entire save — including unrelated star/genre edits in other rows.

**Proven impact (probe B):** renaming `music_1rin` (11-byte slot) to a longer string raises `'...' needs 110 bytes but slot holds 11`.

**Exact fix:** validate stream-name byte-length against `rec.capacity` at edit time (in `_harvest_table` or a pre-save pass) and surface a per-row error, OR route renames that exceed the slot through `_stream_rebuild` (which repacks the pool with no capacity limit) by treating an over-long rename as a structural change. At minimum, do not let one bad cell discard edits that did fit.

---

### MED — `song_manager.py:592` (`_on_remove`, last-song guard) / structural rebuild at small N
**Problem:** The guard `if len(self._songs) <= 1` prevents removing the final song, but the structural rebuild is unproven at very small song counts. `_pool_segments` derives `std_2p_tail_len` from "any odd block that isn't the final one" (lines 106-110); with one remaining song there is no such block, `std_2p_tail_len` stays `None`, and `trailer` falls back to `b""` (line 115), changing the song-0 id-token relocation logic. Removing down to 1-2 songs is reachable and untested.

**Exact fix:** add tests removing down to 1 song. If the trailer/song-0 relocation cannot be guaranteed at N=1, raise an explicit `ValueError` in `_pool_segments`/`_tuning_rebuild` rather than emitting a malformed file.

---

### MED — `song_manager.py:287-291` (`_stream_rebuild`, dead code)
**Problem:** Line 287 builds `records = [...]` with an inline comment admitting it is imprecise, then lines 294-299 rebuild `new_records` from scratch; `records` is never used. Dead, contradictory, and a maintenance hazard.

**Exact fix:** delete line 287 and the two explanatory comment paragraphs (lines 288-292); keep only the `new_records` construction.

---

### LOW — `song_manager.py:642, 658, 700, 728` (`structural` flag inconsistency)
**Problem:** `_build_result` computes `structural` and passes it to all three serializers, but `_serialize_musicinfo` ignores its `structural` parameter (it always rebuilds from `_order`). Misleading.

**Exact fix:** drop the unused parameter from `_serialize_musicinfo`, or add an assertion documenting the `_order`-vs-identity invariant it relies on.

---

### LOW — `song_manager.py:508-523` (`_harvest_table`, no id validation)
**Problem:** Manual table edits are not re-checked for empty or duplicate `song.id`. A blanked id yields `music_` records and tuning stems like `1p_e` with no id. Add-time uniqueness is enforced (lines 546-551) but post-hoc edits bypass it.

**Exact fix:** in `_harvest_table` (or before `_build_result`), re-validate that every `song.id` is non-empty and unique; raise `ValueError` with the offending row otherwise.

---

### LOW — `song_manager.py:632` (`_on_save`, error reporting)
**Problem:** `_on_save` reports `repr(exc)` and discards the whole save on a single bad cell. Acceptable for a dev tool but compounds the MED stream-slot-overflow issue.

**Exact fix:** report partial success / which row failed, and avoid discarding bins that serialized cleanly.

---

## What must change to reach 90+
1. Fix the **CRITICAL** `_on_remove` token mismatch — silent data corruption on a core advertised operation.
2. Resolve the **HIGH** `_Song.k` overloading so original-vs-new indexing is unambiguous (prevents recurrence of #1).
3. Handle the **MED** stream-slot overflow so a single oversized rename cannot abort an entire multi-row save.

The structural machinery itself (pool-segment split, song-0 trailer relocation, tuning/streaminfo rebuilds) is well-reasoned and round-trips correctly: no-op, single-edit (both 1P/2P star blocks), single-add, add-then-remove-last, remove-original, and remove-song0 were all verified byte-correct. The failure is concentrated in the add/remove bookkeeping (`_order` / `_new_songs` / `_Song.k`), which is enough to disqualify production use until fixed.

**SCORE: 58 / 100**

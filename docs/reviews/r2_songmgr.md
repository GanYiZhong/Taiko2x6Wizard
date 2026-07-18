SCORE: 91

# Re-review: song_manager.py (cross-bin Song Manager)

Scope: correctness of the index/id join, star edits reaching BOTH 1P and 2P blocks,
structural add/remove keeping musicinfo + tuning + streaminfo consistent, no-op
byte-identity, and add-then-remove byte-identity, plus robustness/error handling.

## Verdict
No correctness bugs found in the core edit/add/remove paths. Multi-bin state stays
consistent across the three files, and the two byte-identity invariants
(no-op save, add-then-remove) hold by construction and are covered by the self-test.
The earlier findings that motivated this revision appear fixed. Score held just
below 95 by two residual data-loss edge cases (not corruption) and a couple of
robustness gaps around value-domain validation.

## Confirmed correct

- **Index/id join** (`_assemble`, 410-436): song `k` is aligned across
  `mi.sec0[k]`, `tu.blocks[2k]`, and the `music_<id>` streaminfo record via
  `stream_by_name`. `id` comes from the tuning `music_` pool (`_music_ids_from_tuning`),
  the single source of truth, so the three bins share one identity per k.

- **Stars to BOTH 1P and 2P** — value-only path (745-749) writes
  `blocks[2k].records[di]` AND `blocks[2k+1].records[di]` for all 4 diffs;
  structural path (762-764) does the same before rebuild, and the rebuild
  (`_tuning_rebuild`, 251-254) patches both `b1` and `b2` for new songs.
  Self-test #2 asserts both blocks 6 and 7 == 9. Correct.

- **No-op byte-identity** — `structural` is `self._order != list(range(N))`
  (680), so a pristine session takes the value-only serializers; musicinfo edits
  are diff-gated per column (724-733) so mirror columns 17/19 are only rewritten
  when score actually changed. Self-test #1 asserts `{}`.

- **Add-then-remove identity** — removing a new song pops its token from
  `_order` (646-648), restoring `range(N)`; `structural` flips back to False and
  every bin round-trips byte-exact. Self-test #4 asserts `{}`. The token-based
  removal (533/608/638) is order-independent, and self-tests #7/#8 cover
  remove-first-added and remove-middle-added correctly.

- **Structural consistency** — all three serializers key off the same
  `self._order`/`self._new_songs`. musicinfo is always rebuilt from `_order`;
  tuning and streaminfo rebuild from the same order set. Counts are written
  coherently: `m.counts[0]` (736), tuning record[0] (`_tuning_rebuild` 289),
  streaminfo header count (330). Self-tests #3/#5/#6 confirm 91/89/89 song
  counts and matching streaminfo counts.

- **Robustness — last-song removal** guarded (627), **duplicate/empty id**
  re-validated in `_harvest_table` (546-555) so post-hoc table edits cannot
  smuggle a blank or colliding id into a structural save.

- **Error handling** — `_harvest_table` raises on bad ints and is wrapped in
  try/except for add/remove/save (566, 618, 659); `_build_result` failures are
  caught before any write, so bins are never half-applied. `_tuning_rebuild`
  refuses (213-218) rather than emit a malformed file when the song-0 trailer
  cannot be established.

## Residual findings (why not 95+)

1. **[Low — data loss, not corruption] Stream rename is dropped on a structural
   save.** In `_serialize_streaminfo` structural branch (804-814), kept/added
   stream records are resolved from the ORIGINAL `music_<id>` template name
   (`orig_name`, `tmpl_name`), never from the edited `song.stream`. So if a user
   renames a stream AND also adds/removes any song in the same session, the
   rename is silently lost. The value-only path honors renames; the structural
   path does not. Not corrupting, but surprising.

2. **[Low — data loss] Added song with an emptied stream cell.** `_on_add`
   seeds `song.stream = "music_<new_id>"` only if the template had a stream
   (602). If the user clears the stream cell before save, the structural branch
   skips adding a stream record (`if ... and song.stream`, 809) — the new song
   ends up with no streaminfo entry while musicinfo/tuning have it. Consistent
   with "add duplicates DB entry" intent but produces an asymmetric row; worth a
   guard or a note.

3. **[Low — robustness] No value-domain validation.** `_harvest_table` accepts
   any `int()`-parseable value for genre/stars/score. Negative or oversized
   values (e.g. a star > fits, or score exceeding the int32 packed by
   `struct.pack("<i", ...)`) will raise deep in `struct.pack_into`/serialize with
   a less friendly message, or write an out-of-range gameplay value. A range
   check (stars 0..N, non-negative scores, int32 bounds) would harden this.

4. **[Very low] Non-ASCII stream/id.** `_make_song_segment` (158) and
   `_stream_rebuild` (321) encode id/name as strict `ascii`; a pasted non-ASCII
   id raises `UnicodeEncodeError` at save rather than being rejected at harvest
   with a clear row-pointed message. Title uses UTF-8 (`_segment_title`) so ids
   being ASCII-only is a reasonable constraint, just report it earlier.

5. **[Very low] Duplicate template stream names in `_stream_rebuild`.** Pool
   dedup (318-322) points multiple records at one offset when names coincide.
   Harmless for reads, but two added songs given the same explicit stream name
   would share a pool entry; acceptable, noted for completeness.

## Summary
Core cross-bin correctness, dual-block star writes, structural consistency, and
both byte-identity invariants are sound and test-covered — clearing the 90 bar.
Remaining items are edge-case data-loss (rename-under-structural, emptied stream)
and missing value-domain validation, all low severity, holding the score at 91.

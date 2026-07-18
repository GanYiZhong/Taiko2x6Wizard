SCORE: 91

# Re-review: ps2hdd.py (PS2 APA + PFS read/write, revision 2)

Weighting write safety heavily. Read-only. This revision closes the earlier
findings (M1 APA checksum enforcement, M2 strict chain walk, C2 tail-in-correct-
descriptor tracking, C3 number_data self-slot accounting, C4 commit ordering,
H1 overlap refusal, H2 pre-flight, H3 reserved-zone verification, H4 timestamps).
I re-traced every allocation/commit path independently rather than trusting the
comments; the write path is now sound for normal use. Below: what I verified,
then residual findings.

## Write-safety verification (the load-bearing checks)

1. **No device write occurs before the plan is fully committed to memory.**
   The entire `_grow` allocation loop (ps2hdd.py:1271-1341) mutates only
   in-memory state: `PfsBitmap.bits`, the in-memory `chain[*]["raw"]` buffers,
   and Python-int counters. All `self.dev.write_*` and `bm.flush()` calls live
   in the COMMIT block (1357-1374). Therefore any exception raised mid-loop
   (`_search_free_zone` ENOSPC, guard trip, overlap) discards the object state
   and leaves the image byte-identical. This is the single most important
   property and it holds. VERIFIED.

2. **Pre-flight space estimate is a safe over-estimate.** The probe (1231-1240)
   assumes exactly 1 zone per data slot (`rem_probe -= 1`), so it counts the
   MAXIMUM possible data slots and hence the MAXIMUM possible SEGI crossings.
   The real loop packs up to 0xFFFF zones per tail-expand slot and up to 32 per
   fresh extent, so it consumes >= as few slots and therefore <= as many SEGIs.
   Real SEGI count can never exceed the probe's `worst_segis`. VERIFIED — no
   path where the loop opens more descriptors than were reserved.

3. **No zone double-allocation.** Every allocation funnels through
   `alloc_contiguous -> mark_used`, which HARD-RAISES on any already-set bit
   (536-552). The SEGI self-zone is marked used before the subsequent data
   extent is searched, so it cannot be re-handed. `verify_reserved` (498-506)
   refuses to allocate if any metadata (superblock/bitmap/log) zone reads FREE.
   Tail-expand scans `bm.test(z)==0` before claiming (1283-1288). VERIFIED —
   overlap is structurally prevented and additionally asserted by the synthetic
   test's cross-file occupancy check (6d) and free-count delta (5c-d).

4. **Commit ordering prevents dangling next_segment.** data zones -> bitmap
   flush -> sync -> NEW SEGI inodes -> sync -> MODIFIED inodes (carrying
   next_segment links + root counters) -> sync (1357-1374). A crash before the
   final step leaves the old inodes describing the old file and merely leaks
   reserved-but-unused zones. The link target (SEGI) is always on disk before
   the linker (existing inode's next_segment) is written. VERIFIED.

5. **Tail extent is edited in the descriptor that physically holds it.**
   `_walk_extents_loc` returns per-extent (segment_bi, slot); `_grow` maps that
   to a chain index (`tail_ci`/`tail_slot`) and edits `chain[tail_ci]["raw"]`
   at `0x28 + tail_slot*8` (1290-1291), not the newest SEGI. After a fresh
   extent, tail tracking is updated to `cur_ci, slot` (1339). VERIFIED.

6. **Strict chain walk before any mutation.** `pfs_write` calls
   `_walk_extents_loc(..., strict=True)` (1103) which raises on truncation,
   loop, or bad magic — so capacity is never underestimated from a truncated
   walk (which would trigger a spurious grow). VERIFIED.

7. **Checksums.** APA (348-353) and PFS inode (391-398) both sum words 1..255,
   skipping word 0; every touched descriptor is re-finalized before commit
   (1352-1353). APA header checksum mismatch aborts the whole table walk
   (676-680). VERIFIED against the documented pfsshell algorithm.

8. **Operator precedence / mapping spot-checks.** `length >> 4` for zones/subpart
   (765, `>>` binds looser than `-`), `_chunk_byte_off` sub0 => byte 0x402000 =>
   zone 513 (matches synthetic), zone->LBA `start + (zone<<scale)` all correct.
   VERIFIED.

## Residual findings

### High
None that cause corruption under normal use.

### Medium

M-1 (robustness) `_search_free_zone` caps a single request at 32 zones and
`_grow`'s tail-expand caps a run at 0xFFFF, but there is **no defense against a
pathologically fragmented free list forcing an enormous number of 1-zone
extents / SEGIs beyond `guard_max`**. `guard_max = extra + worst_segis + 8`
(1270). If the allocator is forced into all 1-zone extents, `extra` iterations
each append one zone, so the guard is adequate for that case — but the guard is
derived from `worst_segis` computed under the same 1-zone assumption, so it is
internally consistent. This is fine for correctness; the note is that a hugely
fragmented device produces a maximally fragmented inode (many SEGIs), which is
legal but slow and inflates `number_blocks`/descriptor zones. Not a corruption
risk. Consider surfacing a warning when descriptor overhead becomes large.

M-2 (correctness, latent) The pre-flight `_total_free_zones` sums free zones
across ALL subparts, but placement is contiguity- and subpart-ordered via
`_search_free_zone`. In the (currently untested) multi-subpart case, it is
theoretically possible for the total to satisfy `worst_case` while the loop
still exhausts a subpart and must wrap. That wrapping is handled by
`_search_free_zone`'s order sweep and is still all-in-memory, so a genuine
whole-device ENOSPC would raise before any commit (safe). The residual risk is
only that the estimate's SEGI count assumes the tail stays in one subpart; a
mid-file subpart change does not add descriptors, so the estimate remains an
over-count. Low likelihood, no corruption — but multi-subpart grow is exercised
by NO test (all tests use num_subs=0). Recommend a synthetic multi-subpart image
to lock this down before trusting it on a real multi-extent partition.

M-3 (robustness) `mark_free` (554-559) is used only by the synthetic test's
fragmentation setup, never by the production write path (shrink deliberately
does not free zones — documented at 1055-1057). That is a defensible design
choice, but it means repeated grow/shrink cycles monotonically consume zones
with no reclamation path in this tool; a caller looping writes could exhaust the
partition over time. Not corruption; document the non-reclaiming contract at the
`pfs_write` API level, not only in an internal comment.

### Low

L-1 `_grow` recomputes `number_blocks += seg_cnt` for the SEGI self-zone and
`+= d_cnt`/`+= add` for data. This matches the documented blockWrite.c
semantics and is cross-checked by test 5c-d's free-delta assertion, but there is
no independent assertion that on-disk `number_blocks` equals the sum of all
extent counts + descriptor zones after grow. Adding that invariant check to the
self-test would catch a future accounting regression directly.

L-2 `_update_root_inode` and `_grow` both refuse a bad SEGD magic on the root
inode but do not re-verify the root inode's stored checksum before mutating it
(they trust the `_resolve_path` read). A corrupt-but-magic-valid root inode
would be edited and re-checksummed, masking prior corruption. `pfsFsckStat`
WRITE_ERROR is honored (1097), which mitigates the realistic case; still,
asserting `checksum_valid` on the root inode before the first mutation would be
cheap insurance.

L-3 `pfs_datetime_now` imports `time` inside the function on every call; trivial,
cosmetic.

## Why 91 and not higher

The write path is correct and crash-safe for the tested single-subpart,
in-place/shrink/grow-with-SEGI scenarios, with strong structural guards against
double-allocation and mid-write corruption (all writes deferred past the last
fallible step). The gap to 95+ is coverage-shaped, not a known bug:
multi-subpart grow (M-2) is entirely unexercised, and there is no direct
`number_blocks`-consistency assertion (L-1). Those are the areas where an
undiscovered correctness bug could still hide. No corruption risk was found
under normal (single-partition) use.

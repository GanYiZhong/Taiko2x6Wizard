# Code Review: ps2hdd.py (PS2 APA/PFS read/write tool)

**File:** `E:\Taiko No Tatsujin 8\ps2hdd.py`
**Focus:** Write safety (this path mutates an 80 GB disk image), correctness, robustness, error handling, data-loss risk, clarity.

This is a careful, well-documented port. Read paths (APA walk, PFS mount, inode/extent traversal, directory parsing) are solid, and the copy-on-write overlay design for testing is genuinely good safety engineering. However, the grow/allocation **write path** has multiple correctness defects that can corrupt the filesystem, and the self-test only exercises a single happy-path grow on one hardcoded image, so those defects pass undetected.

---

## CRITICAL

### C1. Superblock free-zone accounting never updated -- on-disk inconsistency after every grow
**ps2hdd.py:886-1042 (`_grow`); no superblock writeback anywhere in the file.**
The allocator flips bitmap bits and updates inode `number_data`/`number_blocks`, but the PFS superblock's zone-allocation accounting (the free/used zone bookkeeping the real driver maintains, and `pfsFsckStat` at superblock 0x0C) is never written. The code reads `num_subs` (line 577) but writes nothing back to the superblock zone. After a grow the bitmap reports N fewer free zones while the superblock's accounting still reflects the old value, and `pfsFsckStat` is never set to flag "dirty/needs-check." A subsequent real-driver mount or pfsck can see a mismatch between the bitmap and the superblock free count.
**Fix:** After allocation, decrement the superblock free-zone count by the exact number of bitmap bits flipped (sum of `bm.allocated` across all touched subparts, including SEGI descriptor zones), set `pfsFsckStat` appropriately, recompute any superblock checksum, and write the superblock zone back. If the on-disk format has no such field, verify and document that explicitly against libpfs before shipping. This is the single most likely cause of "fsck repairs the disk" or silent corruption.

### C2. Contiguous-extend writes the updated extent into the WRONG inode/slot after a segment boundary
**ps2hdd.py:946-965**
The "expand last extent" branch writes the new count into `cur_seg` at slot `pfs_fix_index(root_number_data - 1)` (lines 962-965). But `new_extents[-1]` may physically reside in a *previous* segment descriptor, while `cur_seg` is the most recently created SEGI. After a SEGI is spun up (lines 975-1011), `cur_seg` changes but `new_extents[-1]` still points at an extent recorded in the *old* descriptor. The expand path then overwrites slot `fix_index(root_number_data-1)` of the *new* `cur_seg` (a self-pointer or unrelated slot) with that extent's blockinfo -- corrupting the segment descriptor and double-recording the zone.
**Fix:** Track, for the current tail extent, which segment inode and which data[] slot actually holds it (store `(seg_dict, slot_index)` alongside `new_extents[-1]`), and update *that* inode's slot. Do not blindly edit `cur_seg`. Simplest robust fix: never use the in-place expand branch across a freshly created descriptor.

### C3. `number_data` / `pfs_fix_index` self-zone accounting is unverified and only self-consistently tested
**ps2hdd.py:303-313, 654-669, 975-1026**
`root_number_data` is used both as "next logical data index" (`pfs_fix_index(root_number_data)`, lines 1018/1020) and is incremented by 1 for the SEGI self-zone (lines 995-996). `pfs_fix_index` maps logical index -> physical data[] slot assuming slot 0 of each descriptor is the self-pointer. Whether real PFS counts the per-segment self-slots in `number_data` is exactly the invariant that decides if `_walk_extents` (lines 654-669, which relies on `number_data` + `pfs_fix_index`) reads the file back correctly after multi-segment growth. The self-test never validates extents independently -- it grows once and round-trips read-back, so a mutually-consistent index bug (wrong in both writer and reader) passes.
**Fix:** Pin down precisely what `number_data` counts in libpfs (total data[] slots incl. self-pointers, or only real extents) and make `_grow` and `_walk_extents` use identical semantics. Add a test that grows a file across at least two SEGI boundaries and validates the extent layout with an independent count, not just read-back.

### C4. No crash-atomicity: an interrupted grow leaves a corrupt 80 GB image
**ps2hdd.py:1001-1037 (and 166-177)**
In writable (non-overlay) mode, `_grow` writes the previous segment's `next_segment` link to disk (line 1006) *before* the new SEGI inode and bitmap are flushed (lines 1035-1040). If the process dies between these writes, the on-disk chain points `next_segment` at a SEGI zone whose inode was never written (garbage/zeros, wrong magic) and whose bitmap bit may or may not be set. There is no journal, no write ordering (data -> metadata -> link last), and `write_sectors` flushes each call immediately (line 177), so safe reordering is impossible as written. The docstring's claim of "FINAL, self-consistent metadata" addresses the *end state*, not the *write sequence*.
**Fix:** For real-disk writes, order so the linking write (`next_segment`) is the LAST write, after all data, the SEGI inode, and the bitmap are durably flushed; ideally write everything, fsync, then the link, fsync. Alternatively refuse `writable=True` entirely and require overlay -> external atomic apply. At minimum, gate real-disk grow behind an explicit flag and document that an interrupted grow corrupts the image.

---

## HIGH

### H1. `mark_used` silently re-marks already-used zones instead of failing
**ps2hdd.py:398-403, 954-959**
`mark_used` only increments `allocated` when a bit was 0; if any target zone is already used it sets the bit "used" with no error. Combined with C2/C3, an overlapping extent passes silently and the data-write phase (lines 867-876) then overwrites another file's zone, since that phase blindly trusts `new_extents`.
**Fix:** Make `mark_used` assert each target bit is currently free and raise on an already-used bit, turning any overlap into a hard failure instead of silent data loss.

### H2. No pre-flight space reservation; `guard` cap can abort mid-write
**ps2hdd.py:609-631, 941, 1014-1032**
`_search_free_zone` may return fewer zones than requested (it halves on failure). Under heavy fragmentation the loop allocates 1-zone runs plus periodic SEGIs, and the `guard < 1000000` cap (line 941) can abort a legitimate large grow after data, bitmap, and some inodes were already written (the contiguous-extend branch writes counters; the final persist at 1035 still runs) -- leaving a partially grown, inconsistent file.
**Fix:** Pre-compute the worst-case zone need (data extents + descriptor zones) and verify `free_count()` across all candidate subparts *before* writing anything. Fail early with ENOSPC and zero side effects. Reserve the full allocation in memory before any device write.

### H3. No assertion that reserved metadata zones are marked used in the bitmap
**ps2hdd.py:413-465, 589-600, 975-1016**
The allocator scans `self.bits` over `total_zones` and assumes the on-disk bitmap already marks all reserved metadata zones (superblock zone 512, bitmap chunk zones, log zones) as used. The tool never validates this. If any reserved zone reads as free, the allocator hands it out and the data-write phase clobbers the superblock/bitmap/log.
**Fix:** On mount, assert that the superblock zone, all bitmap chunk zones for each subpart, and the log zones read as USED before permitting any allocation; refuse to write otherwise.

### H4. Inode timestamps (mtime/ctime) never updated on write/grow
**ps2hdd.py:878-884, 1034-1037**
Only `size` (0x3D8) and the checksum are updated; `mtime` (0x3D0) and `ctime` (0x3C8) are left stale after a content+size change. A real PFS driver updates these.
**Fix:** Update ctime/mtime on the root inode before recomputing its checksum.

---

## MEDIUM

### M1. APA header checksum decoded but never validated
**ps2hdd.py:251, 512-530**
`self.checksum` is decoded but never verified on read, so a corrupt APA table is walked silently. Read-only today, but a malformed header could mis-locate partition `start`, and the write path would then write to wrong LBAs.
**Fix:** Validate the APA checksum on read; refuse writes if any header checksum is invalid.

### M2. `_walk_extents` silently truncates on a malformed segment chain
**ps2hdd.py:660-665**
On unexpected SEGI magic or a missing `next_segment`, it `break`s and returns a truncated extent list. In `pfs_write`, a truncated `extents` underestimates `capacity` (line 857), triggering an unintended grow that allocates new zones while the real data lived in the un-walked tail.
**Fix:** Raise on a malformed segment chain rather than silently truncating, especially before any write.

### M3. Shrink path leaks zones and never frees them
**ps2hdd.py:810-835, 859-865**
Shrinking keeps the allocated zones and the full extent list; the bitmap's free count then permanently overstates usage relative to file sizes. Documented as intentional, but combined with C1 it compounds bitmap/superblock divergence.
**Fix:** Implement extent truncation + `mark_free` + free-count update, or reject shrink writes explicitly.

### M4. `total_zones = length // 16` drops a partial trailing zone
**ps2hdd.py:589-595**
If `part.length` is not a multiple of 16 sectors, the last partial zone is excluded from `total_zones`, so a zone the on-disk bitmap may account for is invisible to the allocator and `free_count`.
**Fix:** Confirm zone-count rounding against libpfs `pfsGetScale`/zone-count logic and match it.

---

## LOW

### L1. No fsync on write
**ps2hdd.py:166-177** -- `write_sectors` flushes but never `os.fsync`; on a crash the OS can reorder/lose writes, undermining any ordering fix for C4. **Fix:** add `os.fsync(self.f.fileno())` at safe points.

### L2. Self-test hardcodes image path and target file
**ps2hdd.py:1073, 1233-1237** -- hardcodes a specific `.img` and `/list.bin`; silently skips coverage if absent. **Fix:** parameterize and skip/grow a known fixture.

### L3. `latin-1` name decode is lossless only by luck
**ps2hdd.py:255, 712** -- fine for read; risky if names are ever used to construct writes. **Fix:** document the byte-exact name contract or use bytes for write-side name handling.

### L4. Magic loop cap
**ps2hdd.py:941** -- `guard < 1000000` should be derived from `total_zones`, not a constant. **Fix:** bound the loop by the actual zone count.

---

## What must change to reach 90+

1. **C1** -- update and write back superblock free-zone accounting (+ `pfsFsckStat`) so the bitmap and superblock agree. Without this, every grow produces an fsck-dirty filesystem.
2. **C2/C3** -- fix segment-descriptor slot targeting and nail down `number_data` semantics; add a test that grows across multiple SEGI boundaries and validates extents independently of read-back.
3. **C4 + H2** -- reserve the full allocation in memory and verify free space before any write; order/fsync metadata so an interruption cannot leave a dangling `next_segment`. A partial failure must leave the image unchanged.
4. **H1/H3** -- make `mark_used` fail loudly on overlap, and assert reserved metadata zones are marked used before allowing allocation.
5. Broaden the self-test beyond one happy-path grow (fragmentation, ENOSPC, multi-segment, shrink) -- the current suite's mutually-consistent round-trip can pass even when the index math is wrong.

The read side and overlay design are genuinely good (would score ~88 on their own). The score is dominated by write-safety defects on a path that mutates an 80 GB image with no journal and no pre-flight space check.

SCORE: 58

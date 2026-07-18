SCORE: 92

# Re-review: taiko256_archive_tool_v2.py + archive_builder.py

The earlier findings have been addressed well. No correctness bugs found in the
core round-trip / offset / endianness / sector-math / single-copy-build paths.
The two crypt implementations are provably equivalent, and the reader/builder
name_base invariant is asserted on both sides. Robustness (bounds checks,
bounded decompress, mmap try/finally, overlap detection) is solid. Score 92:
above the 90 bar (no correctness bugs + reasonable robustness); held under 95 by
the minor issues below.

## Verified-correct invariants (evidence)

- crypt_list_fast == core.crypt_list: core uses `KEY[i & 0xFF]`; fast uses
  `np.tile(_KEY, reps)[:size]` with a 256-byte `_KEY`, so tile reproduces
  `KEY[i % 256] == KEY[i & 0xFF]` exactly, including the empty-input branch
  (arr.size==0 skips XOR). Guarded by runtime assert at archive_builder.py:55.
- name_base: reader ArchiveLayout uses `(gc*2 + fc + 2)*16`; builder derives
  `file_base=(gc+1)*32`, `name_base=file_base+fc*16`, which is algebraically
  identical. Asserted at archive_builder.py:212.
- Sector math / single-copy build: groups laid out at sector-aligned `out_off`,
  gaps zero-filled by pre-sizing `bytearray(total)`, each block written exactly
  once (archive_builder.py:229-238). mmap source, no second full copy.
- Endianness: all struct pack/unpack use `<` (little-endian) consistently on
  both read and write paths.
- Byte-exact name round-trip: read_cstr decodes latin-1; add_name/name_index
  encode latin-1. Matched. Existing name_offsets reused so re-runs don't grow.

## Remaining findings (all LOW / non-correctness)

### LOW — archive_builder.py:55 — assert stripped under `python -O`
The round-trip invariant `crypt_list_fast == core.crypt_list` and the name_base
invariant (line 212) are enforced only by `assert`. Running under `-O` removes
them, so a future key/layout regression would ship silently. For a byte-exact
archive tool these are load-bearing checks, not debug aids. Fix: promote to
explicit `if not ...: raise RuntimeError(...)`.

### LOW — archive_builder.py:55 — round-trip assert covers only a fixed sample
The assert tests `bytes(range(256))*3` (a length that is an exact multiple of
256 and byte-aligned). It would not catch a `reps`/slice off-by-one on a
non-multiple length or size 0. Consider also asserting an odd length (e.g.
`bytes(range(256))*3 + b"\x01\x02"`) and the empty input.

### LOW — taiko256_archive_tool_v2.py:339 — last-slot capacity includes trailer padding
`file_slot_capacity` returns `group_unpacked_size - start` for the file with the
highest offset, and callers pass `len(original_payload)` which includes the
PAYLOAD_TRAILER_SIZE/PAYLOAD_ALIGN trailer padding. So the last file may grow
into trailer padding during an in-place patch. This is within-group and does not
corrupt other files, but a group whose trailer is semantically meaningful to
hardware could be affected. Behaviorally consistent with the tool's conservative
contract; flagging as an assumption to document, not a bug.

### LOW — inconsistent `unknown2` (content-hash) semantics between the two tools
archive_builder recomputes `zlib.crc32(payload)` for edited/new groups
(builder:179,192) but preserves the original value for unchanged groups.
command_patch (v2:462) keeps the original `unknown2` even for CHANGED groups.
Both files document the field as NOT load-validated, so neither is a correctness
bug, but the divergent handling is a latent trap if the field ever becomes
validated. Recommend picking one policy and sharing a helper.

### LOW — taiko256_archive_tool_v2.py:261 — `import bisect` inside hot function
Minor: `import bisect` is inside `group_capacity_bytes`, called per changed
group. Import caching makes this cheap, but hoist to module scope for cleanliness.

### INFO — no unused/dead code, no endianness or bounds gaps found
extract validates `end > len(payload)` (v2:316); get_group_encoded_block
validates range and EOF (v2:206-209); _bounded_decompress caps expansion
(v2:213-223); mmap cleanup is in try/finally with memoryview release ordering
correct (dst before src_mv before src.close). All good.

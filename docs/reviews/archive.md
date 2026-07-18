# Code Review — Taiko SYSTEM256 Archive Core + Builder

**Files reviewed:**
- `E:\Taiko No Tatsujin 8\taiko256_archive_tool_v2.py` (XOR crypt, zlib groups, parse/extract/patch)
- `E:\Taiko No Tatsujin 8\archive_builder.py` (rebuilder: mmap source, pre-sized buffer, vectorized crypt)

**SCORE: 86 / 100**

The format-2 round-trip path (the path actually exercised) is correct — no proven correctness bug. The two non-obvious offset identities both verify, and XOR crypt encode/decode is symmetric, so byte-exact round-trip holds. The score is held below 90 by resource-safety and malformed-input robustness gaps, plus untested format-1.

---

## Verified-correct (non-obvious) properties

- **`crypt_list_fast` ≡ `core.crypt_list`**: `np.resize(_KEY, arr.size)` tiles the 256-byte key cyclically, matching `KEY[i & 0xFF]`. Equivalent. (Recommend a unit assertion + prefer `np.tile` for clarity, since `np.resize` repeat-vs-pad semantics are a footgun.)
- **Builder name_base ≡ reader name_base**: builder `(group_count+1)*32 + file_count*16` equals reader `(group_count*2 + file_count + 2)*16`. Algebraically identical → names stay byte-exact. (Recommend an assertion; one off-by-one in either file silently desyncs every name.)
- **Crypt symmetry**: `crypt_data_block` keys byte 0 of each 16-byte chunk by chunk index; encode applies it post-compression and decode pre-decompression — symmetric, round-trips. Partial tail chunk is in-bounds.

---

## Findings

### CRITICAL / HIGH

**[HIGH] Format-1 layout offsets unverified and structurally inconsistent**
`taiko256_archive_tool_v2.py:107-116`
For fmt==1: `file_base = group_count*28 + 8`, `name_base = group_count*28 + file_count*16 + 8`. For fmt==2 the bases use a denser packing with a 32-byte header and a sentinel group. The two formulas are structurally inconsistent and only fmt==2 is exercised anywhere. If the fmt==1 math is wrong, parse/extract/patch silently read garbage names/sizes.
**Fix:** Verify fmt==1 base/name offsets against a real format-1 LIST.BIN, or remove fmt==1 support to avoid a false capability.

**[HIGH] `build_archive` leaks mmap + file handle on exception**
`archive_builder.py:76-189`
`fsrc` / `src` / `src_mv` are opened raw; any exception between open and the `src.close()` at line 188 leaks the mmap and handle. On Windows a leaked mmap holds a lock on DATA.000, blocking the next run. This is the most concrete real-world failure in the slice.
**Fix:** Wrap the body in `try/finally` (or use `with`): mmap and the file both support context managers. Release `src_mv`/`dst`, then `src.close()`, then `fsrc.close()` in `finally`.

**[HIGH] In-place patch can corrupt aliased/overlapping file ranges**
`taiko256_archive_tool_v2.py:289-294, 321-327`
`file_slot_capacity` derives capacity from the next file by offset. If two entries in a group share or overlap a blob (same offset, or interleaved ranges), growing one zero-fills `payload[start:start+cap]` and overwrites the other.
**Fix:** Before in-place patch, detect overlapping/aliased file ranges within a group and refuse (or document the no-overlap assumption explicitly).

### MED

**[MED] `command_extract` only catches `EOFError`**
`taiko256_archive_tool_v2.py:255-262, 200`
A group with `packed_size == 0` (or otherwise corrupt zlib) makes `zlib.decompress(b"")` / `decompress` raise `zlib.error`, which is not caught by the `EOFError` handler and aborts the entire extract.
**Fix:** Also catch `zlib.error` and `ValueError` in the extract loop (honoring `--partial`), or special-case empty/zero-packed groups.

**[MED] Lossy name handling breaks byte-exact round-trip for non-ASCII names**
`taiko256_archive_tool_v2.py:93` and `archive_builder.py:84`
`read_cstr` decodes ascii/"replace" (non-ASCII → U+FFFD). `add_name` re-encodes `s.encode("ascii")` strict. A non-ASCII name round-trips to `�` on read then throws `UnicodeEncodeError` (or silently changes bytes) on rebuild.
**Fix:** Read and write names as raw bytes via latin-1 (`decode("latin-1")` / `encode("latin-1")`) to guarantee byte-exact round-trip.

**[MED] Empty-group patch path raises on missing directory**
`taiko256_archive_tool_v2.py:297-301`
`patch_group_payload_in_place` requires `group_dir.is_dir()` and raises `FileNotFoundError` even for a group with zero files that was never extracted.
**Fix:** Short-circuit and return `(original_payload, False, entries)` when `entries == []` before the directory check.

**[MED] Full output held in RAM, defeating the mmap optimization**
`archive_builder.py:177`
The source is mmap-ed specifically to avoid a second full copy, but `new_data = bytearray(total)` allocates the entire output in RAM, which can `MemoryError` on a multi-GB DATA.000.
**Fix:** Stream the output to a file — write each encoded block and copy each src range sector-by-sector — or explicitly document that peak RAM ≈ output size.

**[MED] Edited-group file `unknown` field forced to 0**
`archive_builder.py:113-124`
For retained (non-replaced) files inside an *edited* group, the rebuilt record hardcodes `"unknown": 0` instead of preserving `e["unknown"]`. Unchanged groups preserve it (line 99); edited groups do not.
**Fix:** Preserve `e["unknown"]` for retained files instead of forcing 0. (Low risk given the v2 docstring marks file `unknown` as `=0`, but inconsistent.)

**[MED] No zlib decompression bound (zip-bomb / OOM on corrupt input)**
`taiko256_archive_tool_v2.py:200`
`zlib.decompress(block)` for `compression==2` is unbounded; a corrupt/hostile group can exhaust memory.
**Fix:** Use a `decompressobj` with a `max_length` cap (or wrap and report the offending group). Low risk for self-owned archives.

**[MED] Duplicated padding/alignment logic can diverge**
`archive_builder.py:46-59` vs `117-128`
`_build_payload` and the inline edited-group loop are copy-pasted. They currently agree; two copies of alignment + trailer math are a future correctness hazard.
**Fix:** Call `_build_payload` from both paths, threading `name_offset` through the returned recs.

### LOW

**[LOW] Name table never deduplicated**
`archive_builder.py:82-85`
`add_name` always appends; re-running with the same appended name grows the table unboundedly across iterative rebuilds.
**Fix:** Maintain a `dict[str,int]` and reuse existing offsets.

**[LOW] Stale `unknown2` on edited groups**
`archive_builder.py:132`
Edited groups preserve the old `unknown2` content-hash even though the payload changed; new groups recompute `crc32`. Docstring says `unknown2` is not load-validated, so functionally safe but inconsistent.
**Fix:** Recompute `zlib.crc32(payload)` (or zero it) for edited groups for consistency.

**[LOW] `group_capacity_bytes` re-sorts all groups per call**
`taiko256_archive_tool_v2.py:214-221`
O(n log n) per group → O(n² log n) across a full patch. Fine for thousands; sloppy for tens of thousands.
**Fix:** Precompute the sorted sector list once and reuse.

**[LOW] `decode_group_payload` type contract relies on duck typing**
`archive_builder.py:107`
An `mmap` is passed where core annotates `bytes`. Works (slicing returns bytes, `len` works), but the `bytes` annotation is violated.
**Fix:** Widen the type hint to `bytes | mmap.mmap` or accept a buffer protocol, to make the contract explicit.

**[LOW] Empty new-group spec produces a zero-file group**
`archive_builder.py:135-144`
A `new_group_specs` entry with `files == []` yields a 64-byte zero-trailer payload and a 0-file group. Harmless but probably unintended.
**Fix:** Guard/reject empty `files` if not desired.

---

## What must change to reach 90+

Round-trip correctness on the format-2 path is already sound; the gap is robustness/resource-safety. Doing the following clears 90:

1. **(HIGH) `try/finally` around mmap/file in `build_archive`** — leaked mmap locks DATA.000 on Windows and breaks the next run.
2. **(MED) Catch `zlib.error` / empty groups in `command_extract`** so one bad group doesn't abort the whole extract.
3. **(MED) Latin-1 byte-exact name read/write** so non-ASCII names round-trip and rebuild doesn't throw.
4. **(MED) Short-circuit empty-group patch** before the directory check.
5. **(MED) Stream or document the full-output RAM buffer.**
6. **(LOW) Add the two round-trip assertions** (`crypt_list_fast == crypt_list`; builder name_base == reader name_base) and dedup the name table.
7. **(HIGH-if-used) Verify or drop format-1 offset math.**

**SCORE: 86**

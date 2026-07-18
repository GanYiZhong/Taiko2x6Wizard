# Code Review — 8 Taiko bin-editor modules

**Scope:** `bineditor_enso_parts.py`, `bineditor_lamp.py`, `bineditor_musicinfo.py`, `bineditor_rank.py`, `bineditor_tuning.py`, `bineditor_fname.py`, `bineditor_hdbdinfo.py`, `bineditor_streaminfo.py` (all in `E:\Taiko No Tatsujin 8\`).

**Read-only review.** Each module parses one Taiko gamedata `.bin` (count-prefixed tables, string pools, UTF-16/ASCII) and provides a PySide6 editor with a documented `serialize(parse(d)) == d` round-trip guarantee.

## SCORE: 68

All 8 pass their no-edit round-trip self-tests on the sample files, and the shared interface (`FILENAME`/`parse`/`serialize`/`Editor.result_bytes`) is structurally present everywhere. The set falls short of 90 because the **edit path** carries real correctness bugs, and parse-robustness plus interface consistency are uneven across modules.

---

## Interface consistency snapshot

| File | FILENAME | parse | serialize | result_bytes | parse on bad input | Qt import guarded |
|------|----------|-------|-----------|--------------|--------------------|-------------------|
| enso_parts | yes | yes | yes | yes | crashes uncaught | no |
| lamp | yes | yes | yes | yes | raw fallback | no |
| musicinfo | yes | yes | yes | yes | raises ValueError | no |
| rank | yes | yes | yes | yes | clamps | no |
| tuning | yes | yes | yes | yes | empty model | **yes** |
| fname | yes | yes | yes | yes | raises ValueError | no |
| hdbdinfo | yes | yes | yes | yes | raises ValueError | **yes** |
| streaminfo | yes | yes | yes | yes | raises ValueError | no |

Three different malformed-input contracts (raw-fallback / raise / clamp-or-empty), four different numeric-parse idioms (`_parse_int`, `_u32`, `int(...,0)`, `int(...)`), and only 2 of 8 guard the PySide6 import. That divergence is itself an interface-consistency defect.

---

## Findings

### CRITICAL

- **CRITICAL — `bineditor_enso_parts.py:144-157` — `serialize` ignores `pool_offset` and never recomputes header offsets, so edits can desync the header.**
  Problem: `parse` reads each section from `offsets[i]` (header words 8..14) and the pool from `offsets[6]`, but `serialize` writes `header` unchanged (line 147), then writes `header_tail` + sections back-to-back, then appends the pool with no padding to `pool_offset`. Any gap/alignment between `header_tail` end and `offsets[0]`, or between the last section and `offsets[6]`, is lost, and the (unchanged) header offsets no longer match actual section positions. Passes only because the sample tiles exactly.
  Fix: after editing, recompute `header[8..14]` from actual cumulative section sizes (`HEADER_SIZE + len(header_tail)`, then `+= count*recsize` per section, pool last) and write them back into `header`; and capture any inter-section/pre-pool padding as raw spans at parse time so they are re-emitted. Add a parse-time assert that modeled spans tile exactly to `pool_offset` and to EOF; fall back to a raw view otherwise.

- **CRITICAL — `bineditor_musicinfo.py:267-272` (and `113-159`) — offset recompute assumes zero inter-section padding; SEC0→o0 gap and any section padding are silently dropped.**
  Problem: `parse` comment at line 133 says "p should now equal o0" but never asserts it; SEC1/SEC2/SEC3/SEC4/SEC5 lengths are derived from `(o_next - o_prev)` yet `serialize` recomputes `o0..o5` purely from element counts (lines 267-272). If the real file has padding after SEC0 or between any sections, serialize emits a shorter file with shifted offsets — byte loss on save, even for a single edited field.
  Fix: at parse time validate each computed offset equals the parsed header offset (`o0 == HEADER_SIZE + c0*SEC0_RECSZ`, etc.); capture any gap bytes as raw spans and re-emit them; if validation fails, fall back to a raw round-trip view. Recompute header offsets including those preserved gaps.

### HIGH

- **HIGH — `bineditor_enso_parts.py:178, 203` and `bineditor_musicinfo.py:222` — `encode("ascii","replace")` corrupts non-ASCII names and is asymmetric with `decode(...,"replace")`.**
  Problem: decode maps high bytes to U+FFFD; re-encode maps any non-ASCII char to `b"?"`. In musicinfo, once `dirty_strings` is set by *any* edit, the rebuild (line 222) re-encodes **every** string, turning all non-ASCII pool names into `?` — byte corruption of untouched names.
  Fix: use `latin1` for both decode and encode (lossless 1:1 for bytes ≤0xFF), exactly as `lamp`/`hdbdinfo` do; or preserve the original bytes for any StrRef whose text is unchanged and only re-encode the edited ones.

- **HIGH — `bineditor_streaminfo.py:110-112 (capacity)` and `222-234 (_save)` — last-slot capacity includes trailing pool padding, so editing the final name can overwrite alignment bytes.**
  Problem: `cap = nxt - name_off` where `nxt` is the next strictly-greater record offset or `n_pool`. For the highest-offset name, `cap` runs to pool end and includes any trailing padding; a shorter replacement NUL-pads over those bytes, and a longer one is wrongly permitted to grow into them.
  Fix: cap each slot at its real NUL terminator: `cap = (slot.index(b"\x00")+1)` within the slot region, not the distance to the next record. Limit edits to `len(encoded)+1 <= true_slot_len`. Preserve trailing pool padding as a separate raw span.

- **HIGH — `bineditor_hdbdinfo.py:236 (_str_by_rel)`, `307-331 (_save)` — shared `name_ptr` aliasing: renaming one record silently renames every record pointing at the same pool slot, and record-tab vs string-tab edits have undefined precedence.**
  Problem: `_str_by_rel = {s.rel: s}` is one PoolString per offset. Multiple records can share a `name_ptr`; editing one rewrites the shared slot for all, with no warning. `_save` applies string-tab edits (step 1) then record-tab name edits (step 2) to the same slot, so the final value depends on iteration order when they disagree.
  Fix: detect when more than one record references a given `rel` and either disallow per-record rename for shared slots (raise a clear error) or surface a warning; define a single source of truth for names (resolve string-tab vs record-tab precedence explicitly, e.g. record-tab wins and then sync the string-tab cell).

### MEDIUM

- **MED — `bineditor_hdbdinfo.py:140-144` — `capacity` spans all trailing NULs up to the next non-null string, letting an edited name grow across an entire padding/empty region and overwrite later structure.**
  Fix: cap capacity at the slot's own terminator plus a deliberate, bounded reserve; do not treat a large NUL run as free space for one name.

- **MED — `bineditor_tuning.py:94-110 (_find_pool_offset)` — `data.find(b"music_")` can match inside the binary record region, mislocating the pool and producing a wrong editable view.**
  Problem: no validation that the discovered pool offset is consistent with block tiling. No-edit save is safe (serialize patches raw), but edits land at wrong offsets and the GUI shows wrong data.
  Fix: cross-check `pool_offset` against `starts[-1] + expected_block_size` and against `song_count`; require the pool region to be contiguous printable-ASCII to EOF; fall back to a raw view if checks fail.

- **MED — `bineditor_tuning.py:113-146 (_scan_blocks)` — brittle magic constants (`-300 < v < -1`, delta `> 200`) silently mis-tile or return `[]`, after which `parse` yields a zero-block model that the Editor presents as an empty record table with no warning.**
  Fix: validate `len(blocks) == 180` and that block sizes match the documented 972/984/988 pattern; if discovery fails, mark the model as raw/unstructured and tell the user instead of showing an empty grid.

- **MED — `bineditor_enso_parts.py:110-133 — `parse` has no bounds checks; truncated/malformed input raises `struct.error` out of `Editor.__init__`.**
  Problem: `struct.unpack_from("<16I", raw, 0)` and `raw[HEADER_SIZE:offsets[0]]` assume ≥64 bytes and valid offsets; `Editor.__init__` calls `parse` before any try/except, so the dialog never opens on bad input. Inconsistent with lamp's raw fallback.
  Fix: validate file length ≥ HEADER_SIZE, offsets monotonic and within EOF; raise a clear domain error or fall back to a raw view as lamp does.

- **MED — `bineditor_musicinfo.py:137, 142, 146, 150, 154 — section lengths use floor division (`//4`, `//8`, `//12`) with no clean-multiple check, silently dropping a trailing partial element.**
  Fix: assert `(o_next - o_prev) % elem_size == 0`; on mismatch fall back to a raw round-trip view rather than truncating.

- **MED — `bineditor_fname.py:60-65 (_decode_slot)` — decode does not stop at the `0x0000` terminator and `rstrip(PAD_CHAR)` will not remove an embedded NUL.**
  Problem: a slot like `[c0, 0x0000, c2, 0x3000, 0x0000]` decodes to a string containing `chr(0)`; if that slot is later edited, `_encode_slot` repositions characters incorrectly.
  Fix: truncate the decoded name at the first `0x0000` code unit before stripping padding.

### LOW

- **LOW — `bineditor_fname.py:65 — `rstrip(PAD_CHAR)` strips legitimate trailing U+3000.**
  Problem: a name intentionally ending in a full-width space displays without it; editing it permanently loses the trailing space. Mitigated because serialize only re-encodes changed slots (line 108 compares `_decode_slot(raw) == name`).
  Fix: document the limitation, or reconstruct exact length from the terminator position rather than rstrip.

- **LOW — `bineditor_lamp.py:288-293 — rebuild path repacks the pool from referenced names only and drops trailing pool padding past the final NUL.**
  Problem: if the original pool had alignment padding after the last string, a single name edit shrinks the file.
  Fix: capture and re-append any trailing pool bytes beyond the last referenced string.

- **LOW — `bineditor_rank.py:61-82, 120-127, 168-169 — header `count`/`stride` fields are descriptive-only; editing them in the GUI has no effect on body shape, which can mislead users.**
  Fix: mark those header cells read-only, or document that they are descriptive and do not reshape the grid.

- **LOW — `bineditor_enso_parts.py:357`, `bineditor_fname.py:187`, `bineditor_hdbdinfo.py:348`, `bineditor_streaminfo.py:235`, `bineditor_musicinfo.py:431 — broad `except Exception` in `_save` masks programming errors as generic "Edit error" dialogs.**
  Fix: narrow to `(ValueError, struct.error)` so genuine bugs surface instead of being swallowed.

- **LOW — `bineditor_enso_parts.py:305-316 — the read-only all-pool tab is built from `pool_parts` but edits flow only through the referenced-strings tab, so a string shown in both places won't refresh in the pool tab after editing.**
  Fix: refresh the pool tab on save, or note it is a static snapshot.

- **LOW — interface drift across the set — numeric parsing, malformed-input contract, and Qt import guarding are inconsistent.**
  Fix: introduce one shared `parse_int(text)` helper (`int(text, 0)` + u32/i32 range check), guard the PySide6 import in every module (as tuning/hdbdinfo do), and standardize one malformed-input contract (prefer lamp's raw-fallback for all, or raise a common typed error everywhere).

---

## What must change to reach 90+

1. Fix the offset-rebuild byte-loss (enso_parts CRITICAL, musicinfo CRITICAL): recompute header offsets from actual emitted section sizes **and** preserve every inter-section/pre-pool gap as a raw span, with a parse-time assert that modeled spans tile to EOF (fall back to raw otherwise).
2. Fix the encoding asymmetry (HIGH): use `latin1` for all string decode/encode, or preserve original bytes for unedited refs; never write `ascii/"replace"` into output.
3. Fix shared-pool-string capacity and aliasing (streaminfo HIGH, hdbdinfo HIGH/MED): derive capacity from the real terminator, detect shared pointers, and define edit precedence.
4. Make `parse` robustness uniform (enso/tuning/musicinfo MED): validate offsets/lengths and either raise a clear error or fall back to a raw view like lamp, so a malformed file never throws out of `Editor.__init__`.
5. Unify the interface (LOW, but it is graded): guard the Qt import everywhere, standardize numeric parsing, standardize the malformed-input contract.
6. Harden tuning's heuristics (MED): validate the discovered 180-block layout and warn/fallback instead of presenting an empty or garbled editable view.

The unedited round-trip is sound for all 8 on the sample files; the gap to 90 is the edit path (byte loss / string corruption / slot aliasing) plus uneven parse robustness and interface consistency.

## SCORE: 68

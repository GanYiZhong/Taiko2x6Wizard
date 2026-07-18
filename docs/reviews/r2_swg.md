SCORE: 91

# Re-review: SWG parser (`swg.py`) + editor (`swg_editor.py`)

Files re-read (read-only):
- `E:\Taiko No Tatsujin 8\swg.py`
- `E:\Taiko No Tatsujin 8\swg_editor.py`

Prior score was 52/100. Every finding from the previous pass has been addressed, and the
revised code has no remaining correctness bug, cannot corrupt adjacent data on any exposed edit
path, and has reasonable robustness/error handling. That clears the 90 bar.

## Verification of prior findings — all resolved

- **C1 (capacity over-counting) — FIXED.** `_extract_strings` now sets `capacity=(j-i)+1`
  (`swg.py:160`): the string's own bytes plus exactly one terminating null. Trailing padding
  past the terminator is no longer absorbed, so a following little-endian field beginning with
  `0x00` can never be clobbered. Round-trip byte-exactness holds.
- **C2 (in-place length change without pointer rewrite) — FIXED.** `set_string` (`swg.py:259-288`)
  refuses any edit where `len(b) != old_len` (`old_len = capacity - 1`), writes only within the
  proven slot, and restores the terminator. Pointer-delimited strings can no longer be grown or
  shrunk, so referencing pointer tables can never go stale.
- **H1 (false-positive matrices editable) — FIXED.** Scanned matrices carry `verified=False`
  (`swg.py:182`). The editor renders their cells read-only and greyed (`swg_editor.py:161-172`),
  and `_save` skips every unverified row (`swg_editor.py:281-283`). Because the scanner never
  emits a verified matrix, no blind-scan false positive is ever writable — a sound conservative
  default.
- **H2 (scan anchor `& ~0xF` → 0x40 in header) — FIXED.** `_extract_matrices` now starts at
  `o = BODY_OFFSET` (0x48) and strides 4 bytes on a miss (`swg.py:178-186`); it no longer reads
  header bytes and no longer skips 16-byte-misaligned blocks.
- **H3 (lossy name round-trip / tail zeroing) — FIXED.** `name_raw` is captured at parse
  (`swg.py:129,133`); `set_name` only rewrites on a real change, validates ASCII and
  `len < NAME_SIZE`, sets a single terminator, and preserves the original tail bytes beyond it
  (`swg.py:197-220`). No lossy `replace` value is fed back into a strict encoder because edits
  are gated on the user actually changing the field.
- **H4 (one bad cell aborts save; NaN/inf accepted) — FIXED.** `_parse_cell`
  (`swg_editor.py:242-260`) rejects empty/blank/non-numeric/NaN/inf per cell and never raises;
  `_save` collects per-field errors and isolates the offending field only
  (`swg_editor.py:262-323`). `set_float`/`set_matrix_translation`/`set_matrix_scale` independently
  reject non-finite values (`swg.py:228-248`).
- **M (bounds checks) — FIXED.** `_check_bounds` guards `set_float`, `set_u32`,
  `set_matrix_*`, and `set_string` (`swg.py:222-225` and call sites).
- **M (resolution gating) — ADDRESSED.** `set_resolution` clamps to `RES_MIN..RES_MAX`
  (`swg.py:189-195`); the read path is non-destructive.
- **L (truncation) — FIXED.** `parse` checks `len(data) < RES_OFFSET + 4` and raises a clear
  `SwgError` (`swg.py:124-126`).
- **L (per-call `import math`) — FIXED.** Now a module-level import (`swg.py:42`).
- **L (texture unpack) — FIXED.** Both the pixmap collection and the strip loop tolerate a
  malformed texture list via `try/except (TypeError, ValueError)` (`swg_editor.py:119-124`,
  `206-210`).
- **Typed errors — ADDED.** New `SwgError(ValueError)` (`swg.py:59-60`) is raised consistently.

## Safety of exposed edit paths (all confirmed non-corrupting)

- **Strings:** length-locked and slot-accurate → an edit writes exactly the original byte extent;
  cannot reach a neighbouring field or a later string. The previous ascending-order overlap
  concern is dissolved by the slot-accurate capacity.
- **Matrices:** never editable from the UI (all unverified); direct API calls are bounds- and
  finite-checked.
- **Name:** preserves tail, validates charset/length, single terminator.
- **Resolution / floats / u32:** range- and bounds-validated.

## Residual limitations (capability gaps, NOT correctness bugs)

These are correctly documented and defended by refusal, so they cost only a few points; they do
not corrupt data:

- The `_SYM_` symbol section and the `0x48` object graph are still not parsed. Consequence:
  matrices are never editable and string length changes are always refused. This is a deliberate,
  safe capability ceiling — the editor does less, but nothing it does is wrong.
- Matrix discovery remains a heuristic scan; because results are display-only, false
  positives/negatives are cosmetic (they affect which markers/rows appear, never the bytes).
- Minor: `_save`'s error dialog leaves earlier successful edits applied to the in-memory `SwgFile`
  while writing nothing to the archive. This is stated in the message and is not corrupting (the
  in-memory object stays internally consistent: e.g. `set_name` updates both `raw` and `name`), but
  a future refactor could stage edits and commit atomically for cleaner semantics.

## Why 91 and not higher

No correctness defects and safe edits satisfy the 90 threshold. It is not higher because the
format's authoritative structure (`_SYM_` / object graph) is still un-parsed, so a class of
legitimate edits (string resize, matrix editing) is unavailable rather than supported — the tool
is correct and safe but intentionally narrow. Reaching the mid-90s would require parsing that
structure so those edits become possible *with* pointer/count maintenance, plus atomic
staged-commit save semantics.

# Code Review: SWG parser (`swg.py`) and editor (`swg_editor.py`)

**Files reviewed (read-only):**
- `E:\Taiko No Tatsujin 8\swg.py` — SWG (Namco 2D scene/layout) container parser
- `E:\Taiko No Tatsujin 8\swg_editor.py` — PySide6 editor dialog

**SCORE: 52/100**

90 = no correctness bugs + reasonable robustness. This sits well below the bar because the
parser never implements the structure its own docstring documents (`_SYM_` pointer table +
`0x48` object graph). It uses blind byte/float scans instead, and several of those scans
silently corrupt the container when an edit is applied. "Byte-exact unless changed" holds
only for *untouched* files; any string-length change or any false-positive matrix edit
destroys adjacent data.

---

## CRITICAL

- **CRITICAL — `swg.py:107-124` (`_extract_strings`) — `capacity` over-counting corrupts adjacent fields.**
  Capacity is computed by walking trailing `0x00` bytes until the next non-null byte
  (`while k < n and raw[k] == 0: k += 1`). Binary fields that follow a string and begin with
  `0x00` (e.g. a little-endian float/int/offset whose high bytes are zero) get absorbed into
  the string's `capacity`. `set_string` (line 187) then writes `b + b"\0" * (capacity - len(b))`
  across that whole range, zeroing bytes that belong to the *next* field/offset-table entry.
  Round-trip byte-exactness breaks the moment a string is grown, and following data is silently
  destroyed.
  **Fix:** Do not derive capacity from "however many nulls happen to follow." Parse the
  `_SYM_` symbol table to obtain each string's true slot length. Until that exists, clamp edits
  to `len(new_bytes) + 1 <= original_terminator_run` where `original_terminator_run` is only the
  null bytes that provably belong to this slot (never spanning into a following non-padding
  field), and never write past the original string extent `[offset, j]` plus that proven run.

- **CRITICAL — `swg.py:178-188` (`set_string`) — in-place length change without updating pointer tables.**
  The module docstring (lines 7-9, 23-24) states strings are referenced by pointer tables and the
  `_SYM_` section stores `(string_ptr_table, count)`. `set_string` permits any `text` whose bytes
  fit `capacity`, including lengths different from the original, without touching those pointer
  tables or `_SYM_` counts. A shortened/lengthened string leaves the pointer table referencing
  stale offsets/extents → structurally invalid file even when the byte write itself "fits".
  **Fix:** Parse `_SYM_` and the `0x48` object-graph pointer tables. Only permit edits to strings
  whose slot is a known fixed-width field; for pointer-delimited strings, either forbid
  length changes (require `len(new) == len(old)`) or rewrite the referencing pointers/counts in
  the same operation.

---

## HIGH

- **HIGH — `swg.py:194-203` (`_looks_like_matrix`) — false-positive matrix detection; editing one corrupts the file.**
  The predicate accepts *any* 16 floats with `|v| <= 1e6`, ≥3 diagonal entries within `1e-6` of
  `1.0`, and ≥4 nonzero entries. Unrelated float arrays (vertices, color tables, animation
  keyframes) routinely satisfy this. The editor exposes every match as an editable transform;
  writing pos/scale into a false positive (via `set_matrix_translation`/`set_matrix_scale`)
  corrupts non-matrix data with no guard. Conversely, a genuine matrix with scale ≠ 1 and fewer
  than 3 diagonal ones is missed.
  **Fix:** Derive matrices by following the parsed object graph from the root object at `0x48`
  (the `(offset, count)` pointer pairs), not a blind float scan. If graph parsing is not yet
  available, mark scanned matrices as "unverified" and disable editing for them.

- **HIGH — `swg.py:136-144` (`_extract_matrices`) — scan anchor/alignment is wrong.**
  `o = BODY_OFFSET & ~0xF` evaluates to `0x40`, which is *before* `BODY_OFFSET` (`0x48`) and
  inside the name/header region — so the scan can match header bytes. On a non-match it advances
  by 16 bytes, so a real 64-byte matrix not aligned to this particular 16-byte lattice is never
  found. The `& ~0xF` silently drops the intended 8-byte start offset.
  **Fix:** Anchor the search to a correct, intentional offset (ideally from the object graph).
  If a linear scan is kept, start at `BODY_OFFSET` (not `& ~0xF`) and document the alignment
  assumption; do not begin inside the header.

- **HIGH — `swg.py:92` + `swg.py:151-156` — lossy name round-trip / strict re-encode mismatch.**
  Parse uses `.decode("ascii", "replace")`, so any byte ≥ `0x80` becomes U+FFFD. The editor
  pre-fills the name field with this lossy value. If the user edits it, `set_name` does
  `name.encode("ascii")` (line 152) which raises on U+FFFD or any non-ASCII char. Also
  `split(b"\0")[0]` (line 92) drops anything after the first null inside the 64-byte field, and
  `set_name` zero-fills all 64 bytes (line 155), destroying any legitimate trailing slot data.
  The parsed `name` is therefore not guaranteed faithful to the file.
  **Fix:** Preserve the original raw name bytes; only rewrite the name slot when the user actually
  changes it, validate input is ASCII and `< NAME_SIZE` before encoding, and surface a clear error
  if non-ASCII is entered rather than relying on a raised exception. Do not zero bytes beyond the
  string's own null run unless the full 64-byte field is confirmed to be name-only.

- **HIGH — `swg_editor.py:224-240` (`_save`) — one bad matrix cell aborts the whole save; NaN/inf accepted.**
  `float(self.tbl_mat.item(r, c).text())` is called per matrix cell with no per-cell guard. A
  blank or non-numeric cell raises `ValueError`, caught by the broad `except Exception` at line 238,
  which aborts the *entire* save (all other edits lost). Worse, `float("nan")`/`float("inf")`
  succeed and get written into the matrix via `set_matrix_*`, producing a corrupt transform.
  **Fix:** Validate each cell individually: parse to float, reject `nan`/`inf` (e.g.
  `math.isfinite`), and on failure highlight that specific cell and skip/refuse just that field
  rather than aborting all edits. Do not write non-finite values.

---

## MEDIUM

- **MED — `swg.py:93,147-149` + `swg_editor.py:130-131` — resolution offset assumed, not validated.**
  Parse reads `0x50` as `<HH` and `set_resolution` writes it; the editor allows 1–4096. The
  docstring hedges ("e.g. 640x480"). If `0x50` is not resolution for some file variant, the editor
  silently corrupts 4 bytes with no per-variant gating beyond the magic.
  **Fix:** Sanity-check the read values (plausible resolution range) and/or gate on `format_const`;
  surface uncertainty in the UI instead of unconditionally treating `0x50` as resolution.

- **MED — `swg.py:112` — strings shorter than 2 chars are dropped (`(j - i) >= 2`).**
  Single-character symbol/element names are neither tracked nor editable. If such a name is
  pointer-referenced, the editor presents an incomplete symbol set.
  **Fix:** Lower the threshold (carefully, to avoid matching stray printable bytes) once slot
  boundaries are known from `_SYM_`, or document the limitation explicitly.

- **MED — `swg_editor.py:234-237` + `swg.py:118` — ascending-offset edits compound the capacity bug.**
  `_save` edits strings in table (ascending offset) order. Because `capacity` (C1) can over-count
  into a later string's bytes, an earlier `set_string` write can clobber a later string's slot
  before that later row is processed, so the later edit operates on already-corrupted bytes.
  **Fix:** Resolved automatically once capacity is slot-accurate (see CRITICAL C1). Until then,
  detect and reject overlapping `[offset, offset+capacity)` ranges before writing.

- **MED — `swg.py:158,175-176` — `set_float`/`set_u32`/`set_matrix_*` lack bounds checks.**
  Offsets currently come from parse (and the matrix scan guarantees `o + 64 <= n`), so live risk is
  low, but a malformed/edited offset passed in would raise an opaque `struct.error` mid-API.
  **Fix:** Validate `offset + size <= len(self.raw)` and raise a clear, typed error.

## LOW

- **LOW — `swg.py:40,87-93` — `is_swg` checks `len >= 8` but `parse` reads through `0x53` (84 bytes).**
  A valid-magic file shorter than 84 bytes raises an opaque `struct.error` instead of a clear
  "truncated SWG" message.
  **Fix:** In `parse`, after the magic check, assert `len(data) >= RES_OFFSET + 4` and raise a
  descriptive `ValueError` if not.

- **LOW — `swg.py:195 (`import math` inside `_looks_like_matrix`).**
  Per-call import inside a function invoked across a full-file scan loop.
  **Fix:** Move `import math` to module top.

- **LOW — `swg_editor.py:114** — `self._tex_pixmaps = [pix for _name, pix in textures]` assumes every element is a 2-tuple.
  A malformed texture list raises in `__init__`.
  **Fix:** Defensive unpack / validate the texture list shape before constructing.

- **LOW — `swg.py` (whole module) — documented `_SYM_` section is never parsed.**
  Lines 23-24 describe `_SYM_` as the authoritative symbol/pointer table, but nothing parses it;
  string extraction is a blind body scan. This is the root cause of CRITICAL C1/C2 and HIGH H1/H2.
  **Fix:** Implement `_SYM_` parsing (locate the `_SYM_` marker, read the `(string_ptr_table, count)`
  groups, resolve pointers to strings with known extents) and base both string and matrix discovery
  on the real structure.

---

## What must change to reach 90+

1. **Parse the real structure** — the `_SYM_` symbol/pointer table and the `0x48` object graph of
   `(offset, count)` pairs — instead of blind byte/float scans. This eliminates C1, C2, H1, H2, and
   the L4 root cause at once, because strings and matrices then have *known* slot lengths and
   provenance.
2. **Make string edits slot-length-aware** (C1, C2): never absorb following-field nulls into
   capacity; reject edits that exceed the true slot; update pointer tables/counts when length
   changes are allowed, or forbid length changes for pointer-delimited strings.
3. **Gate matrix editing behind graph-verified transforms** (H1): never expose blind-scan false
   positives as editable.
4. **Per-cell validation in the editor save path** (H4): reject NaN/inf, fail the offending field
   only, never abort all edits or write non-finite values.
5. **Faithful name round-trip + truncation guards** (H3, L2): no lossy `replace` feeding back into a
   strict encoder; validate length/charset; add a minimum-length check in `parse`.

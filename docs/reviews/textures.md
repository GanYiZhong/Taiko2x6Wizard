# Code Review: TIM2 texture decode/encode and song-name generators

**Files reviewed (read-only):**
- `E:\Taiko No Tatsujin 8\tim2.py` — TIM2 (.nut PS2 texture) decode/encode
- `E:\Taiko No Tatsujin 8\songtex.py` — kenri song-name texture generator
- `E:\Taiko No Tatsujin 8\songtex_all.py` — per-song full texture-set generator

**SCORE: 84 / 100**

## Verdict

The core decode↔encode round-trip is **correct and bug-free** for the intended
4-bit-indexed template workflow:

- Nibble order is consistent between decode (`tim2.py:91-93`) and encode
  (`tim2.py:232`) — low nibble = even pixel, high nibble = odd pixel — so it
  round-trips.
- PS2 alpha scaling (0..128 ↔ 0..255) is symmetric between `_expand_alpha`
  (`tim2.py:24-26`) and `rgba_to_indexed4_white` (`songtex.py:80-85`).
- RGBA5551 expansion (`tim2.py:29-36`) is correct.
- Strict `image_size` / `clut_size` equality checks (`tim2.py:240-243`) prevent
  silent corruption of the template when the packed sizes don't match.
- The header / GS registers / sizes are preserved verbatim, so the spliced
  output stays a valid `.nut` the game accepts.

Points deducted are **robustness and error-handling gaps**, not happy-path
correctness bugs.

---

## Findings

### tim2.py

- **HIGH — tim2.py:97-98 — CSM1 unswizzle gate doesn't distinguish CSM1 vs CSM2.**
  The gate `if clut_colors >= 256 and not (clut_type & 0x80):` decides whether to
  call `_unswizzle_clut256`. It keys only off colour count and the high "linear"
  bit (0x80). A 256-colour CSM2 palette would be wrongly unswizzled, producing
  corrupted colours. CSM2 palettes must never be unswizzled.
  **Fix:** Gate on an explicit CSM-type check. CSM1 is the low subfield of the
  CLUT-type byte (`clut_type & 0x07 == 1` for CSM1; CSM2 is a different value).
  Only call `_unswizzle_clut256` when the CSM type is CSM1 *and* the palette is
  stored swizzled (high "linear" bit clear). E.g.:
  ```python
  csm = clut_type & 0x07
  swizzled = not (clut_type & 0x80)
  if clut_colors >= 256 and csm == 1 and swizzled:
      pal = _unswizzle_clut256(pal)
  ```

- **MED — tim2.py:131-138 — no bounds validation of header_size / image_size /
  clut_size against the file length.** Python slicing never raises, so a
  malformed/hostile nut with a bogus `header_size` (< 0x30), an oversized
  `image_size`, or `img_start + image_size + clut_size > len(data)` yields a
  silently truncated `img`/`clut` that `_decode_pixels` then zero-pads — a
  wrong-but-no-error decode that masks corruption.
  **Fix:** After unpacking the per-picture header, validate and raise:
  ```python
  if header_size < 0x30 or image_size < 0 or clut_size < 0:
      raise ValueError(f"invalid TIM2 picture header at off {off}")
  if img_start + image_size + clut_size > len(data):
      raise ValueError("TIM2 picture extends past end of file")
  ```

- **MED — tim2.py:114-115 and 69-71 — silent zero-padding of short pixel/palette
  data.** `_decode_pixels` (`rgba.shape[0] < n` → `np.vstack` with zeros) and
  `_read_clut` (`pal.shape[0] < colors` → zero-pad) tolerate truncated input by
  filling zeros. Combined with the missing bounds checks above, real corruption
  is hidden behind a "successful" decode.
  **Fix:** Once the bounds validation above is in place, make these pad branches
  raise instead of silently zero-filling (or at minimum emit a warning), since a
  short buffer past validation indicates genuine corruption.

- **MED — tim2.py:150-159 — `tim2_summary` unpacks header fields with no length
  guard.** It reads `data[5]`, `struct.unpack_from("<H", data, 6)`,
  `struct.unpack_from("<III", data, base)`, `data[base + 0x12]`, etc., for
  `base` up to 0x80. On a truncated file this raises a raw `struct.error` /
  `IndexError` rather than a clean, catchable error.
  **Fix:** Guard length first:
  ```python
  if not is_tim2(data) or len(data) < base + 0x18:
      raise ValueError("truncated TIM2 header")
  ```
  (compute `base` before the length check).

- **MED — tim2.py:142-144 — multi-picture stride trusts `total_size` blindly.**
  The loop advances `off += total_size` with no check that
  `total_size >= header_size + image_size + clut_size`. A too-small `total_size`
  desyncs subsequent picture offsets (overlap); a value past EOF just breaks the
  loop. TIM2 in practice is well-formed, but there's no validation.
  **Fix:** Validate `total_size >= header_size + image_size + clut_size` before
  using it as the stride; raise or break with a clear error otherwise.

- **LOW — tim2.py:128 — `max(1, pic_count)` masks a declared count of 0.** A
  header declaring 0 pictures still decodes one. Harmless in practice (real TIM2
  always has ≥1) but it's a silent assumption.
  **Fix:** If `pic_count == 0`, raise `ValueError("TIM2 declares 0 pictures")`
  rather than forcing one.

- **LOW — tim2.py:99 — `np.clip(idx, 0, pal.shape[0]-1)` silently remaps
  out-of-range indices.** Acceptable for display robustness; noting it because it
  hides palette/index mismatches. No change required if display-tolerance is the
  intent.

- **LOW — tim2.py:78 — `_unswizzle_clut256` loop bound `range(0, n-23, 32)`
  leaves a trailing partial block (entries 8..23) swizzled for palettes whose
  size isn't a multiple of 32.** For the standard 256-entry case this is correct
  (blocks at 0,32,…,224). Only non-standard sizes are affected.
  **Fix (defensive):** iterate `range(0, pal.shape[0], 32)` and guard each block
  so it only swaps when `i + 24 <= pal.shape[0]`.

### songtex.py

- **MED — songtex.py:54-69 — `render_kenri_rgba` has no width-fitting and no
  vertical-overflow guard.** Unlike `songtex_all._fit_font_to_width`, it draws at
  fixed `title_size`/`sub_size` with no shrink-to-fit, so a long title overflows
  the 640 px width and is silently clipped. Vertically, `y` starts at 4 and each
  line advances `asc + desc + 4`; title 30 + sub 23×3 can exceed 160 px, clipping
  the bottom line.
  **Fix:** Reuse a width-fit helper (port `_fit_font_to_width` from
  songtex_all.py, or import it) so each line shrinks to fit `WIDTH - 2*x`. Track
  cumulative `y` and shrink sub_size / reduce line gap if the total would exceed
  `HEIGHT`, so no line is clipped.

- **LOW — songtex.py:35-42 — `_font` falls back to `ImageFont.load_default()`,
  which cannot render CJK.** If `Font.ttf` and all system fonts are missing,
  Japanese text renders as blank/tofu silently rather than failing loudly.
  **Fix:** If no CJK-capable font is found, raise a clear error (or log a
  visible warning) instead of returning the bitmap default.

- **LOW — songtex.py:138, 153 — parameter named `copyright` shadows the
  builtin.** Cosmetic.
  **Fix:** Rename to `copyright_text` (also applies in songtex_all.py).

### songtex_all.py

- **MED — songtex_all.py:255-265 — vertical layout ignores per-glyph height
  exceeding the cell.** `cell = asc + desc`, but a glyph whose `gh > cell` (e.g.
  with combining marks) is centred via `gy = y + (cell - gh)//2 - t` and renders
  outside its cell, clipping at the canvas top/bottom.
  **Fix:** When choosing `size` (the `while size > 8` loop at 232-247), also
  require `max_glyph_height <= cell` (or track the true max glyph bbox height and
  use that as the cell advance instead of `asc + desc`).

- **MED — songtex_all.py:97-102 — `_text_size` uses `textbbox` width which can
  exclude right-side bearing for brush fonts**, so `_fit_font_to_width` may
  slightly under-estimate and let glyphs touch/overrun the right edge.
  **Fix:** Add a small safety margin to the fit (`if w <= max_w - pad`), or use
  the font's advance width (`font.getlength(text)`) rather than the ink bbox for
  the width test.

- **LOW — songtex_all.py:35 — `import os` is unused.**
  **Fix:** Remove the import.

- **LOW — songtex_all.py:328-329 — `generate_song_textures` silently skips falsy
  templates (`not tpl`).** Empty-bytes templates are dropped with no warning.
  **Fix:** Acceptable, but consider logging skipped types so a missing template
  is visible to the caller.

- **NOTE (not a bug) — songtex_all.py:316-317 — regenerating a fresh linear
  16-entry palette is correct** because 16-colour 4-bit CLUTs are not subject to
  the 256-entry CSM1 swizzle. Worth a one-line comment to prevent a future "fix"
  from wrongly applying swizzle here.

---

## What must change to reach 90+

1. **tim2.py:** add header/section bounds validation (header_size, image_size,
   clut_size vs `len(data)`) and raise on gross violation instead of silently
   zero-padding (lines 131-138, 114-115, 69-71); length-guard `tim2_summary`
   (150-159).
2. **tim2.py:97-98:** fix the CSM1 unswizzle gate to test the CSM-type subfield
   explicitly so CSM2 256-colour palettes are never unswizzled.
3. **songtex.py:** add width-fit and vertical-overflow guards to
   `render_kenri_rgba` so long titles / 4 tall lines don't clip.
4. **songtex.py:** make the missing-CJK-font case a hard error or explicit
   warning instead of silent tofu.
5. **songtex_all.py:** guard the vertical layout against per-glyph height
   exceeding the cell; remove the dead `import os`.

Correctness is essentially clean for the intended scope; the gap holding the
score under 90 is silent handling of malformed input (tim2) and silently clipped
text (renderers).

**SCORE: 84**

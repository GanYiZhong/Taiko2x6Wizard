SCORE: 91

# Round-2 review — tim2.py, songtex.py, songtex_all.py

Re-scored after the earlier fixes. All previously suspect areas were verified empirically
(scratchpad round-trip harness). No correctness bugs remain in the core TIM2 decode/encode
or the texture-generation paths; robustness and error handling are now solid. Remaining
items are latent-only or cosmetic.

## Empirically verified correct
- **4-bit index pack/unpack round-trip, including odd pixel counts** — decoder (tim2.py
  L100-103: even=low nibble, odd=high nibble) and encoder (L278: `(odd<<4)|(even&0xF)`)
  agree; odd-width padding byte is dropped on decode. Round-trip exact.
- **RGBA5551 channel order** — R=bits0-4, G=5-9, B=10-14, A=bit15 matches PS2 A1B5G5R5.
  Pure-red/blue/alpha probes correct.
- **CSM1 256-colour unswizzle is a true involution** and leaves a non-multiple-of-32
  trailing block untouched (the `i+24 <= n` guard works as documented).
- **PS2 alpha expansion** (`min(255, a*2)`) maps 128→255 and saturates; encoder writes
  ps2a=128 for index 15, decoder expands back to 255. Alpha round-trip lossless at endpoints.
- **Header/section bounds validation** in `decode_tim2` (header_size>=0x30, non-negative
  sizes, img+clut within file, total_size >= sections) closes the silent zero-pad truncation
  hole; short buffers now raise instead of masking corruption.
- **CLUT truncation** raises with a precise message; `_clut_bpp` infers 2/3/4 from on-disk
  size when the format nibble is unknown.

## Findings (non-blocking)

### F1 (Low, latent) — encoder ignores template `clut_type`; only correct for RGBA32 palettes
`tim2.encode_indexed4_into_template` always builds a 4-byte-per-entry palette
(`pal[:clut_colors].tobytes()`, 64 bytes for 16 colours) and writes `clut[:clut_size]`
into the template. It checks `image_type == 4` but never checks `clut_type`. If a template's
CLUT were 16-bit (clut_size=32) or 24-bit, the splice would write the first N bytes of the
4bpp buffer — a garbled palette — with no error. Verified: a 32-byte clut_size would get the
first 8 entries' RGBA rows, not 16 RGBA5551 entries. Harmless in practice because every real
Taiko song-name nut is RGBA32/16-colour (clut_size=64), and the songtex/ songtex_all callers
only ever build the matching white-alpha palette. Worth a one-line guard:
`if lay["clut_size"] != lay["clut_colors"] * 4: raise ...` (or assert clut_type low-nibble==3)
to fail loudly on an unexpected template.

### F2 (Low, latent) — `first_picture_layout` does not validate `header_size`
Unlike `decode_tim2`, `first_picture_layout` (tim2.py L237) computes
`img_start = base + header_size` with no `header_size >= 0x30` / bounds check. A malformed
template with a tiny or huge header_size would produce an img_start pointing into the header
or past EOF, and the encoder's `out[img_start:...]` slice assignment would silently mis-splice
(slice assignment on bytearray grows/relocates rather than erroring cleanly). Templates are
trusted real nuts, so this is latent, but a shared validation helper would make both paths
consistent.

### F3 (Cosmetic) — `_render_kenri` in songtex_all.py has no height-overflow guard
`songtex.render_kenri_rgba` shrinks the inter-line gap (4→0) so four lines never overflow
the 160 px plate (L94-99). The parallel `_render_kenri` in songtex_all.py (L189-214) uses a
fixed `y += asc+desc+2` with no such guard, so an unusually tall font/size combination could
clip the last line. Not a correctness bug (text still renders; only overflow clips), and the
two modules are documented as sharing the encode path — but the render path diverges here.
Consider factoring the gap-shrink logic so both plates behave identically.

### F4 (Nit) — silent `except Exception: pass` in live-preview refresh
`songtex.SongTexDialog._refresh` (L227) and `songtex_all.SongTextureDialog._refresh` (L504)
swallow all exceptions to keep the live preview responsive while typing. Reasonable for the
preview, and the `_save` paths correctly surface errors via QMessageBox, so this is only a
debuggability nit, not a defect.

## Robustness coverage summary
- Unusual image_types: 1/2/3/4/5 all handled; unknown types raise. Good.
- CLUT formats: 16/24/32-bit + size-inference fallback. Good.
- Multi-picture: iterated with per-picture bounds + stride (total_size) sanity; `total_size==0`
  terminates. Good. (Mipmaps are not parsed, but TIM2 mipmap_count lives in the GS-register
  area past 0x30 and these song textures have none — acceptable scope.)
- Malformed input: truncated header, truncated CLUT, oversized sections, 0-picture count,
  short pixel buffer all raise with clear messages.

Deduction from 90-baseline is +1 (no correctness bugs, strong robustness/error handling),
held back from higher by the two latent template-trust gaps (F1/F2) that a defensive decoder
would guard.

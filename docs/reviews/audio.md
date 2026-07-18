# Audio Pipeline Code Review

Files reviewed (read-only): `vagtool.py`, `audioplayer.py`, `hdbd.py`
Scope: PS2 VAG ADPCM decode/encode (custom interleaved-stereo container with
interleave_last final chunk; predictor history clamped to int16), the PySide6
audio player (QMediaPlayer + temp WAV, WAV/VAG export), and the Sony SCEI HD/BD
sound-bank parser.

**SCORE: 74/100**

The ADPCM math is correct: the per-sample int16 history clamp (the most
commonly-missed VAG bug) is present, interleave_last de-interleaving matches the
game, and mono/stereo + sample-rate handling are right. What holds the score
below 90 is **robustness**, not correctness — on-disk sizes are trusted without
bounds checks, the temp-WAV lifecycle leaks/locks on Windows, and full-length
encode blocks the GUI thread.

---

## CRITICAL

### C1 — vagtool.py:205,228 — `data_size`/`per_channel` trusted absolutely; truncated VAG silently decodes padding as audio
`per_channel = data_size // channels` comes straight from the header with no
check that `len(payload) >= data_size`. On a truncated file, `payload[pos:pos+interleave]`
returns short/empty slices and `raw[:per_channel]` includes zero-padding blocks
as if they were ADPCM, producing silent corruption rather than an error.
**Fix:** before de-interleaving, compute `avail = len(payload)`, and clamp:
`data_size = min(data_size, avail)`; recompute `per_channel`, `full`, `last`
from the clamped value. If `data_size > avail`, raise `ValueError("VAG payload truncated: header says N bytes, file has M")` or at minimum warn.

### C2 — vagtool.py:198; audioplayer.py:30; hdbd.py:101,104,110 — `struct.unpack`/`unpack_from` with no length guard
`decode_vag` does `struct.unpack('<4I', data[:HEADER_SIZE])` with no check that
`len(data) >= 16` → opaque `struct.error` on short blobs. `hdbd.parse_hd` guards
the *first* table read (line 92) but then reads `vagi+8` (101), `vagi+0x10`
(104), and `vagi+p` (110) with no bounds check that those offsets + `n_entries*4`
fall inside `hd`.
**Fix:** add a helper `def _need(buf, off, span, what): if off < 0 or off + span > len(buf): raise ValueError(f"{what}: need {span} bytes at {off}, buffer is {len(buf)}")` and call it before every unpack. In `decode_vag`, early `if len(data) < HEADER_SIZE: raise ValueError(...)`.

---

## HIGH

### H1 — hdbd.py:104,123,145 — waveform table fully trusted; `bd_offset`/size can be out of range, negative, or reversed
`size = end - bd_off` goes negative if Vagi entries aren't monotonic (the
docstring only asserts *section* offsets are largest-first, not Vagi entries). A
negative size makes `bd[start:start+size]` empty/reversed; an out-of-range
`bd_offset` silently yields empty PCM. The self-test checks these invariants but
the production `parse_hd`/`decode_waveform` path does not.
**Fix:** in `decode_waveform`, clamp: `start = max(0, min(start, len(bd)))`,
`size = max(0, min(size, len(bd) - start))`; if `size == 0` return
`(wf["sample_rate"], np.zeros(0, np.int16))`. In `parse_hd`, drop or flag entries
where `end < bd_off`.

### H2 — vagtool.py:373,487 — full-length encode runs synchronously on the Qt GUI thread
`_on_save` calls `convert_audio_file` directly in the dialog slot. Encode is pure
Python, O(F·28·65): a ~200s song is ~315k frames × 65 candidates × 28 steps and
freezes the UI for a long time; the `status_label.repaint()` is a band-aid. The
self-test deliberately slices to 8s, confirming full-length encode is
impractical synchronously.
**Fix:** move `convert_audio_file` to a `QThread`/`QRunnable` worker with a
progress signal (emit per N frames from `_encode_channel`) and a cancel flag;
disable Save while running and re-enable on finish. At minimum, document the
expected wall-clock time.

### H3 — audioplayer.py:113–117,183–189 — temp-WAV cleanup swallows Windows file-lock errors; previous temp leaks
`load_pcm` does `setSource(QUrl())` then `_cleanup()`, but on Windows
QMediaPlayer may not release the prior handle synchronously, so `os.remove`
raises `OSError`, is swallowed (188), and temp files accumulate in `%TEMP%`
across every load.
**Fix:** maintain a `self._pending_delete: list[str]`; on `_cleanup` failure,
append the path and retry-sweep it at the start of the next `load_pcm` and on
teardown. Or use `QTemporaryFile`/`QMediaPlayer.setSourceDevice` to avoid the
on-disk handle entirely.

### H4 — audioplayer.py:54,183,227 — `AudioPlayer` never deletes its last temp WAV on teardown
`_cleanup` is only reachable from `load_pcm`. On widget/dialog destruction the
final `self._tmp` leaks. `SoundBankDialog.closeEvent` (227) calls
`self.player.stop()` but not cleanup.
**Fix:** add `def closeEvent(self, ev): self._cleanup(); super().closeEvent(ev)`
to `AudioPlayer` (and/or connect `self.destroyed` to a cleanup), and have
`SoundBankDialog.closeEvent` call `self.player._cleanup()` after `stop()`.

---

## MEDIUM

### M1 — vagtool.py:89 — `(nib << 12) >> shift` is not normalized for `shift > 12`
Canonical PS2 decode is `s = (i16)(nibble << (12 - shift))`. For `shift <= 12`
the two are numerically equal, and `CAND_SHIFTS` caps the encoder at 12, but real
VAG frames can carry `shift` in 0..15. Hardware/vgmstream treats `shift > 12` as
a defined case (vgmstream forces `shift = 9` when `> 12`); here `>> shift` just
loses precision instead of matching hardware.
**Fix:** `shift = np.where(shift > 12, 9, shift)` before computing `scaled`, to
match vgmstream. Low practical impact for this game's files but not decoder-exact
without it.

### M2 — vagtool.py:106–118 — history feeds back unrounded clamped float, not the rounded int16
The IIR clamps `val` to ±32767/−32768 as float (correct per spec) but only
applies `np.round` at the very end (118). A bit-exact integer VAG decoder feeds
the *rounded* int16 back as history, so this decoder drifts sub-LSB from
vgmstream. Round-trip with this file's own encoder stays self-consistent (encoder
uses the same float history), so correlation is unaffected.
**Fix (only if vgmstream-exact output is required):** inside the loop do
`val = float(round(val))` after the clamp, and store/feed that integer as
`hist1`.

### M3 — vagtool.py:199–202 — silent header "repair" can mask corruption and allow a huge allocation
`if interleave == 0: interleave = INTERLEAVE` and `if channels <= 0: channels = 2`
paper over bad headers without warning. A bogus `channels` (e.g. 0x20202020)
isn't `<= 0`, so it passes through and builds `chan_bytes`/`pcm` of width
~539M → MemoryError.
**Fix:** validate `if channels not in (1, 2): raise ValueError(...)` and
`if not (8000 <= sample_rate <= 48000): raise ValueError(...)` before allocating.

### M4 — audioplayer.py:31 vs vagtool.py:199 — `is_vag` and `decode_vag` disagree on `interleave == 0`
`is_vag` requires `interleave == 0x8000`, but `decode_vag` accepts `interleave == 0`
as a fallback. A file `decode_vag` would happily decode is classified not-VAG.
**Fix:** align the heuristic — accept `interleave in (0, 0x8000)` in `is_vag`, or
drop the `== 0` fallback in `decode_vag`. Pick one model and apply it both places.

### M5 — vagtool.py:255–256 — dead/contradictory code
`per_channel = max(len(e) for e in enc)` is immediately overwritten by
`per_channel = len(enc[0])`. The `max` line is dead; if channel lengths ever
differed (they can't here) line 256 would silently truncate.
**Fix:** delete line 255; optionally `assert len({len(e) for e in enc}) == 1`.

### M6 — hdbd.py:61–75 — `_find_vagi` never validates `head_sz`/`hd_size`; fallback scan can false-match
The fallback `hd.find(b"IECSigaV")` could match inside data embedded in a
malformed HD; `head_sz == 0x40` is documented but unchecked.
**Fix:** sanity-check `head_sz == 0x40` and `hd_size <= len(hd)` at the top of
`parse_hd`; reject the fallback match if its tag/count fields are implausible.

---

## LOW

### L1 — vagtool.py:356 — generic non-wav/ogg branch doesn't `int()` the rate
`_load_ogg` casts librosa's rate but the generic soundfile branch (356) leaves
`rate` as whatever sf returns. Harmless (sf returns int) but inconsistent.
**Fix:** `rate = int(rate)` after every loader.

### L2 — vagtool.py:365 — mono→stereo via `np.repeat` then per-channel resample does 2× work on identical data
**Fix:** resample mono first, then duplicate to stereo (cosmetic perf only).

### L3 — audioplayer.py:119 — `pcm.ndim` used as truthiness; `else` branch unreachable
`ndim` is always ≥ 1, so `len(pcm)` is dead.
**Fix:** use `pcm.shape[0]` unconditionally.

### L4 — audioplayer.py:212 — double-click handler plays without ensuring the row is loaded
`itemDoubleClicked` → `self.player.player.play()` relies on `currentRowChanged`
having already fired. Double-click before a selection change plays stale/no PCM.
**Fix:** in the double-click lambda, call `self._load(self.lst.currentRow())`
first, then play.

### L5 — vagtool.py:267 — writes exactly 8 × 0xFF then zero-fill; "matches real files" is unverified by the self-test
The self-test header check only compares `interleave` (`e_ib == o_ib`), not the
padding bytes, so a regression in the FF-run length wouldn't be caught. Cosmetic
for playback.
**Fix:** if byte-exact container output matters, compare the full first-block
padding against a reference in the self-test.

### L6 — hdbd.py:128–131 — `loop_start`/`loop_end`/`name` always `None`
Documented and intentional (names live in tone/prog tables; loops are per-frame
flags). `list_bank` surfaces `name=None`, UI falls back to `wave NNN`. No fix
needed — noted for completeness.

---

## What must change to reach 90+

1. **Bounds-check every on-disk-driven unpack/slice** (C1, C2, H1, M3): validate
   header/table offsets and sizes against actual buffer length, reject
   negative/oversized values with `ValueError`, before any allocation or decode.
   This is the single biggest gap between "works on the 9 known-good banks" and
   "production-robust."
2. **Fix the temp-WAV lifecycle** (H3, H4): guarantee cleanup on teardown and
   tolerate Windows file locks.
3. **Move full-length encode off the GUI thread** (H2).

The MEDIUM decode-exactness items (M1, M2) are quality-of-decode notes that only
matter for vgmstream-identical output; they do not break playback.

**Strengths:** correct per-sample int16 history clamp; correct, well-justified
interleave_last de-interleave; decoder-matched brute-force encoder giving stable
round-trips; gracefully optional ogg support; frame-trimming guards against
ragged ADPCM tails.

**SCORE: 74/100**

SCORE: 92

# Round-2 Audio Review — vagtool.py, audioplayer.py, hdbd.py

Scope: VAG ADPCM decode/encode, audio player temp-WAV lifecycle, HD/BD bank
parsing. Re-scored after the fixes from round 1. All earlier correctness
findings are resolved; remaining items are minor robustness/edge polish.

## Verdict
No correctness bugs found in the audio pipeline. Decode/encode are
decoder-exact and round-trip stable; playback path is clean with correct temp
file cleanup; HD/BD parsing is well bounded. Score reflects a couple of small
robustness gaps that are not defects but could be tightened.

## Correctness (resolved / verified)

- ADPCM coefficients: `VAG_F0`/`VAG_F1` are the standard PS2 sets scaled by
  1/64. Correct.
- Predictor/shift: `predictor = clip(ctrl>>4, 0, 4)`; `shift = ctrl & 0x0F`
  with `shift>12 -> 9` (vgmstream-consistent, only affects out-of-spec input).
  Nibble sign-extension (`>7 -> -16`) and `scaled = (nib<<12)>>shift` are
  correct.
- History clamp order (the round-1 bug): decoder now does
  `val = round(...)` then clamp to `[-32768, 32767]` and feeds THAT back into
  `hist1/hist2` (vagtool.py:115-124). The encoder mirrors it exactly
  (vagtool.py:200-204: `np.round(val)` then `np.clip(val, -32768, 32767)`
  fed forward as `eh1`). Encoder state is bit-identical to the decoder, so
  round-trip correlation stays ~1.0. Correct and symmetric.
- flags==7 silence handling zeroes the frame and resets history — matches PS2.
- interleave_last (the other round-1 bug): decode (vagtool.py:255-266) and
  encode (vagtool.py:324-336) both emit `full = per_channel // interleave`
  round-robin blocks followed by a single `last = per_channel % interleave`
  partial chunk per channel. The two directions are symmetric, so songs whose
  per-channel size isn't a block multiple (e.g. music_xjapan) round-trip
  cleanly. Correct.
- Container truncation: `data_size` is clamped to the actually-present payload
  (`avail`), so zero padding is not decoded as audio; per-channel raw is
  trimmed to a FRAME_BYTES multiple, and `n = min(len(x) ...)` guards ragged
  channels. Header validation rejects `channels not in (1,2)` and out-of-range
  sample rate, blocking the huge-allocation vector. `interleave==0` tolerated,
  `interleave < FRAME_BYTES` rejected. Good.
- encode_vag channel-length assertion (vagtool.py:317) prevents silent
  truncation if channel encodes ever diverge.
- mono/stereo + rate: `convert_audio_file` resamples BEFORE mono->stereo
  duplication (avoids resampling identical channels), clips to int16, forces
  stereo. `_resample_linear` guards `m<=0`/`n==0`. Correct.
- HD/BD parsing: `_need` bounds-checks every read; `parse_hd` validates
  `head_sz == 0x40` and `hd_size <= len(hd)`; Vagi located via the HeadSCEI
  table with a validated linear-scan fallback (checks tag + entry-count fits).
  Waveform sizes are deltas clamped to `[0, bd_size]`; `decode_waveform`
  re-clamps `start`/`size` into `bd` and trims to a frame boundary, returning
  empty PCM for degenerate entries rather than a garbled slice. Correct.
- Temp-WAV cleanup: `_cleanup` removes the current temp file and, on a Windows
  `OSError` (handle still held after `setSource(QUrl())`), defers to
  `_pending_delete`; `_sweep_pending` retries on next load and on teardown.
  `load_pcm` releases the previous source before writing a new temp file.
  `closeEvent` (both AudioPlayer and SoundBankDialog) stops, releases, cleans,
  and sweeps. No temp-file leak in the normal lifecycle. Good.
- ogg-without-soundfile: `_load_ogg` falls back to librosa, and if neither is
  present raises a clear, actionable RuntimeError instead of crashing at
  import. Module import never hard-fails on the optional dep. Good.

## Robustness / edge notes (minor — did not lower below 90)

1. `_pending_delete` is only swept on the next `load_pcm`/close. If the app
   exits without another load, files Windows never released remain in %TEMP%.
   Acceptable (they are ordinary temp files the OS may reclaim), but a
   process-exit `atexit` sweep would fully close the leak. Low.

2. `_load_ogg`/generic soundfile path assumes `_sf.read` channel order matches
   playback; multichannel (>2) ogg is truncated to the first 2 channels
   (`pcm[:, :2]`) rather than downmixed. Fine for this game's stereo target,
   but a true downmix would preserve energy better. Cosmetic.

3. `decode_vag` accepts `sample_rate` up to 48000 and rejects outside
   `[8000, 48000]`. The `is_vag` heuristic in audioplayer.py mirrors the same
   bounds and `interleave in (0, 0x8000)`, so classification and decode stay in
   sync — good — but the two range checks are duplicated literals in separate
   modules and could drift on a future edit. Consider centralizing. Low.

4. `_encode_channel` brute-forces 5 predictors x 13 shifts per frame in pure
   Python; correct but slow on long clips. This is why encode runs off the GUI
   thread with progress/cancel — handled well. Not a bug, just a perf note.

5. SoundBankDialog reaches into `self.player._cleanup()` / `_sweep_pending()`
   (private members) in its own `closeEvent`. Works, but couples the dialog to
   AudioPlayer internals; a public `AudioPlayer.release()` would be cleaner.
   Style only.

## Why 92 (not higher)
Playback and DSP are correct and the round-1 defects are genuinely fixed. Held
just under the mid-90s by the residual temp-file-on-abrupt-exit gap (item 1)
and the duplicated validation bounds (item 3) — both robustness/maintainability
rather than correctness. Nothing here would produce wrong audio or a crash on
real game data.

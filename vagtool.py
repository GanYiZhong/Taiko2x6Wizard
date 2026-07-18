"""vagtool.py - Audio converter for Taiko no Tatsujin (SYSTEM256/PS2 arcade) BGM.

Handles the game's custom interleaved-stereo PS2-ADPCM "VAG" container.

Container layout (little-endian), confirmed by inspecting real files
(music_10tai, music_1ps, STR_DOJO_BGM in test/sound/stream):

    0x00 u32  interleaveBlock = 0x8000 (32768 bytes per channel block)
    0x04 u32  dataSize        (total ADPCM payload size summed over channels,
                               multiple of 16; per channel = dataSize/channels)
    0x08 u32  channels        (2 for songs)
    0x0C u32  sampleRate      (44100 for songs, 32000 for some system BGM)
    0x10 ..   padding (0xFF then 0x00) filling the first interleave block;
              audio payload begins at offset 0x8000.

Audio payload = standard PS2 VAG ADPCM per channel, interleaved in 0x8000-byte
blocks (block0=ch0, block1=ch1, block2=ch0, ...). The payload on disk is padded
with zero blocks up to a whole number of interleave blocks. PS2 VAG ADPCM:
16-byte frames, 28 samples each: byte0=(shift | predictor<<4), byte1=flags,
bytes2..15 = 14 bytes of 4-bit nibbles (low nibble first).
"""

import struct
import wave
import io

import numpy as np

# Optional ogg support (do not hard-fail module import if missing).
try:
    import soundfile as _sf  # type: ignore
    _HAVE_SF = True
except Exception:
    _HAVE_SF = False

try:
    import librosa as _librosa  # type: ignore
    _HAVE_LIBROSA = True
except Exception:
    _HAVE_LIBROSA = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
INTERLEAVE = 0x8000          # bytes per channel block
HEADER_SIZE = 0x10           # meaningful header fields
DATA_START = INTERLEAVE      # audio begins at the first full interleave block
FRAME_BYTES = 16             # ADPCM frame size
FRAME_SAMPLES = 28           # samples per frame

# Standard PS2 ADPCM predictor coefficients (scaled by 1/64).
VAG_F0 = [0.0, 60.0 / 64.0, 115.0 / 64.0, 98.0 / 64.0, 122.0 / 64.0]
VAG_F1 = [0.0, 0.0, -52.0 / 64.0, -55.0 / 64.0, -60.0 / 64.0]
_NUM_PREDICTORS = len(VAG_F0)


# ---------------------------------------------------------------------------
# ADPCM core (single channel)
# ---------------------------------------------------------------------------
def _decode_channel(adpcm: bytes) -> np.ndarray:
    """Decode one channel of PS2 VAG ADPCM bytes -> int16 samples.

    Vectorised per-frame nibble extraction; the IIR history recursion across
    samples is inherently sequential and runs per frame (28 steps).
    """
    n_frames = len(adpcm) // FRAME_BYTES
    if n_frames == 0:
        return np.zeros(0, dtype=np.int16)
    buf = np.frombuffer(adpcm, dtype=np.uint8).reshape(n_frames, FRAME_BYTES)

    ctrl = buf[:, 0].astype(np.int32)
    shift = ctrl & 0x0F
    # Hardware/vgmstream treats shift > 12 as a defined case (forced to 9)
    # rather than letting the right-shift silently lose precision. Real game
    # frames use shift <= 12, so this only matters for out-of-spec input.
    shift = np.where(shift > 12, 9, shift)
    predictor = np.clip(ctrl >> 4, 0, _NUM_PREDICTORS - 1)
    flags = buf[:, 1].astype(np.int32)

    f0 = np.array(VAG_F0)[predictor]   # (n_frames,)
    f1 = np.array(VAG_F1)[predictor]

    # Extract 28 signed 4-bit nibbles per frame -> scaled residual samples.
    payload = buf[:, 2:16].astype(np.int32)            # (n_frames, 14)
    lo = payload & 0x0F
    hi = (payload >> 4) & 0x0F
    nib = np.empty((n_frames, FRAME_SAMPLES), dtype=np.int32)
    nib[:, 0::2] = lo
    nib[:, 1::2] = hi
    nib = np.where(nib > 7, nib - 16, nib)             # sign-extend

    # scaled = (nib << 12) >> shift, done in float for the IIR.
    scaled = (nib << 12) >> shift[:, None]
    scaled = scaled.astype(np.float64)

    out = np.empty((n_frames, FRAME_SAMPLES), dtype=np.float64)
    hist1 = 0.0
    hist2 = 0.0
    silence = (flags == 7)
    for fi in range(n_frames):
        if silence[fi]:
            out[fi, :] = 0.0
            hist1 = 0.0
            hist2 = 0.0
            continue
        a0 = f0[fi]
        a1 = f1[fi]
        s_row = scaled[fi]
        for i in range(FRAME_SAMPLES):
            val = s_row[i] + hist1 * a0 + hist2 * a1
            # A bit-exact integer VAG decoder rounds each output sample to int16
            # and feeds THAT back as predictor history. Rounding here (rather
            # than only at the very end) keeps the IIR identical to vgmstream and
            # avoids sub-LSB drift.
            val = float(round(val))
            # PS2/vgmstream clamp each sample to int16 BEFORE feeding it back
            # into the predictor history. Without this, loud peaks make the IIR
            # diverge and produce audible clicks/distortion on following samples.
            if val > 32767.0:
                val = 32767.0
            elif val < -32768.0:
                val = -32768.0
            hist2 = hist1
            hist1 = val
            out[fi, i] = val

    return out.reshape(-1).astype(np.int16)


class EncodeCancelled(Exception):
    """Raised from _encode_channel when a cancel callback returns True."""


def _encode_channel(samples: np.ndarray, progress=None, cancel=None) -> bytes:
    """Encode one channel of int16 samples -> PS2 VAG ADPCM bytes.

    Vectorised, decoder-matched encoder. Two phases:

      1. Per frame, pick the predictor whose 2-tap residual has the least energy
         and derive the shift analytically from that residual's peak.
      2. Run the 28-sample ADPCM IIR for ALL frames at once (a 28-step loop over
         F-wide arrays instead of an F*28 Python loop over 65 candidates), using
         the SAME integer ``(q<<12)>>shift`` reconstruction the decoder uses, so
         round-trips stay tight. A second IIR pass seeds each frame's history
         from the previous frame's reconstructed tail, tracking the decoder's
         cross-frame state without a sequential per-frame loop.

    This replaces the old brute-force search (F * 28 * 65 numpy ops) that took
    minutes on a full song; it now runs in well under a second.

    Optional ``progress(done_frames, total_frames)`` / ``cancel()`` keep the same
    interface; both default to None so non-GUI callers are unaffected.
    """
    samples = np.asarray(samples).astype(np.float64)
    n = len(samples)
    n_frames = (n + FRAME_SAMPLES - 1) // FRAME_SAMPLES
    if n_frames == 0:
        return b""
    pad = n_frames * FRAME_SAMPLES - n
    if pad:
        samples = np.concatenate([samples, np.zeros(pad, dtype=np.float64)])
    if cancel is not None and cancel():
        raise EncodeCancelled()
    if progress is not None:
        progress(0, n_frames)

    F = n_frames
    f0a = np.array(VAG_F0)                       # (P,)
    f1a = np.array(VAG_F1)
    flat = samples                               # length F*28
    prev1 = np.empty_like(flat); prev1[0] = 0.0; prev1[1:] = flat[:-1]
    prev2 = np.empty_like(flat); prev2[:2] = 0.0; prev2[2:] = flat[:-2]

    # ---- phase 1: choose predictor (min residual energy) + shift per frame ---
    # Residual of each predictor over the whole signal, then per-frame energy.
    energies = np.empty((_NUM_PREDICTORS, F))
    resids = []
    for p in range(_NUM_PREDICTORS):
        r = flat - f0a[p] * prev1 - f1a[p] * prev2
        resids.append(r)
        energies[p] = (r.reshape(F, FRAME_SAMPLES) ** 2).sum(axis=1)
    pred_sel = energies.argmin(axis=0)                       # (F,)
    # Peak residual of the chosen predictor per frame -> analytic shift.
    resid_stack = np.stack(resids).reshape(_NUM_PREDICTORS, F, FRAME_SAMPLES)
    chosen_resid = resid_stack[pred_sel, np.arange(F)]       # (F, 28)
    peak = np.abs(chosen_resid).max(axis=1)                  # (F,)
    # q = round(resid * 2^sh / 4096) must stay within +/-7; keep one step of
    # headroom so reconstructed-history jitter can't overflow the nibble.
    with np.errstate(divide="ignore"):
        sh = np.floor(np.log2(28672.0 / np.maximum(peak, 1.0)))
    shift_sel = np.clip(sh, 0, 12).astype(np.int64)          # (F,)

    a0 = f0a[pred_sel]                                        # (F,)
    a1 = f1a[pred_sel]
    inv = (2.0 ** shift_sel) / 4096.0                        # q = round(diff*inv)
    x = flat.reshape(F, FRAME_SAMPLES)                       # (F, 28)

    # ---- phase 2: vectorised IIR encode (2 passes for cross-frame history) ---
    nibs = np.empty((F, FRAME_SAMPLES), dtype=np.int64)
    h1 = np.zeros(F); h2 = np.zeros(F)                        # frame-start history
    recon_tail1 = recon_tail2 = None
    for _pass in range(2):
        if _pass == 1:
            # Seed frame f's history from frame f-1's reconstructed tail.
            h1 = np.zeros(F); h2 = np.zeros(F)
            h1[1:] = recon_tail1[:-1]                          # prev frame last sample
            h2[1:] = recon_tail2[:-1]                          # prev frame 2nd-last
        eh1 = h1.copy(); eh2 = h2.copy()
        for i in range(FRAME_SAMPLES):
            pred = eh1 * a0 + eh2 * a1
            q = np.round((x[:, i] - pred) * inv)
            np.clip(q, -8, 7, out=q)
            qi = q.astype(np.int64)
            nibs[:, i] = qi
            scaled = (qi << 12) >> shift_sel                  # decoder-exact
            val = np.round(scaled + pred)
            np.clip(val, -32768.0, 32767.0, out=val)
            eh2 = eh1
            eh1 = val
        recon_tail1 = eh1                                     # sample 27 per frame
        recon_tail2 = eh2                                     # sample 26 per frame

    if cancel is not None and cancel():
        raise EncodeCancelled()

    # ---- pack frames -> bytes -----------------------------------------------
    out = np.empty((F, FRAME_BYTES), dtype=np.uint8)
    out[:, 0] = ((shift_sel & 0x0F) | ((pred_sel & 0x0F) << 4)).astype(np.uint8)
    out[:, 1] = 0
    nb = (nibs & 0x0F).astype(np.uint8)
    out[:, 2:16] = nb[:, 0::2] | (nb[:, 1::2] << 4)
    if progress is not None:
        progress(n_frames, n_frames)
    return out.tobytes()


# ---------------------------------------------------------------------------
# Container (de)interleaving
# ---------------------------------------------------------------------------
def decode_vag(data: bytes) -> tuple:
    """Return (sample_rate, channels, pcm); pcm int16 shape (n, channels)."""
    if len(data) < HEADER_SIZE:
        raise ValueError(
            f"VAG too small: need {HEADER_SIZE} header bytes, have {len(data)}")
    interleave, data_size, channels, sample_rate = struct.unpack('<4I', data[:HEADER_SIZE])
    # Header repair / validation. interleave==0 is tolerated (treated as the
    # standard block size) but bogus channel/rate values are rejected so a
    # corrupt header can't drive a huge allocation (e.g. channels=0x20202020).
    if interleave == 0:
        interleave = INTERLEAVE
    if interleave < FRAME_BYTES:
        raise ValueError(f"VAG interleave too small: {interleave}")
    if channels not in (1, 2):
        raise ValueError(f"VAG bad channel count: {channels}")
    if not (8000 <= sample_rate <= 48000):
        raise ValueError(f"VAG bad sample rate: {sample_rate}")

    payload = data[DATA_START:]
    avail = len(payload)
    # Trust the buffer over the header: a truncated file must not decode its
    # zero padding as audio. Clamp data_size to what's actually present.
    if data_size <= 0:
        data_size = avail
    if data_size > avail:
        data_size = avail
    per_channel = data_size // channels  # real ADPCM bytes per channel

    # De-interleave. The header `interleave` field (0x8000) is the size of one
    # COMPLETE L+R interleave cycle, NOT the per-channel chunk. The game (and
    # MFAudio, which shows "Interleave 4000") swap channels every
    # interleave/channels = 0x4000 bytes. Using the full 0x8000 as the per-channel
    # chunk splices half a block of the OTHER channel into each channel, so the
    # stereo image flip-flops ~1.5x/sec — the audible "warble". Divide by channels.
    block = max(FRAME_BYTES, interleave // channels)
    # `full` whole per-channel blocks round-robin, then a final partial chunk of
    # (per_channel % block) bytes per channel — songs whose per-channel size isn't
    # a whole number of blocks (e.g. music_1ps, music_xjapan) decode cleanly.
    full = per_channel // block
    last = per_channel - full * block
    chan_bytes = [bytearray() for _ in range(channels)]
    pos = 0
    for _ in range(full):
        for c in range(channels):
            chan_bytes[c] += payload[pos:pos + block]
            pos += block
    if last:
        for c in range(channels):
            chan_bytes[c] += payload[pos:pos + last]
            pos += last

    decoded = []
    for c in range(channels):
        raw = bytes(chan_bytes[c][:per_channel])
        raw = raw[:len(raw) - (len(raw) % FRAME_BYTES)]
        decoded.append(_decode_channel(raw))

    n = min(len(x) for x in decoded)
    pcm = np.empty((n, channels), dtype=np.int16)
    for c in range(channels):
        pcm[:, c] = decoded[c][:n]
    return sample_rate, channels, pcm


def encode_vag(pcm, sample_rate: int, channels: int = 2,
               progress=None, cancel=None) -> bytes:
    """Encode int16 PCM (n,channels) to the game's interleaved VAG container.

    Optional ``progress(done, total)`` / ``cancel()`` callbacks are forwarded to
    the per-channel encoder so a GUI can show progress and abort. Total frames
    are reported across all channels.
    """
    pcm = np.asarray(pcm)
    if pcm.ndim == 1:
        pcm = pcm[:, None]
    n, nch = pcm.shape
    if channels <= 0:
        channels = nch
    # adjust channel count
    if nch < channels:
        pcm = np.concatenate([pcm, np.tile(pcm[:, -1:], (1, channels - nch))], axis=1)
    elif nch > channels:
        pcm = pcm[:, :channels]
    pcm = pcm.astype(np.int16)

    # Per-channel frame count for an aggregated progress total across channels.
    frames_per_ch = (n + FRAME_SAMPLES - 1) // FRAME_SAMPLES
    total_frames = frames_per_ch * channels

    # Encode each channel, offsetting progress so it spans all channels.
    enc = []
    for c in range(channels):
        base = c * frames_per_ch
        ch_progress = None
        if progress is not None:
            ch_progress = (lambda done, _tot, _b=base:
                           progress(_b + done, total_frames))
        enc.append(_encode_channel(pcm[:, c], progress=ch_progress, cancel=cancel))
    # All channels share the same frame count (same n), so encoded lengths are
    # equal; assert it instead of silently truncating to enc[0] if they differ.
    assert len({len(e) for e in enc}) <= 1, "channel encode length mismatch"
    per_channel = len(enc[0])
    data_size = per_channel * channels

    # Interleave. The header field stores INTERLEAVE (0x8000) = one full L+R cycle,
    # but channels are physically swapped every INTERLEAVE/channels = 0x4000 bytes
    # (matches the game's decoder and every original file). Interleaving at the
    # full 0x8000 per channel — as the old code did — makes the game play a
    # warbling, channel-swapped mess. Use the per-channel block for the actual
    # interleaving, but keep the header field and file padding at INTERLEAVE.
    block = max(FRAME_BYTES, INTERLEAVE // channels)
    full = per_channel // block
    last = per_channel - full * block
    out = bytearray()
    out += struct.pack('<4I', INTERLEAVE, data_size, channels, sample_rate)
    out += b'\xFF' * 8                       # match real files: 8x FF
    out += b'\x00' * (DATA_START - len(out))  # zero-fill rest of first block

    for b in range(full):
        for c in range(channels):
            out += enc[c][b * block:(b + 1) * block]
    if last:
        for c in range(channels):
            out += enc[c][full * block:full * block + last]
    out += b'\x00' * ((-len(out)) % INTERLEAVE)   # pad to whole interleave block
    return bytes(out)


# ---------------------------------------------------------------------------
# Audio file loading / resampling
# ---------------------------------------------------------------------------
def _resample_linear(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interp resample float/int pcm shape (n, ch) -> (m, ch)."""
    if src_rate == dst_rate:
        return pcm
    n = pcm.shape[0]
    ch = pcm.shape[1]
    m = int(round(n * dst_rate / src_rate))
    if m <= 0 or n == 0:
        return np.zeros((0, ch), dtype=pcm.dtype)
    src_x = np.arange(n, dtype=np.float64)
    dst_x = np.linspace(0, n - 1, m)
    out = np.empty((m, ch), dtype=np.float64)
    for c in range(ch):
        out[:, c] = np.interp(dst_x, src_x, pcm[:, c].astype(np.float64))
    return out


def _load_wav(path: str):
    """Load wav via stdlib wave -> (pcm int16 (n,ch), rate)."""
    with wave.open(path, 'rb') as w:
        ch = w.getnchannels()
        sw = w.getsampwidth()
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    if sw == 2:
        arr = np.frombuffer(frames, dtype='<i2').astype(np.int16)
    elif sw == 1:
        arr = (np.frombuffer(frames, dtype=np.uint8).astype(np.int16) - 128) * 256
        arr = arr.astype(np.int16)
    elif sw == 3:
        b = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3)
        a32 = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
               (b[:, 2].astype(np.int32) << 16))
        a32 = np.where(a32 & 0x800000, a32 - 0x1000000, a32)
        arr = (a32 >> 8).astype(np.int16)
    elif sw == 4:
        arr = (np.frombuffer(frames, dtype='<i4') >> 16).astype(np.int16)
    else:
        raise ValueError(f"Unsupported wav sample width: {sw}")
    arr = arr.reshape(-1, ch)
    return arr, rate


def _load_ogg(path: str):
    """Load ogg via soundfile or librosa -> (pcm int16 (n,ch), rate)."""
    if _HAVE_SF:
        data, rate = _sf.read(path, always_2d=True, dtype='float32')
        pcm = np.clip(data, -1.0, 1.0)
        pcm = (pcm * 32767.0).round().astype(np.int16)
        return pcm, rate
    if _HAVE_LIBROSA:
        data, rate = _librosa.load(path, sr=None, mono=False)
        if data.ndim == 1:
            data = data[None, :]
        data = data.T  # (n, ch)
        pcm = np.clip(data, -1.0, 1.0)
        pcm = (pcm * 32767.0).round().astype(np.int16)
        return pcm, int(rate)
    raise RuntimeError(
        "OGG support requires 'soundfile' or 'librosa' (neither is installed). "
        "Please provide a WAV file instead.")


def _load_audio_any(path: str):
    """Load .wav/.ogg (or anything soundfile handles) -> (int16 pcm (n,ch), rate)."""
    low = path.lower()
    if low.endswith('.wav'):
        return _load_wav(path)
    if low.endswith('.ogg'):
        return _load_ogg(path)
    if _HAVE_SF:
        data, rate = _sf.read(path, always_2d=True, dtype='float32')
        return (np.clip(data, -1, 1) * 32767.0).round().astype(np.int16), int(rate)
    return _load_wav(path)


def detect_lead_silence_ms(path: str, threshold: int = 1000) -> float:
    """Milliseconds of leading near-silence before the music actually starts.

    Finds the first sample whose |amplitude| (max across channels) exceeds
    ``threshold`` (int16 scale). Used to auto-compute the chart-sync gap: how much
    silence the source audio already has vs. how far in the chart's first note is.
    Returns 0.0 if the file is silent/unreadable.
    """
    try:
        pcm, rate = _load_audio_any(path)
    except Exception:
        return 0.0
    if pcm.ndim == 1:
        pcm = pcm[:, None]
    amp = np.abs(pcm.astype(np.int32)).max(axis=1)
    hits = np.nonzero(amp > threshold)[0]
    if len(hits) == 0:
        return 0.0
    return float(hits[0]) / rate * 1000.0


def convert_audio_file(path: str, sample_rate: int = 44100,
                       progress=None, cancel=None, lead_silence_ms: float = 0.0,
                       trim_start_ms: float = 0.0) -> bytes:
    """Load a .wav or .ogg, resample to sample_rate stereo, return game VAG bytes.

    ``lead_silence_ms`` prepends that many milliseconds of silence to the front of
    the audio. The game plays the BGM and the chart both from t=0, so a song whose
    music should start N ms into the enso needs N ms of leading silence baked in
    (retail songs carry 1.5-2.5 s of it, individually tuned per chart). Without it
    a gapless custom track plays too early and the notes look late.

    ``trim_start_ms`` cuts that many milliseconds off the FRONT instead. A TJA
    whose OFFSET is more negative than one measure needs the music to start
    *earlier* than the blank lead measure allows, which silence cannot express.
    The two are mutually exclusive; see song_builder.prepare_tja_for_game.

    Optional ``progress(done, total)`` / ``cancel()`` callbacks are forwarded to
    the encoder so a GUI worker thread can report progress and cancel.
    """
    pcm, rate = _load_audio_any(path)

    rate = int(rate)
    if pcm.ndim == 1:
        pcm = pcm[:, None]
    if pcm.shape[1] > 2:
        pcm = pcm[:, :2]

    # Resample BEFORE duplicating mono -> stereo so we don't resample two
    # identical channels (half the work). Mono stays mono through resampling.
    if rate != sample_rate:
        resampled = _resample_linear(pcm, rate, sample_rate)
        pcm = np.clip(resampled, -32768, 32767).round().astype(np.int16)

    # force stereo
    if pcm.shape[1] == 1:
        pcm = np.repeat(pcm, 2, axis=1)

    # Shift the music so its downbeat lands where the chart expects it (see
    # docstring). Done after resample so the gap is exact in output samples.
    if trim_start_ms and trim_start_ms > 0:
        cut = int(round(trim_start_ms * sample_rate / 1000.0))
        pcm = pcm[cut:] if cut < len(pcm) else pcm[:0]
    if lead_silence_ms and lead_silence_ms > 0:
        pad = int(round(lead_silence_ms * sample_rate / 1000.0))
        if pad > 0:
            pcm = np.concatenate([np.zeros((pad, 2), dtype=np.int16), pcm], axis=0)

    return encode_vag(pcm, sample_rate, channels=2,
                      progress=progress, cancel=cancel)


# ---------------------------------------------------------------------------
# PySide6 import dialog
# ---------------------------------------------------------------------------
try:
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
        QFileDialog, QLineEdit, QDialogButtonBox, QProgressBar,
    )
    from PySide6.QtCore import Qt, QThread, Signal
    _HAVE_QT = True
except Exception:
    _HAVE_QT = False


if _HAVE_QT:
    class _EncodeWorker(QThread):
        """Run convert_audio_file off the GUI thread.

        Emits progress(done, total); on finish emits done(bytes-or-None, error).
        Set ``self._cancel = True`` to request cancellation.
        """
        progress = Signal(int, int)
        finished_ok = Signal(object, str)

        def __init__(self, path: str, sample_rate: int, parent=None):
            super().__init__(parent)
            self._path = path
            self._sr = sample_rate
            self._cancel = False

        def cancel(self):
            self._cancel = True

        def run(self):
            try:
                data = convert_audio_file(
                    self._path, self._sr,
                    progress=lambda d, t: self.progress.emit(int(d), int(t)),
                    cancel=lambda: self._cancel)
                self.finished_ok.emit(data, "")
            except EncodeCancelled:
                self.finished_ok.emit(None, "cancelled")
            except Exception as e:  # noqa: BLE001
                self.finished_ok.emit(None, str(e))

    class AudioImportDialog(QDialog):
        """Pick a wav/ogg and convert it into a game VAG, using an existing
        game vag as template (sample rate / channel defaults).

        On accept, self.result_bytes holds the new VAG bytes (else None).
        """

        def __init__(self, template_vag: bytes = None, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Import Audio -> Game VAG")
            self.result_bytes = None
            self._src_path = None

            # defaults from template
            self._sample_rate = 44100
            self._channels = 2
            if template_vag and len(template_vag) >= HEADER_SIZE:
                try:
                    _ib, _ds, ch, sr = struct.unpack('<4I', template_vag[:HEADER_SIZE])
                    if sr > 0:
                        self._sample_rate = sr
                    if ch > 0:
                        self._channels = ch
                except Exception:
                    pass

            lay = QVBoxLayout(self)

            lay.addWidget(QLabel(
                f"Target: {self._sample_rate} Hz, {self._channels} ch "
                f"(from template)"))

            # file picker row
            row = QHBoxLayout()
            self.path_edit = QLineEdit()
            self.path_edit.setPlaceholderText("Choose a .wav or .ogg file...")
            self.path_edit.setReadOnly(True)
            browse = QPushButton("Browse...")
            browse.clicked.connect(self._browse)
            row.addWidget(self.path_edit)
            row.addWidget(browse)
            lay.addLayout(row)

            self.info_label = QLabel("No file selected.")
            self.info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(self.info_label)

            self.status_label = QLabel("")
            lay.addWidget(self.status_label)

            self.progress = QProgressBar()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.progress.setVisible(False)
            lay.addWidget(self.progress)

            bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
            self.save_btn = bb.button(QDialogButtonBox.Save)
            self.save_btn.setEnabled(False)
            self.cancel_btn = bb.button(QDialogButtonBox.Cancel)
            bb.accepted.connect(self._on_save)
            bb.rejected.connect(self._on_reject)
            lay.addWidget(bb)

            self._worker = None

        def _browse(self):
            import appconfig                       # last-used-path memory
            filt = "Audio (*.wav *.ogg);;WAV (*.wav);;OGG (*.ogg);;All (*.*)"
            path = appconfig.pick_open(self, "wav", "Select audio file", filt)
            if not path:
                return
            self._src_path = path
            self.path_edit.setText(path)
            self._update_info()

        def _update_info(self):
            if not self._src_path:
                return
            try:
                low = self._src_path.lower()
                if low.endswith('.wav'):
                    pcm, rate = _load_wav(self._src_path)
                elif low.endswith('.ogg'):
                    pcm, rate = _load_ogg(self._src_path)
                else:
                    pcm, rate = _load_wav(self._src_path)
                if pcm.ndim == 1:
                    pcm = pcm[:, None]
                n = pcm.shape[0]
                dur = n / float(rate) if rate else 0.0
                out_n = int(round(n * self._sample_rate / rate)) if rate else 0
                est_frames = (out_n + FRAME_SAMPLES - 1) // FRAME_SAMPLES
                est_bytes = est_frames * FRAME_BYTES * self._channels + DATA_START
                self.info_label.setText(
                    f"Source: {rate} Hz, {pcm.shape[1]} ch, "
                    f"{dur:.2f} s\n"
                    f"Output (est): {self._sample_rate} Hz stereo, "
                    f"~{est_bytes/1024/1024:.2f} MB")
                self.status_label.setText("")
                self.save_btn.setEnabled(True)
            except Exception as e:
                self.info_label.setText(f"Cannot read file: {e}")
                self.save_btn.setEnabled(False)

        def _on_save(self):
            if not self._src_path or self._worker is not None:
                return
            # Encode off the GUI thread: full-length encode is O(F*65*28) pure
            # Python and would freeze the UI for many seconds otherwise.
            self.status_label.setText("Converting (encoding ADPCM)...")
            self.progress.setValue(0)
            self.progress.setVisible(True)
            self.save_btn.setEnabled(False)
            self._worker = _EncodeWorker(self._src_path, self._sample_rate, self)
            self._worker.progress.connect(self._on_progress)
            self._worker.finished_ok.connect(self._on_encoded)
            self._worker.start()

        def _on_progress(self, done: int, total: int):
            if total > 0:
                self.progress.setValue(int(done * 100 / total))

        def _on_encoded(self, data, err: str):
            self._worker = None
            self.progress.setVisible(False)
            if data is not None:
                self.result_bytes = data
                self.accept()
                return
            self.result_bytes = None
            self.save_btn.setEnabled(True)
            if err == "cancelled":
                self.status_label.setText("Cancelled.")
            else:
                self.status_label.setText(f"Error: {err}")

        def _on_reject(self):
            # If an encode is running, cancel it and wait; otherwise close.
            if self._worker is not None:
                self.status_label.setText("Cancelling...")
                self._worker.cancel()
                self._worker.wait()
                self._worker = None
                self.progress.setVisible(False)
                self.save_btn.setEnabled(True)
                return
            self.reject()

        def closeEvent(self, ev):
            if self._worker is not None:
                self._worker.cancel()
                self._worker.wait()
                self._worker = None
            super().closeEvent(ev)
else:
    class AudioImportDialog:  # pragma: no cover - fallback when Qt missing
        def __init__(self, *a, **k):
            raise RuntimeError("PySide6 is not available; AudioImportDialog cannot be used.")


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def _correlation(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    n = min(len(a), len(b))
    a = a[:n]
    b = b[:n]
    if n == 0 or a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _self_test() -> bool:
    import os
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "test", "sound", "stream")
    candidates = ["music_10tai", "music_1ps", "STR_DOJO_BGM"]
    real = None
    for c in candidates:
        p = os.path.join(base, c, "vag")
        if os.path.isfile(p):
            real = p
            break
    if real is None:
        print("FAIL: no real game vag found under", base)
        return False

    print(f"Self-test using: {real}")
    data = open(real, "rb").read()

    # parse original header
    o_ib, o_ds, o_ch, o_sr = struct.unpack('<4I', data[:HEADER_SIZE])
    print(f"Original header: interleave=0x{o_ib:X} dataSize={o_ds} "
          f"channels={o_ch} sampleRate={o_sr}")

    # 1) decode
    sr, ch, pcm = decode_vag(data)
    print(f"Decoded: {sr} Hz, {ch} ch, {pcm.shape[0]} samples "
          f"({pcm.shape[0]/sr:.1f} s)")
    assert sr == o_sr and ch == o_ch, "header mismatch on decode"

    # ~8 s slice keeps the self-test snappy; the vectorised encoder handles the
    # full song in a couple of seconds, so this bound is just for test speed.
    slice_n = min(pcm.shape[0], sr * 8)
    pcm_slice = pcm[:slice_n]

    # 2) re-encode
    enc = encode_vag(pcm_slice, sr, ch)

    # header check
    e_ib, e_ds, e_ch, e_sr = struct.unpack('<4I', enc[:HEADER_SIZE])
    print(f"Re-encoded header: interleave=0x{e_ib:X} dataSize={e_ds} "
          f"channels={e_ch} sampleRate={e_sr}")
    header_ok = (e_ib == o_ib and e_ch == o_ch and e_sr == o_sr)

    # 3) decode again
    sr2, ch2, pcm2 = decode_vag(enc)
    assert sr2 == sr and ch2 == ch, "header mismatch on re-decode"

    # metrics per channel
    corrs = []
    maes = []
    for c in range(ch):
        a = pcm_slice[:, c]
        b = pcm2[:, c]
        n = min(len(a), len(b))
        corrs.append(_correlation(a[:n], b[:n]))
        maes.append(float(np.mean(np.abs(a[:n].astype(np.float64) -
                                         b[:n].astype(np.float64)))))
    min_corr = min(corrs)
    max_mae = max(maes)
    print(f"Round-trip correlation per channel: "
          f"{['%.4f' % c for c in corrs]}")
    print(f"Round-trip mean-abs-error per channel: "
          f"{['%.1f' % m for m in maes]}  (int16 scale, max=32768)")

    # 4) encode->decode self-consistency already covered (decode_vag worked)
    enc_decodable = (e_ib == INTERLEAVE and e_ch == ch and e_sr == sr)

    ok = (min_corr > 0.95 and header_ok and enc_decodable)
    print()
    print(f"  header fields match : {header_ok}")
    print(f"  encode decodable    : {enc_decodable}")
    print(f"  min correlation>0.95: {min_corr > 0.95} ({min_corr:.4f})")
    print(f"  max MAE             : {max_mae:.1f}")
    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_test() else 1)

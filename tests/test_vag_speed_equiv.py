"""Optimised VAG decode / sinc resample must be bit-identical to the originals."""
import numpy as np

from taiko2x6.audio import vagtool as V


def _old_decode_channel(adpcm):
    n_frames = len(adpcm) // V.FRAME_BYTES
    if n_frames == 0:
        return np.zeros(0, dtype=np.int16)
    buf = np.frombuffer(adpcm, dtype=np.uint8).reshape(n_frames, V.FRAME_BYTES)
    ctrl = buf[:, 0].astype(np.int32)
    shift = ctrl & 0x0F
    shift = np.where(shift > 12, 9, shift)
    predictor = np.clip(ctrl >> 4, 0, V._NUM_PREDICTORS - 1)
    flags = buf[:, 1].astype(np.int32)
    f0 = np.array(V.VAG_F0)[predictor]
    f1 = np.array(V.VAG_F1)[predictor]
    payload = buf[:, 2:16].astype(np.int32)
    nib = np.empty((n_frames, V.FRAME_SAMPLES), dtype=np.int32)
    nib[:, 0::2] = payload & 0x0F
    nib[:, 1::2] = (payload >> 4) & 0x0F
    nib = np.where(nib > 7, nib - 16, nib)
    scaled = ((nib << 12) >> shift[:, None]).astype(np.float64)
    out = np.empty((n_frames, V.FRAME_SAMPLES), dtype=np.float64)
    hist1 = hist2 = 0.0
    silence = (flags == 7)
    for fi in range(n_frames):
        if silence[fi]:
            out[fi, :] = 0.0
            hist1 = hist2 = 0.0
            continue
        a0 = f0[fi]
        a1 = f1[fi]
        s_row = scaled[fi]
        for i in range(V.FRAME_SAMPLES):
            val = s_row[i] + hist1 * a0 + hist2 * a1
            val = float(round(val))
            if val > 32767.0:
                val = 32767.0
            elif val < -32768.0:
                val = -32768.0
            hist2 = hist1
            hist1 = val
            out[fi, i] = val
    return out.reshape(-1).astype(np.int16)


def _old_sinc(pcm, src_rate, dst_rate, half_taps=16, chunk=1 << 18):
    pcm = np.asarray(pcm)
    if pcm.ndim == 1:
        pcm = pcm[:, None]
    n, ch = pcm.shape
    if src_rate == dst_rate or n == 0:
        return pcm.astype(np.float64)
    ratio = dst_rate / src_rate
    cutoff = min(1.0, ratio) * 0.94
    m = int(round(n * ratio))
    x = pcm.astype(np.float64)
    out = np.empty((m, ch), dtype=np.float64)
    ks = np.arange(-half_taps + 1, half_taps + 1)
    for s in range(0, m, chunk):
        t = np.arange(s, min(m, s + chunk), dtype=np.float64) / ratio
        i0 = np.floor(t).astype(np.int64)
        frac = t - i0
        acc = np.zeros((len(t), ch), dtype=np.float64)
        for k in ks:
            d = k - frac
            w = cutoff * np.sinc(cutoff * d) * (0.5 + 0.5 * np.cos(np.pi * d / half_taps))
            idx = i0 + k
            ok = (idx >= 0) & (idx < n)
            acc += np.where(ok[:, None], x[np.clip(idx, 0, n - 1)], 0.0) * w[:, None]
        out[s:s + len(t)] = acc
    return out


def _signals():
    rng = np.random.default_rng(3)
    for n in (0, 1, 27, 28, 29, 1000, 12345):
        t = np.arange(n)
        yield np.zeros(n, np.int16)
        yield (np.sin(t * 0.05) * 12000).astype(np.int16)
        yield rng.integers(-32768, 32768, n).astype(np.int16)
        yield np.clip(np.sin(t * 0.01) * 60000, -32768, 32767).astype(np.int16)


def test_decode_channel_identical():
    for s in _signals():
        enc = V._encode_channel(s)
        assert np.array_equal(V._decode_channel(enc), _old_decode_channel(enc))
    rng = np.random.default_rng(9)
    raw = rng.integers(0, 256, 16 * 500, dtype=np.uint8).tobytes()  # out-of-spec bytes
    assert np.array_equal(V._decode_channel(raw), _old_decode_channel(raw))
    ba = bytearray(raw)
    for f in range(0, 500, 7):
        ba[f * 16 + 1] = 7                                          # silence frames
    assert np.array_equal(V._decode_channel(bytes(ba)), _old_decode_channel(bytes(ba)))


def test_sinc_identical():
    rng = np.random.default_rng(5)
    for n in (1, 50, 5000):
        pcm = rng.integers(-32768, 32768, (n, 2)).astype(np.int16)
        for dst in (32000, 48000, 24000):
            assert np.array_equal(V.resample_sinc(pcm, 44100, dst),
                                  _old_sinc(pcm, 44100, dst))

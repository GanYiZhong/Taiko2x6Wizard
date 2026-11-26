"""Audio stage of the Omnimix build: sinc speed-up identity, cache robustness."""
import os

import numpy as np

from taiko2x6.audio import vagtool as V
from taiko2x6.songs import omnimix_maker as M


def _ref_sinc(pcm, src_rate, dst_rate, half_taps=16, chunk=1 << 18):
    """The original (pre-optimisation) resample_sinc, kept as the reference."""
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
            w = np.where(ok, w, 0.0)
            acc += x[np.clip(idx, 0, n - 1)] * w[:, None]
        out[s:s + len(t)] = acc
    return out


def test_sinc_matches_reference():
    rng = np.random.default_rng(11)
    for n in (1, 7, 40, 4095, 4096, 4097, 9000, 30000):
        pcm = rng.integers(-32768, 32768, (n, 2)).astype(np.int16)
        for dst in (32000, 24000, 48000, 44099):
            assert np.array_equal(V.resample_sinc(pcm, 44100, dst),
                                  _ref_sinc(pcm, 44100, dst)), (n, dst)
    mono = rng.integers(-32768, 32768, 5000).astype(np.int16)
    assert np.array_equal(V.resample_sinc(mono, 44100, 32000),
                          _ref_sinc(mono, 44100, 32000))
    assert np.array_equal(V.resample_sinc(mono, 44100, 32000, chunk=333),
                          _ref_sinc(mono, 44100, 32000))


def _song(seed, n=44100):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    y = np.stack([np.sin(t * 0.03) * 9000, np.sin(t * 0.02 + seed) * 8000], 1)
    y += rng.normal(0, 300, y.shape)
    return V.encode_vag(np.clip(y, -32768, 32767).astype(np.int16), 44100, 2)


def test_cache_hit_miss_corrupt_and_identity(tmp_path):
    cache = str(tmp_path / "cache")
    songs = [_song(i, 20000 + 1000 * i) for i in range(3)]
    direct = [V.resample_vag(s, 32000) for s in songs]

    # miss: encodes and fills the cache; output identical to the direct path
    assert M._resample_many(songs, 32000, cache, False, workers=2) == direct
    files = sorted(f for f in os.listdir(cache) if f.endswith(".vag"))
    assert len(files) == 3
    assert not [f for f in os.listdir(cache) if f.endswith(".tmp")]

    # hit: served from the cache with no encoder call at all
    real = V.resample_vag
    V.resample_vag = lambda *a, **k: (_ for _ in ()).throw(AssertionError("encoded"))
    try:
        assert M._resample_many(songs, 32000, cache, False, workers=1) == direct
    finally:
        V.resample_vag = real

    # different rate / mono flag are different keys
    assert M._audio_cache_path(cache, songs[0], 24000, False) != \
        M._audio_cache_path(cache, songs[0], 32000, False)
    assert M._audio_cache_path(cache, songs[0], 32000, True) != \
        M._audio_cache_path(cache, songs[0], 32000, False)

    # corrupt entries (bit flip, truncation, garbage) are dropped and rebuilt
    p0 = M._audio_cache_path(cache, songs[0], 32000, False)
    p1 = M._audio_cache_path(cache, songs[1], 32000, False)
    p2 = M._audio_cache_path(cache, songs[2], 32000, False)
    b = bytearray(open(p0, "rb").read()); b[-5] ^= 0xFF
    open(p0, "wb").write(bytes(b))
    open(p1, "wb").write(open(p1, "rb").read()[:50])
    open(p2, "wb").write(b"garbage")
    assert M._cache_read(p0) is None and not os.path.exists(p0)
    open(p0, "wb").write(bytes(b))
    assert M._resample_many(songs, 32000, cache, False, workers=2) == direct
    for p, d in zip((p0, p1, p2), direct):
        assert M._cache_read(p) == d


def test_prune_cache_lru(tmp_path):
    cache = str(tmp_path)
    paths = []
    for i in range(4):
        p = os.path.join(cache, "e%d.vag" % i)
        M._cache_write(p, b"x" * 1000)
        os.utime(p, (1000 + i, 1000 + i))
        paths.append(p)
    size = os.path.getsize(paths[0])
    M.prune_audio_cache(cache, max_bytes=2 * size + 1)
    assert [os.path.exists(p) for p in paths] == [False, False, True, True]


def test_map_parallel_empty_and_order():
    assert M._map_parallel(abs, []) == []
    assert M._map_parallel(abs, [-3, -2, -1], workers=2) == [3, 2, 1]

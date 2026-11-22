"""vagtool: band-limited resampling and re-encoding a stream at a lower rate."""
import struct

import numpy as np

from taiko2x6.audio import vagtool as V


def _tone(freq, rate=44100, secs=1.0, amp=12000):
    t = np.arange(int(rate * secs)) / rate
    return (np.sin(2 * np.pi * freq * t) * amp)[:, None]


def _peak(y, rate):
    y = y[1000:-1000, 0]
    spec = np.abs(np.fft.rfft(y))
    return np.fft.rfftfreq(len(y), 1 / rate)[spec.argmax()], np.abs(y).max()


def test_resample_keeps_tones_and_length():
    y = V.resample_sinc(_tone(1000), 44100, 32000)
    assert len(y) == 32000
    f, amp = _peak(y, 32000)
    assert abs(f - 1000) < 2 and 11500 < amp < 12500


def test_resample_removes_content_above_new_nyquist():
    # 20 kHz cannot exist at 32 kHz; a plain interpolator folds it to 12 kHz
    y = V.resample_sinc(_tone(20000), 44100, 32000)
    assert np.abs(y[1000:-1000]).max() < 200


def test_resample_vag_roundtrip():
    stereo = np.hstack([_tone(440, secs=0.5), _tone(880, secs=0.5)]).astype(np.int16)
    vag = V.encode_vag(stereo, 44100, 2)
    out = V.resample_vag(vag, 32000)
    size = lambda b: struct.unpack_from("<I", b, 4)[0]          # noqa: E731
    assert size(out) < size(vag) * 0.75
    rate, ch, pcm = V.decode_vag(out)
    assert (rate, ch) == (32000, 2)
    assert abs(pcm.shape[0] - 16000) < 64            # same duration
    # already low enough / looped streams are left alone
    assert V.resample_vag(out, 32000) is out
    looped = bytearray(vag)
    struct.pack_into("<i", looped, 16, 0)
    assert V.resample_vag(bytes(looped), 32000) == bytes(looped)

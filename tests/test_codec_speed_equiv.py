"""The vectorised XOR codec must be byte-identical to the original per-byte loops."""
import random
import zlib

from taiko2x6.archive import taiko256_archive_tool_v2 as core


def _old_crypt_list(data):
    return bytes(b ^ core.KEY[i & 0xFF] for i, b in enumerate(data))


def _old_crypt_data_block(data):
    out = bytearray(data)
    for ci in range((len(out) + 15) // 16):
        out[ci * 16] ^= core.KEY[ci & 0xFF]
    return bytes(out)


def _samples():
    rng = random.Random(1)
    for n in [0, 1, 15, 16, 17, 255, 256, 4095, 4096, 4097, 100003, 300000]:
        yield rng.randbytes(n)
    yield b"abc" * 50000
    yield bytes(70000)


def test_crypt_identical_to_reference():
    for d in _samples():
        assert core.crypt_data_block(d) == _old_crypt_data_block(d)
        assert core.crypt_list(d) == _old_crypt_list(d)
        assert core.crypt_data_block(core.crypt_data_block(d)) == d


def test_group_roundtrip_and_encoding_identical():
    for d in _samples():
        for comp in (2, 6):
            enc = core.encode_group_payload(d, comp)
            if comp == 6:
                assert enc == _old_crypt_data_block(d)
            else:
                co = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=zlib.MAX_WBITS)
                assert enc == _old_crypt_data_block(co.compress(d) + co.flush())
            group = {"index": 0, "name": "t", "sector": 0, "packed_size": len(enc), "compression": comp}
            assert core.decode_group_payload(enc, group) == d

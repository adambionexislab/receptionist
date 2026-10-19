"""G.711 μ-law <-> 16-bit linear PCM, without audioop (gone since Python 3.13).

The caller's audio goes to gpt-realtime as audio/pcmu: the same 8 kHz μ-law
OpenAI gets over SIP today, so what she hears is unchanged (roadmap D3).
"""

from array import array

_BIAS = 0x84
_CLIP = 32635


def _decode_byte(byte: int) -> int:
    u = ~byte & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


def _encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    if sign:
        sample = -sample
    sample = min(sample, _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not sample & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


_DECODE = [_decode_byte(b) for b in range(256)]
# Indexed by the 16-bit sample as unsigned (sample & 0xFFFF).
_ENCODE = bytes(_encode_sample(s - 65536 if s >= 32768 else s) for s in range(65536))


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    """Little-endian signed 16-bit mono → μ-law, one byte per sample."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    return bytes(_ENCODE[s & 0xFFFF] for s in samples)


def ulaw_to_pcm16(ulaw: bytes) -> bytes:
    return array("h", (_DECODE[b] for b in ulaw)).tobytes()

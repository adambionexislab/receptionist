"""Debug capture of the caller's audio (DEBUG_CAPTURE_DIR, off by default).

Two WAV files per call, for test calls only:
  <uuid>-from-asterisk.wav   what Asterisk sent: 16-bit PCM, 8 kHz (AudioSocket 0x10)
  <uuid>-to-openai.wav       what went to OpenAI: G.711 μ-law, 8 kHz (audio/pcmu)

Both play in any audio player. If the first sounds clean and the second is
noise, the conversion is wrong; if both are noise, the audio was already bad
when it left Asterisk. Caller audio is personal data: switch capture on only
for your own test calls, and delete the files afterwards.
"""

import logging
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

_PCM = 1
_MULAW = 7


class WavWriter:
    """A mono 8 kHz WAV written as audio arrives; sizes fixed on close()."""

    def __init__(self, path: Path, fmt: int) -> None:
        self.path = path
        self.fmt = fmt
        self.bits = 16 if fmt == _PCM else 8
        self.data_bytes = 0
        self._f = open(path, "wb")
        self._f.write(self._header())

    def _header(self) -> bytes:
        rate, channels = 8000, 1
        block = channels * self.bits // 8
        if self.fmt == _PCM:
            fmt_chunk = struct.pack("<4sIHHIIHH", b"fmt ", 16, _PCM, channels, rate,
                                    rate * block, block, self.bits)
            fact = b""
        else:
            # Non-PCM formats carry cbSize and a fact chunk (sample count).
            fmt_chunk = struct.pack("<4sIHHIIHHH", b"fmt ", 18, self.fmt, channels, rate,
                                    rate * block, block, self.bits, 0)
            fact = struct.pack("<4sII", b"fact", 4, self.data_bytes // block)
        data_hdr = struct.pack("<4sI", b"data", self.data_bytes)
        riff_size = 4 + len(fmt_chunk) + len(fact) + len(data_hdr) + self.data_bytes
        return struct.pack("<4sI4s", b"RIFF", riff_size, b"WAVE") + fmt_chunk + fact + data_hdr

    def write(self, data: bytes) -> None:
        self._f.write(data)
        self.data_bytes += len(data)

    def close(self) -> None:
        if self._f.closed:
            return
        self._f.seek(0)
        self._f.write(self._header())
        self._f.close()


class CallCapture:
    def __init__(self, directory: str, call_uuid: str) -> None:
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        self.from_asterisk = WavWriter(folder / f"{call_uuid}-from-asterisk.wav", _PCM)
        self.to_openai = WavWriter(folder / f"{call_uuid}-to-openai.wav", _MULAW)
        logger.warning("Call %s: capturing caller audio to %s (DEBUG_CAPTURE_DIR)", call_uuid[:8], folder)

    def close(self) -> None:
        self.from_asterisk.close()
        self.to_openai.close()


def open_capture(directory: str, call_uuid: str) -> CallCapture | None:
    if not directory:
        return None
    try:
        return CallCapture(directory, call_uuid)
    except OSError as exc:
        logger.error("Debug capture unavailable (%s): %s", directory, exc)
        return None

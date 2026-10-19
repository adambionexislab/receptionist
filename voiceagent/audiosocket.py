"""Asterisk AudioSocket framing.

Every message: 1 byte kind, 2 bytes payload length (big-endian), payload.
Asterisk sends the call's UUID first (16 raw bytes), then 20 ms audio frames:
signed 16-bit little-endian PCM, 8 kHz mono (320 bytes). We send audio the same
way, and a hangup (kind 0x00, empty) to end the call.
"""

import asyncio
import struct
import uuid

KIND_HANGUP = 0x00
KIND_UUID = 0x01
KIND_DTMF = 0x03
KIND_AUDIO = 0x10
KIND_ERROR = 0xFF

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS // 1000  # 320


def pack(kind: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BH", kind, len(payload)) + payload


async def read_message(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    """One message, or None when Asterisk closed the socket."""
    try:
        header = await reader.readexactly(3)
        kind, length = struct.unpack(">BH", header)
        payload = await reader.readexactly(length) if length else b""
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
    return kind, payload


def uuid_from_payload(payload: bytes) -> str:
    return str(uuid.UUID(bytes=payload[:16]))

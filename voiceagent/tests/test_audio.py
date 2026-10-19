"""G.711 μ-law, AudioSocket framing, and what-was-heard alignment."""

import asyncio
import uuid
from array import array

from voiceagent import audiosocket, g711
from voiceagent.eleven import AlignmentTrack


def test_ulaw_round_trip_is_close():
    samples = array("h", [0, 100, -100, 1000, -1000, 8000, -8000, 32000, -32000])
    back = array("h")
    back.frombytes(g711.ulaw_to_pcm16(g711.pcm16_to_ulaw(samples.tobytes())))
    for a, b in zip(samples, back):
        assert abs(a - b) <= max(16, abs(a) // 16)  # μ-law step size


def test_ulaw_known_values():
    assert g711.pcm16_to_ulaw(array("h", [0]).tobytes()) == b"\xff"
    assert g711.ulaw_to_pcm16(b"\xff") == array("h", [0]).tobytes()
    assert g711.ulaw_to_pcm16(b"\x00") == array("h", [-32124]).tobytes()


def test_a_20ms_frame_becomes_160_ulaw_bytes():
    assert len(g711.pcm16_to_ulaw(b"\x00" * audiosocket.FRAME_BYTES)) == 160


async def test_audiosocket_messages_round_trip():
    reader = asyncio.StreamReader()
    call_id = uuid.uuid4()
    reader.feed_data(audiosocket.pack(audiosocket.KIND_UUID, call_id.bytes))
    reader.feed_data(audiosocket.pack(audiosocket.KIND_AUDIO, b"\x01\x02" * 160))
    reader.feed_data(audiosocket.pack(audiosocket.KIND_HANGUP))
    reader.feed_eof()

    kind, payload = await audiosocket.read_message(reader)
    assert kind == audiosocket.KIND_UUID
    assert audiosocket.uuid_from_payload(payload) == str(call_id)
    kind, payload = await audiosocket.read_message(reader)
    assert kind == audiosocket.KIND_AUDIO and len(payload) == 320
    assert await audiosocket.read_message(reader) == (audiosocket.KIND_HANGUP, b"")
    assert await audiosocket.read_message(reader) is None


def test_alignment_keeps_a_running_offset_across_messages():
    # Phase 0: times restart at 0 in every audio message.
    track = AlignmentTrack()
    track.add({"chars": list("Dobrý "), "char_start_times_ms": [0, 60, 120, 180, 240, 300]}, 360)
    track.add({"chars": list("deň."), "char_start_times_ms": [0, 80, 160, 240]}, 320)
    assert track.heard(0) == ""
    assert track.heard(200) == "Dobr"  # D, o, b, r start at 0/60/120/180 ms
    assert track.heard(400) == "Dobrý d"
    assert track.heard(10_000) == "Dobrý deň."
    assert track.audio_ms == 680


async def test_tts_stream_spells_numbers_flushes_once_at_the_end_and_sets_language():
    import json

    from voiceagent import config
    from voiceagent.eleven import TTSStream

    cfg = config.load({
        "OPENAI_API_KEY": "o", "ELEVENLABS_API_KEY": "e",
        "RENDER_API_BASE": "https://example.test", "VOICE_AGENT_TOKEN": "t",
        "ELEVENLABS_PRONUNCIATION_DICT_ID": "d1", "ELEVENLABS_PRONUNCIATION_DICT_VERSION": "v1",
    })

    class Socket:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            self.sent.append(json.loads(raw))

        async def close(self):
            pass

    stream = TTSStream(cfg, "voice-sk", "sk", lambda pcm: None)
    assert "language_code=sk" in stream._url() and "model_id=eleven_v4_turbo" in stream._url()
    assert stream._init_message()["pronunciation_dictionary_locators"] == [
        {"pronunciation_dictionary_id": "d1", "version_id": "v1"}
    ]
    sock = Socket()
    stream._ws = sock
    stream._open.set()
    for delta in ["Vaše číslo je 0903 1", "23 456. Ďaku", "jem"]:
        await stream.feed(delta)
    stream.done.set()
    await stream.finish()

    texts = [m["inputs"][0]["text"] for m in sock.sent if "inputs" in m]
    assert "".join(texts) == "Vaše číslo je nula deväť nula tri, jedna dva tri, štyri päť šesť. Ďakujem"
    # On this endpoint a flush ends the speech, so there is exactly one, and it
    # carries the end of the reply. A sentence end inside the reply doesn't flush.
    flushes = [m for m in sock.sent if m.get("flush")]
    assert len(flushes) == 1 and flushes[0] is sock.sent[-2]
    assert flushes[0]["inputs"][0]["text"] == "Ďakujem"
    assert sock.sent[-1] == {"close_socket": True}
    # Text arriving after the flush is not sent (it would never be spoken).
    await stream.feed("navyše")
    assert all("navyše" not in json.dumps(m) for m in sock.sent)


def test_alignment_without_timing_still_advances():
    track = AlignmentTrack()
    track.add(None, 500)
    track.add({"chars": ["A"], "char_start_times_ms": [0]}, 100)
    assert track.heard(499) == ""
    assert track.heard(501) == "A"

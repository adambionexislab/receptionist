"""A call end to end against fake OpenAI, ElevenLabs and Render."""

import asyncio
import json
import uuid

import pytest

from voiceagent import audiosocket, config
from voiceagent.callflow import Call
from voiceagent.eleven import AlignmentTrack

CFG = config.load({
    "OPENAI_API_KEY": "o", "ELEVENLABS_API_KEY": "e",
    "RENDER_API_BASE": "https://example.test", "VOICE_AGENT_TOKEN": "t",
    "ELEVENLABS_VOICE_ID_SK": "voice-sk",
})

ANSWER = {
    "decision": "eleven",
    "locale": "sk",
    "session": {"type": "realtime", "instructions": "prompt", "tools": [],
                "audio": {"input": {"turn_detection": {"type": "server_vad"}},
                          "output": {"voice": "marin"}}},
    "greeting_prompt": "Pozdrav volajúceho.",
    "farewell_response": {"type": "response.create",
                          "response": {"instructions": "Rozlúč sa.", "tool_choice": "none"}},
}

MS_PER_CHAR = 10  # fake speech: every character is 10 ms of audio


class FakeTTS:
    instances: list["FakeTTS"] = []

    def __init__(self, cfg, voice, language, on_audio):
        self.voice, self.language, self.on_audio = voice, language, on_audio
        self.alignment = AlignmentTrack()
        self.done = asyncio.Event()
        self.fed: list[str] = []
        self.closed = False
        self.failed = False
        FakeTTS.instances.append(self)

    async def open(self):
        pass

    async def feed(self, text):
        self.fed.append(text)
        # Each piece is its own audio message; times restart at 0 (Phase 0).
        self.alignment.add(
            {"chars": list(text), "char_start_times_ms": [i * MS_PER_CHAR for i in range(len(text))]},
            len(text) * MS_PER_CHAR,
        )
        self.on_audio(b"\x01\x00" * 8 * MS_PER_CHAR * len(text))

    async def finish(self):
        self.done.set()

    async def close(self):
        self.closed = True
        self.done.set()


class FakeOpenAI:
    """Records what the agent sends; the test pushes events with emit()."""

    def __init__(self):
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        await self.inbox.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)

    def types(self):
        return [e["type"] for e in self.sent]


class FakeWriter:
    def __init__(self):
        self.frames: list[tuple[int, bytes]] = []
        self.closed = False

    def write(self, data: bytes):
        while data:
            kind, length = data[0], int.from_bytes(data[1:3], "big")
            self.frames.append((kind, data[3:3 + length]))
            data = data[3 + length:]

    def close(self):
        self.closed = True


class FakeRender:
    def __init__(self):
        self.tools = []

    async def tool(self, call_uuid, name, arguments, timeout=10.0):
        self.tools.append((name, arguments))
        return json.dumps([{"address": "Hlavná 12"}])


class FakeReporter:
    def __init__(self):
        self.reports = []

    def submit(self, call_uuid, payload):
        self.reports.append((call_uuid, payload))


@pytest.fixture
async def world():
    FakeTTS.instances.clear()
    oai = FakeOpenAI()
    reader = asyncio.StreamReader()
    writer = FakeWriter()
    render = FakeRender()
    reporter = FakeReporter()

    async def oai_connect(url, **kwargs):
        assert "gpt-realtime-2.1" in url
        return oai

    async def start():
        call = Call(
            CFG, render, reporter, str(uuid.uuid4()),
            {"answer": ANSWER, "caller": "421903123456", "dialed": "421482304191", "diversion": ""},
            reader, writer, tts_factory=FakeTTS, oai_connect=oai_connect,
        )
        task = asyncio.create_task(call.run())
        await asyncio.sleep(0.01)
        return call, task

    return start, oai, reader, writer, render, reporter


async def say(oai, rid, item_id, text, *, done=True):
    """Model response `rid` writing `text` (as two deltas)."""
    await oai.inbox.put({"type": "response.created", "response": {"id": rid}})
    await oai.inbox.put({"type": "response.output_item.added", "response_id": rid,
                         "item": {"id": item_id, "type": "message"}})
    half = len(text) // 2
    for piece in (text[:half], text[half:]):
        await oai.inbox.put({"type": "response.output_text.delta", "response_id": rid, "delta": piece})
    if done:
        await oai.inbox.put({"type": "response.output_text.done", "response_id": rid})
        await oai.inbox.put({"type": "response.done", "response": {"id": rid, "status": "completed"}})
    await asyncio.sleep(0.05)


def audio_bytes(writer):
    return sum(len(p) for k, p in writer.frames if k == audiosocket.KIND_AUDIO)


async def test_session_and_greeting_are_set_up(world):
    start, oai, reader, writer, *_ = world
    call, task = await start()
    update = oai.sent[0]
    assert update["type"] == "session.update"
    assert update["session"]["output_modalities"] == ["text"]
    assert update["session"]["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert oai.sent[1]["item"]["content"][0]["text"] == "Pozdrav volajúceho."
    assert oai.sent[2] == {"type": "response.create"}
    reader.feed_eof()
    await task


async def test_caller_audio_goes_to_openai_as_ulaw(world):
    start, oai, reader, *_ = world
    call, task = await start()
    reader.feed_data(audiosocket.pack(audiosocket.KIND_AUDIO, b"\x00\x00" * 160))
    await asyncio.sleep(0.02)
    appended = [e for e in oai.sent if e["type"] == "input_audio_buffer.append"]
    assert len(appended) == 1
    import base64
    assert len(base64.b64decode(appended[0]["audio"])) == 160
    reader.feed_eof()
    await task


async def test_a_reply_is_spoken_spelled_out_and_played(world):
    start, oai, reader, writer, *_ = world
    call, task = await start()
    await say(oai, "r1", "i1", "Číslo 0903 123 456.")
    tts = FakeTTS.instances[0]
    assert tts.voice == "voice-sk" and tts.language == "sk"
    await asyncio.sleep(0.6)  # let the player write the audio out
    assert audio_bytes(writer) >= sum(len(t) for t in tts.fed) * 16 * MS_PER_CHAR - 320
    assert call.transcript[-1] == {"role": "apollonia", "text": "Číslo 0903 123 456."}
    reader.feed_eof()
    await task


async def test_barge_in_keeps_only_what_the_caller_heard(world):
    start, oai, reader, writer, *_ = world
    call, task = await start()
    text = "Dobrý deň, mám pre vás tri ponuky v Pezinku."
    await say(oai, "r1", "i1", text)
    await asyncio.sleep(0.1)  # ~100 ms played ≈ 10 characters
    played = call.replies[0].played_samples / 8
    await oai.inbox.put({"type": "input_audio_buffer.speech_started"})
    await asyncio.sleep(0.02)

    heard = text[: int(played // MS_PER_CHAR) + 1].strip()
    assert oai.sent[-2] == {"type": "conversation.item.delete", "item_id": "i1"}
    created = oai.sent[-1]
    assert created["item"]["role"] == "assistant"
    got = created["item"]["content"][0]["text"]
    assert text.startswith(got) and 0 < len(got) < len(text)
    assert abs(len(got) - len(heard)) <= 3
    assert FakeTTS.instances[0].closed
    assert not call.replies  # playback stopped
    before = audio_bytes(writer)
    await asyncio.sleep(0.1)
    assert audio_bytes(writer) == before
    reader.feed_eof()
    await task


async def test_barge_in_mid_response_replaces_the_item_once_the_response_ends(world):
    start, oai, reader, *_ = world
    call, task = await start()
    await say(oai, "r1", "i1", "Prvá ponuka je na Hlavnej ulici.", done=False)
    await oai.inbox.put({"type": "input_audio_buffer.speech_started"})
    await asyncio.sleep(0.02)
    assert "conversation.item.delete" not in oai.types()  # response still open
    await oai.inbox.put({"type": "response.done", "response": {"id": "r1", "status": "cancelled"}})
    await asyncio.sleep(0.02)
    assert {"type": "conversation.item.delete", "item_id": "i1"} in oai.sent
    reader.feed_eof()
    await task


async def test_a_tool_call_goes_to_render_and_its_output_back_to_the_model(world):
    start, oai, reader, writer, render, reporter = world
    call, task = await start()
    await oai.inbox.put({"type": "response.created", "response": {"id": "r1"}})
    await oai.inbox.put({"type": "response.function_call_arguments.done", "response_id": "r1",
                         "name": "search_listings", "call_id": "fc1", "arguments": '{"zone":"Pezinok"}'})
    await asyncio.sleep(0.02)
    assert render.tools == [("search_listings", '{"zone":"Pezinok"}')]
    assert "response.create" not in oai.types()[3:]  # held: the response is open
    await oai.inbox.put({"type": "response.done", "response": {"id": "r1", "status": "completed"}})
    await asyncio.sleep(0.02)
    outputs = [e for e in oai.sent if e.get("item", {}).get("type") == "function_call_output"]
    assert outputs[0]["item"]["call_id"] == "fc1"
    assert oai.sent[-1] == {"type": "response.create"}
    reader.feed_eof()
    await task
    assert reporter.reports[0][1]["tool_calls"] == [{"name": "search_listings", "arguments": '{"zone":"Pezinok"}'}]


async def test_end_call_plays_the_goodbye_then_hangs_up_and_reports(world):
    start, oai, reader, writer, render, reporter = world
    call, task = await start()
    await oai.inbox.put({"type": "response.created", "response": {"id": "r1"}})
    await oai.inbox.put({"type": "response.function_call_arguments.done", "response_id": "r1",
                         "name": "end_call", "call_id": "fc9", "arguments": "{}"})
    await oai.inbox.put({"type": "response.done", "response": {"id": "r1", "status": "completed"}})
    await asyncio.sleep(0.02)
    assert oai.sent[-1] == ANSWER["farewell_response"]

    await say(oai, "r2", "i2", "Ďakujem, dovidenia.")
    # Talking over the goodbye does not cut it.
    await oai.inbox.put({"type": "input_audio_buffer.speech_started"})
    await asyncio.wait_for(task, 3)

    assert writer.frames[-1] == (audiosocket.KIND_HANGUP, b"")
    assert audio_bytes(writer) > 0
    payload = reporter.reports[0][1]
    assert payload["ended_by"] == "apollonia"
    assert payload["from"] == "421903123456" and payload["to"] == "421482304191"
    assert payload["transcript"][-1]["text"] == "Ďakujem, dovidenia."


async def test_a_reply_elevenlabs_cannot_speak_hangs_up_instead_of_dead_air(world, monkeypatch):
    start, oai, reader, writer, render, reporter = world

    async def failing_open(self):
        self.failed = True
        self.done.set()

    async def silent_feed(self, text):
        pass

    monkeypatch.setattr(FakeTTS, "open", failing_open)
    monkeypatch.setattr(FakeTTS, "feed", silent_feed)
    call, task = await start()
    await say(oai, "r1", "i1", "Dobrý deň.")
    await asyncio.wait_for(task, 2)
    assert reporter.reports[0][1]["ended_by"] == "tts_failed"
    assert writer.frames[-1] == (audiosocket.KIND_HANGUP, b"")


async def test_caller_hangup_ends_and_reports(world):
    start, oai, reader, writer, render, reporter = world
    call, task = await start()
    reader.feed_data(audiosocket.pack(audiosocket.KIND_HANGUP))
    await asyncio.wait_for(task, 2)
    assert reporter.reports[0][1]["ended_by"] == "caller"
    assert writer.closed

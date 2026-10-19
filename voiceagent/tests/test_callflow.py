"""A call end to end against fake OpenAI, ElevenLabs and Render.

ElevenLabs is faked at the WebSocket, under the real TTSStream, and behaves
like the real Text-to-Dialogue endpoint as observed (Phase 0, first VM calls):
a flush speaks the text received so far and ends the speech; text sent after
it on the same stream is never spoken. The first version of this fake spoke
everything, which is how sentence-end flushes shipped and cut every reply
after its first sentence.
"""

import asyncio
import base64
import json
import re
import uuid

import pytest

from voiceagent import audiosocket, config, eleven
from voiceagent.callflow import Call
from voiceagent.eleven import TTSStream

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
BYTES_PER_MS = 16  # 8 kHz, 16-bit


class FakeElevenLabs:
    """One Text-to-Dialogue WebSocket, as the real endpoint behaves."""

    instances: list["FakeElevenLabs"] = []
    mode = "normal"  # "normal" | "silent" (never sends a final message or closes)

    def __init__(self, url: str):
        self.url = url
        self.init: dict | None = None
        self.pending = ""        # text received, not yet spoken
        self.spoken = ""         # text that became audio
        self.ignored = ""        # text sent after the flush: never spoken
        self.flushed = False
        self.outbox: asyncio.Queue = asyncio.Queue()
        FakeElevenLabs.instances.append(self)

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        if self.init is None:
            self.init = msg
            return
        for item in msg.get("inputs") or []:
            if self.flushed:
                self.ignored += item["text"]
            else:
                self.pending += item["text"]
        if msg.get("flush") and not self.flushed:
            self.flushed = True
            self._speak(self.pending)
            self.spoken, self.pending = self.pending, ""
            if self.mode == "normal":
                await self.outbox.put({"is_final_audio_for_turn": True})
        if msg.get("close_socket") and self.mode == "normal":
            await self.outbox.put({"is_final": True})
            await self.outbox.put(None)

    def _speak(self, text: str) -> None:
        for i in range(0, len(text), 12):  # several audio messages; times restart at 0
            piece = text[i:i + 12]
            self.outbox.put_nowait({
                "audio": base64.b64encode(b"\x01\x00" * 8 * MS_PER_CHAR * len(piece)).decode(),
                "alignment": {
                    "chars": list(piece),
                    "char_start_times_ms": [j * MS_PER_CHAR for j in range(len(piece))],
                    "char_durations_ms": [MS_PER_CHAR] * len(piece),
                },
            })

    async def close(self) -> None:
        await self.outbox.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.outbox.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)


async def fake_connect(url, **kwargs):
    return FakeElevenLabs(url)


def tts_factory(cfg, voice, language, on_audio, **kwargs):
    return TTSStream(cfg, voice, language, on_audio, connector=fake_connect, **kwargs)


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
async def world(monkeypatch):
    FakeElevenLabs.instances.clear()
    FakeElevenLabs.mode = "normal"
    monkeypatch.setattr(eleven, "QUIET_AFTER_FLUSH_SECONDS", 0.3)
    oai = FakeOpenAI()
    reader = asyncio.StreamReader()
    writer = FakeWriter()
    render = FakeRender()
    reporter = FakeReporter()

    async def oai_connect(url, **kwargs):
        assert "gpt-realtime-2.1" in url
        return oai

    async def start(factory=tts_factory):
        call = Call(
            CFG, render, reporter, str(uuid.uuid4()),
            {"answer": ANSWER, "caller": "421903123456", "dialed": "421482304191", "diversion": ""},
            reader, writer, tts_factory=factory, oai_connect=oai_connect,
        )
        task = asyncio.create_task(call.run())
        await asyncio.sleep(0.01)
        return call, task

    return start, oai, reader, writer, render, reporter


async def say(oai, rid, item_id, text, *, done=True):
    """Model response `rid` writing `text` word by word, as gpt-realtime does:
    a delta often ends exactly at a sentence end ("Test. ")."""
    await oai.inbox.put({"type": "response.created", "response": {"id": rid}})
    await oai.inbox.put({"type": "response.output_item.added", "response_id": rid,
                         "item": {"id": item_id, "type": "message"}})
    for word in re.findall(r"\S+\s*", text):
        await oai.inbox.put({"type": "response.output_text.delta", "response_id": rid,
                             "delta": word})
    if done:
        await oai.inbox.put({"type": "response.output_text.done", "response_id": rid})
        await oai.inbox.put({"type": "response.done", "response": {"id": rid, "status": "completed"}})
    await asyncio.sleep(0.05)


async def caller_turn(oai):
    await oai.inbox.put({"type": "input_audio_buffer.speech_started"})
    await asyncio.sleep(0.02)
    await oai.inbox.put({"type": "input_audio_buffer.speech_stopped"})


def audio_bytes(writer):
    return sum(len(p) for k, p in writer.frames if k == audiosocket.KIND_AUDIO)


async def wait_for(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


# ── the bug from the first VM calls (2026-10-19) ─────────────────────────────
async def test_a_two_sentence_greeting_is_spoken_whole_and_the_caller_gets_an_answer(world):
    start, oai, reader, writer, *_ = world
    call, task = await start()

    greeting = "Dobrý deň, som virtuálna asistentka Apollonia Voice Test. Ako vám môžem pomôcť?"
    await say(oai, "r1", "i1", greeting)
    first = FakeElevenLabs.instances[0]
    # One flush, at the end of the reply: nothing is left unspoken.
    assert first.spoken == greeting and first.ignored == ""
    await wait_for(lambda: call.transcript and call.transcript[-1]["text"] == greeting)
    assert audio_bytes(writer) >= len(greeting) * MS_PER_CHAR * BYTES_PER_MS - 320

    # The caller speaks; the server's response must be spoken and played.
    played_before = audio_bytes(writer)
    await caller_turn(oai)
    await say(oai, "r2", "i2", "Áno, rozumiem. Čo hľadáte?")
    await wait_for(lambda: call.transcript[-1]["text"] == "Áno, rozumiem. Čo hľadáte?")
    assert FakeElevenLabs.instances[1].spoken == "Áno, rozumiem. Čo hľadáte?"
    assert audio_bytes(writer) > played_before
    reader.feed_eof()
    await task


async def test_a_stream_that_goes_quiet_without_a_final_still_ends_the_turn(world):
    start, oai, reader, writer, *_ = world
    FakeElevenLabs.mode = "silent"  # audio, then nothing: no final, no close
    call, task = await start()
    await say(oai, "r1", "i1", "Dobrý deň.")
    await wait_for(lambda: call.transcript and call.transcript[-1]["text"] == "Dobrý deň.")
    assert FakeElevenLabs.instances[0].spoken == "Dobrý deň."

    FakeElevenLabs.mode = "normal"
    await caller_turn(oai)
    await say(oai, "r2", "i2", "Áno?")
    await wait_for(lambda: call.transcript[-1]["text"] == "Áno?")
    reader.feed_eof()
    await task


# ── the rest of a call ───────────────────────────────────────────────────────
async def test_session_and_greeting_are_set_up(world):
    start, oai, reader, writer, *_ = world
    call, task = await start()
    update = oai.sent[0]
    assert update["type"] == "session.update"
    assert update["session"]["output_modalities"] == ["text"]
    assert update["session"]["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert "transcription" not in update["session"]["audio"]["input"]  # INFO: off
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
    assert len(base64.b64decode(appended[0]["audio"])) == 160
    reader.feed_eof()
    await task


async def test_numbers_are_spelled_and_the_stream_is_slovak(world):
    start, oai, reader, writer, *_ = world
    call, task = await start()
    await say(oai, "r1", "i1", "Číslo 0903 123 456.")
    el = FakeElevenLabs.instances[0]
    assert "language_code=sk" in el.url and "model_id=eleven_v4_turbo" in el.url
    assert el.init == {"voices": ["voice-sk"]}
    assert el.spoken == "Číslo nula deväť nula tri, jedna dva tri, štyri päť šesť."
    await wait_for(lambda: call.transcript and call.transcript[-1]["text"] == "Číslo 0903 123 456.")
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
    # The tool-only response's stream is closed without speaking.
    assert FakeElevenLabs.instances[0].spoken == "" and not FakeElevenLabs.instances[0].flushed
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


async def test_a_reply_elevenlabs_cannot_speak_hangs_up_instead_of_dead_air(world):
    start, oai, reader, writer, render, reporter = world

    async def refused(url, **kwargs):
        raise OSError("connection refused")

    def failing_factory(cfg, voice, language, on_audio, **kwargs):
        return TTSStream(cfg, voice, language, on_audio, connector=refused, **kwargs)

    call, task = await start(failing_factory)
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

"""One call: AudioSocket <-> gpt-realtime (text out) <-> ElevenLabs <-> AudioSocket.

The behaviour mirrors the OpenAI SIP call loop (backend/call/router.py,
_run_call): greeting on connect, tool calls through Render, end_call → a
farewell response → hang up once the goodbye has played, the reply nudge, a
100 s silence hang-up. What is new is that the agent owns the audio:

  - every model response gets its own ElevenLabs stream (Reply);
  - replies play in order, 20 ms at a time, paced in real time;
  - when the caller talks over her (speech_started), playback stops and the
    assistant item in the conversation is replaced with only what the caller
    actually heard, worked out from ElevenLabs' alignment — otherwise she
    would believe she said things the caller never heard (roadmap §4.2.5).
"""

import asyncio
import base64
import datetime
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from voiceagent import audiosocket, g711
from voiceagent.config import Config
from voiceagent.eleven import TTSStream
from voiceagent.render_api import RenderAPI, RenderError
from voiceagent.reporting import EndReporter
from voiceagent.turns import (
    FAREWELL_TIMEOUT_SECONDS,
    SILENCE_HANGUP_SECONDS,
    ResponseGate,
    should_nudge_reply,
)

logger = logging.getLogger(__name__)

_FINISH = object()  # end-of-text marker in a reply's feed queue
# Pause after the goodbye has played before hanging up, so its last syllable
# isn't clipped by the hang-up.
_AFTER_FAREWELL_SECONDS = 0.4
# Safety nets, so she can never get stuck silent:
# a response that never reports response.done stops holding the gate after this;
_RESPONSE_STUCK_SECONDS = 60.0
# a reply whose audio never completes stops blocking playback after this
# (TTSStream gives up after MAX_AFTER_FLUSH_SECONDS on its own; this is the
# backstop if even that doesn't happen).
_REPLY_STUCK_SECONDS = 45.0


def realtime_session(session: dict[str, Any], transcribe_model: str = "") -> dict[str, Any]:
    """Render's OpenAI SIP accept config, made into the agent's session: the
    same prompt, tools, reasoning and turn detection, but text out, and the
    caller's audio in as G.711 μ-law (what OpenAI gets over SIP today, D3).

    transcribe_model is for debugging only (LOG_LEVEL=DEBUG): it adds input
    transcription so the log shows what the caller said. Production leaves it
    off, like the SIP path (a second, paid ASR pass whose only reader is the log)."""
    s = json.loads(json.dumps(session))
    s["type"] = "realtime"
    s["output_modalities"] = ["text"]
    audio_in = dict((s.get("audio") or {}).get("input") or {})
    audio_in["format"] = {"type": "audio/pcmu"}
    if transcribe_model:
        audio_in["transcription"] = {"model": transcribe_model}
    # Text out: the output audio block (voice "marin") no longer applies.
    s["audio"] = {"input": audio_in}
    return s


@dataclass
class Reply:
    """One model response and its speech."""

    response_id: str
    tts: TTSStream | None = None
    item_id: str | None = None
    text: str = ""
    buffer: bytearray = field(default_factory=bytearray)
    played_samples: int = 0
    response_done: bool = False
    feed_closed: bool = False
    cancelled: bool = False
    replaced: bool = False
    heard: str | None = None  # set on barge-in
    is_farewell: bool = False
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    feeder: asyncio.Task | None = None
    done_at: float | None = None  # loop time of response.done

    @property
    def finished(self) -> bool:
        """Nothing left to play, and nothing more will come."""
        audio_complete = self.tts is None or self.tts.done.is_set()
        return self.response_done and self.feed_closed and audio_complete and not self.buffer


class Call:
    def __init__(
        self,
        cfg: Config,
        render: RenderAPI,
        reporter: EndReporter,
        call_uuid: str,
        start: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        tts_factory=TTSStream,
        oai_connect=connect,
    ) -> None:
        self.cfg = cfg
        self.render = render
        self.reporter = reporter
        self.uuid = call_uuid
        self.answer = start["answer"]
        self.caller = start.get("caller", "")
        self.dialed = start.get("dialed", "")
        self.diversion = start.get("diversion", "")
        self.reader = reader
        self.writer = writer
        self.tts_factory = tts_factory
        self.oai_connect = oai_connect

        self.locale = self.answer.get("locale") or cfg.elevenlabs_language_code
        self.voice = cfg.voice_for(self.locale)
        self.loop = asyncio.get_running_loop()
        now = self.loop.time()
        self.state: dict[str, Any] = {
            "awaiting_reply_since": None,
            "ending_at": None,
            "last_speech_at": now,
        }
        self.started_mono = now
        self.started_at = datetime.datetime.now(datetime.timezone.utc)
        self.replies: deque[Reply] = deque()
        self.by_response: dict[str, Reply] = {}
        self.tool_calls: list[dict[str, Any]] = []
        self.transcript: list[dict[str, Any]] = []
        self.ended_by: str | None = None
        self.stop = asyncio.Event()
        self._farewell_next = False
        self._ws = None
        self.gate = ResponseGate(self._oai_send)
        self._bg: set[asyncio.Task] = set()
        self.response_open_since: float | None = None
        self.tag = call_uuid[:8]
        self.debug = logger.isEnabledFor(logging.DEBUG)

    def _dbg(self, fmt: str, *args: Any) -> None:
        if self.debug:
            logger.debug("Call %s +%.2fs: " + fmt, self.tag, self.loop.time() - self.started_mono, *args)

    # ── plumbing ─────────────────────────────────────────────────────────────
    async def _oai_send(self, event: dict[str, Any]) -> None:
        if self._ws is None:
            return
        if self.debug and event.get("type") != "input_audio_buffer.append":
            self._dbg("→ openai %s", event.get("type"))
        try:
            await self._ws.send(json.dumps(event, ensure_ascii=False))
        except ConnectionClosed:
            self._end("openai_closed")

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    def _end(self, reason: str) -> None:
        if self.ended_by is None:
            self.ended_by = reason
        self.stop.set()

    def hang_up(self, reason: str) -> None:
        """Hang up from our side: tell Asterisk, then wind the call down."""
        if self.ended_by is None:
            logger.info("Call %s: hanging up (%s)", self.uuid, reason)
            try:
                self.writer.write(audiosocket.pack(audiosocket.KIND_HANGUP))
            except Exception:
                pass
        self._end(reason)

    # ── run ──────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        logger.info("Call %s: answered (locale=%s)", self.uuid, self.locale)
        try:
            self._ws = await self.oai_connect(
                f"wss://api.openai.com/v1/realtime?model={self.cfg.openai_realtime_model}",
                additional_headers={"Authorization": f"Bearer {self.cfg.openai_api_key}"},
                max_size=None,
            )
        except Exception:
            logger.exception("Call %s: could not reach OpenAI", self.uuid)
            self.hang_up("openai_unreachable")
            await self._finish()
            return

        await self._oai_send({
            "type": "session.update",
            "session": realtime_session(
                self.answer["session"],
                self.cfg.debug_transcribe_model if self.debug else "",
            ),
        })
        # Same greeting trigger as the SIP path: a user turn with the locale's
        # greeting prompt, then a response.
        await self._oai_send({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": self.answer["greeting_prompt"]}],
            },
        })
        await self.gate.request()

        tasks = [
            asyncio.create_task(self._caller_audio()),
            asyncio.create_task(self._oai_events()),
            asyncio.create_task(self._player()),
            asyncio.create_task(self._watchdog()),
            asyncio.create_task(self.stop.wait()),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._finish()

    async def _finish(self) -> None:
        for reply in list(self.by_response.values()):
            if reply.tts is not None:
                await reply.tts.close()
            if reply.feeder is not None:
                reply.feeder.cancel()
        for task in list(self._bg):
            task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        try:
            self.writer.close()
        except Exception:
            pass
        payload = {
            "started_at": self.started_at.isoformat(),
            "ended_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "ended_by": self.ended_by or "unknown",
            "from": self.caller,
            "to": self.dialed,
            "diversion": self.diversion,
            "tool_calls": self.tool_calls,
            "transcript": self.transcript,
        }
        logger.info("Call %s: ended (%s), %d tool calls", self.uuid, payload["ended_by"], len(self.tool_calls))
        self.reporter.submit(self.uuid, payload)

    # ── caller → OpenAI ──────────────────────────────────────────────────────
    async def _caller_audio(self) -> None:
        frames = 0
        while True:
            msg = await audiosocket.read_message(self.reader)
            if msg is None:
                self._end("caller")
                return
            kind, payload = msg
            if kind == audiosocket.KIND_AUDIO:
                frames += 1
                if frames == 1 or frames % 250 == 0:  # first frame, then every ~5 s
                    self._dbg("caller audio: %d frames (%d bytes in the last)", frames, len(payload))
                await self._oai_send({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(g711.pcm16_to_ulaw(payload)).decode(),
                })
            elif kind == audiosocket.KIND_HANGUP:
                self._end("caller")
                return
            elif kind == audiosocket.KIND_ERROR:
                logger.warning("Call %s: AudioSocket error %r", self.uuid, payload)

    # ── OpenAI events ────────────────────────────────────────────────────────
    async def _oai_events(self) -> None:
        try:
            async for raw in self._ws:
                await self.handle_event(json.loads(raw))
        except ConnectionClosed:
            pass
        self._end(self.ended_by or "openai_closed")

    async def handle_event(self, msg: dict[str, Any]) -> None:
        etype = msg.get("type")
        now = self.loop.time()

        if etype == "response.created":
            self.gate.opened()
            self.response_open_since = now
            rid = (msg.get("response") or {}).get("id") or f"r{len(self.by_response)}"
            reply = Reply(response_id=rid, is_farewell=self._farewell_next)
            if self._farewell_next:
                self._farewell_next = False
                self.state["ending"] = True
            self.by_response[rid] = reply
            self.replies.append(reply)
            self._dbg("response.created %s%s (gate open, %d reply(s) queued)",
                      rid, " [farewell]" if reply.is_farewell else "", len(self.replies))
            # Open the speech stream now, while the model starts writing: it
            # hides the connection time (Phase 0).
            reply.tts = self.tts_factory(
                self.cfg, self.voice, self.locale, reply.buffer.extend,
                label=f"Call {self.tag} {rid[-8:]}",
            )
            self._spawn(reply.tts.open())
            reply.feeder = self._spawn(self._feed(reply))

        elif etype == "response.output_item.added":
            item = msg.get("item") or {}
            reply = self.by_response.get(msg.get("response_id"))
            if reply is not None and item.get("type") == "message":
                reply.item_id = item.get("id")

        elif etype == "response.output_text.delta":
            reply = self.by_response.get(msg.get("response_id"))
            delta = msg.get("delta") or ""
            if reply is not None and not reply.cancelled and delta:
                reply.text += delta
                reply.queue.put_nowait(delta)

        elif etype == "response.output_text.done":
            reply = self.by_response.get(msg.get("response_id"))
            if reply is not None:
                self._dbg("text done %s: %r", reply.response_id, reply.text)
                reply.queue.put_nowait(_FINISH)

        elif etype == "response.done":
            resp = msg.get("response") or {}
            reply = self.by_response.get(resp.get("id"))
            status = resp.get("status")
            await self.gate.closed()
            self.response_open_since = None
            self._dbg("response.done %s status=%s (gate closed)", resp.get("id"), status)
            if reply is not None:
                reply.response_done = True
                reply.done_at = now
                reply.queue.put_nowait(_FINISH)  # no-op if text already finished
                if reply.heard is not None and not reply.replaced:
                    await self._replace_item(reply)
            if status not in (None, "completed", "cancelled"):
                logger.warning("Call %s: response %s: %s", self.uuid, status, resp.get("status_details"))

        elif etype == "conversation.item.input_audio_transcription.completed":
            # Only when LOG_LEVEL=DEBUG turned transcription on.
            self._dbg("caller said: %r", msg.get("transcript"))

        elif etype == "response.function_call_arguments.done":
            name = msg.get("name")
            if name == "end_call":
                await self._end_call(msg.get("call_id"))
            elif name:
                self._spawn(self._run_tool(name, msg.get("call_id"), msg.get("arguments")))

        elif etype == "input_audio_buffer.speech_started":
            self._dbg("caller speech started (gate %s, %d reply(s) queued)",
                      "open" if self.gate.active else "closed", len(self.replies))
            self.state["last_speech_at"] = now
            self.state["awaiting_reply_since"] = None
            await self.barge_in()

        elif etype == "input_audio_buffer.speech_stopped":
            self._dbg("caller speech stopped")
            self.state["awaiting_reply_since"] = now

        elif etype in ("session.created", "session.updated"):
            self._dbg("%s", etype)

        elif etype == "error":
            err = msg.get("error") or {}
            logger.error(
                "Call %s: OpenAI error [%s/%s]: %s", self.uuid, err.get("type"),
                err.get("code"), err.get("message"),
            )

    async def _feed(self, reply: Reply) -> None:
        """Text deltas → this reply's speech stream, in order."""
        while True:
            item = await reply.queue.get()
            if item is _FINISH:
                break
            if reply.tts is not None and not reply.cancelled:
                await reply.tts.feed(item)
        reply.feed_closed = True
        if reply.tts is not None and not reply.cancelled:
            if reply.text:
                await reply.tts.finish()
            else:
                await reply.tts.close()  # a tool-call-only response: no speech

    # ── tools and ending ─────────────────────────────────────────────────────
    async def _run_tool(self, name: str, fc_id: str | None, arguments: str | None) -> None:
        self.tool_calls.append({"name": name, "arguments": arguments})
        try:
            output = await self.render.tool(self.uuid, name, arguments)
        except RenderError as exc:
            logger.error("Call %s: tool %s failed: %s", self.uuid, name, exc)
            output = json.dumps({"error": "temporarily unavailable"})
        if output is None:
            return
        await self._oai_send({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": fc_id, "output": output},
        })
        await self.gate.request()

    async def _end_call(self, fc_id: str | None) -> None:
        logger.info("Call %s: Apollonia ends the call", self.uuid)
        await self._oai_send({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": fc_id,
                     "output": json.dumps({"ended": True})},
        })
        self.state["ending_at"] = self.loop.time()

        def farewell_sent() -> None:
            self._farewell_next = True

        await self.gate.request(self.answer["farewell_response"], on_sent=farewell_sent)

    # ── barge-in ─────────────────────────────────────────────────────────────
    async def barge_in(self) -> None:
        """The caller started talking: stop her, and keep in the conversation
        only what the caller heard. The goodbye is never cut."""
        if self.state.get("ending") or not self.replies:
            return
        for i, reply in enumerate(list(self.replies)):
            played_ms = reply.played_samples / 8 if i == 0 else 0.0
            reply.heard = reply.tts.alignment.heard(played_ms) if reply.tts else ""
            reply.cancelled = True
            reply.buffer.clear()
            reply.queue.put_nowait(_FINISH)
            if reply.tts is not None:
                self._spawn(reply.tts.close())
            if reply.response_done:
                await self._replace_item(reply)
        self.replies.clear()
        logger.info("Call %s: caller interrupted", self.uuid)

    async def _replace_item(self, reply: Reply) -> None:
        reply.replaced = True
        heard = reply.heard or ""
        if reply.text or heard:
            self.transcript.append({"role": "apollonia", "text": heard, "interrupted": True})
        if not reply.item_id:
            return
        await self._oai_send({"type": "conversation.item.delete", "item_id": reply.item_id})
        if heard:
            await self._oai_send({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": heard}],
                },
            })

    # ── her audio → caller ───────────────────────────────────────────────────
    async def _player(self) -> None:
        """Write her audio to Asterisk in 20 ms frames, paced in real time."""
        frame_s = audiosocket.FRAME_MS / 1000
        next_at = self.loop.time()
        while True:
            next_at += frame_s
            await asyncio.sleep(max(0.0, next_at - self.loop.time()))
            if self.loop.time() - next_at > 0.2:  # we fell behind: don't burst
                next_at = self.loop.time()
            reply = self.replies[0] if self.replies else None
            if reply is None:
                continue
            if reply.buffer:
                chunk = bytes(reply.buffer[: audiosocket.FRAME_BYTES])
                del reply.buffer[: len(chunk)]
                if len(chunk) < audiosocket.FRAME_BYTES:
                    audio_complete = reply.tts is None or reply.tts.done.is_set()
                    if not audio_complete:  # more is coming: wait for a full frame
                        reply.buffer[:0] = chunk
                        continue
                    chunk += b"\x00" * (audiosocket.FRAME_BYTES - len(chunk))
                self.writer.write(audiosocket.pack(audiosocket.KIND_AUDIO, chunk))
                reply.played_samples += len(chunk) // 2
                self.state["last_speech_at"] = self.loop.time()
            elif (
                reply.done_at is not None
                and not reply.finished
                and self.loop.time() - reply.done_at > _REPLY_STUCK_SECONDS
            ):
                # Backstop: this reply's audio never completed. Don't let it
                # block every reply after it.
                logger.warning("Call %s: reply %s stuck — dropping it", self.uuid, reply.response_id)
                reply.feed_closed = True
                if reply.tts is not None:
                    self._spawn(reply.tts.close())
                reply.done_at = None
            elif reply.finished:
                self.replies.popleft()
                self._dbg(
                    "reply %s played: %d bytes (%.1fs), stream end: %s",
                    reply.response_id, reply.played_samples * 2, reply.played_samples / 8000,
                    getattr(reply.tts, "end_reason", "") or "-",
                )
                if reply.text and reply.tts is not None and reply.tts.failed and not reply.cancelled:
                    # ElevenLabs could not speak this reply: the caller would
                    # sit in dead air. Hang up rather than leave them there
                    # (roadmap §6: a recorded apology is still to be added).
                    logger.error("Call %s: speech failed for a reply — hanging up", self.uuid)
                    self.hang_up("tts_failed")
                    return
                if reply.text and not reply.cancelled:
                    self.transcript.append({"role": "apollonia", "text": reply.text})
                    logger.info("Call %s: Apollonia: %s", self.uuid, reply.text.strip())
                    # She answered: nothing is owed to the caller.
                    self.state["awaiting_reply_since"] = None
                if reply.is_farewell:
                    await asyncio.sleep(_AFTER_FAREWELL_SECONDS)
                    self.hang_up("apollonia")
                    return

    # ── timers ───────────────────────────────────────────────────────────────
    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(1)
            now = self.loop.time()
            ending_at = self.state.get("ending_at")
            if ending_at and now - ending_at > FAREWELL_TIMEOUT_SECONDS + 15:
                # The goodbye never finished playing: don't hold the line open.
                self.hang_up("farewell_timeout")
                return
            if ending_at and not self.state.get("ending") and now - ending_at > FAREWELL_TIMEOUT_SECONDS:
                self.hang_up("farewell_timeout")
                return
            if (
                self.response_open_since is not None
                and now - self.response_open_since > _RESPONSE_STUCK_SECONDS
            ):
                # response.done never came: don't hold every later turn behind it.
                logger.warning("Call %s: response open for %.0fs without response.done — releasing the gate",
                               self.uuid, now - self.response_open_since)
                self.response_open_since = None
                await self.gate.closed()
            if should_nudge_reply(self.state, self.gate.active, now):
                logger.warning("Call %s: no reply after the caller finished — requesting one", self.uuid)
                self.state["awaiting_reply_since"] = None
                await self.gate.request()
            if now - self.state["last_speech_at"] > SILENCE_HANGUP_SECONDS:
                self.hang_up("silence")
                return
            if now - self.started_mono > self.cfg.max_call_seconds:
                self.hang_up("max_duration")
                return

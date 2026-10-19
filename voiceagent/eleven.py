"""ElevenLabs Text-to-Dialogue streaming, one stream per reply.

Decisions from Phase 0 (elevenlabs-voice-roadmap.md), all applied here:
  - eleven_v4_turbo only runs on the Text-to-Dialogue WebSocket.
  - language_code (sk) on every stream, and digits spelled out as Slovak words
    first (sk_numbers): together they stop the drift into Czech.
  - flush only at sentence ends and at the end of the reply; an early flush
    after a few words truncated audio.
  - alignment times restart at 0 in every audio message, so AlignmentTrack
    keeps a running offset to map "ms played" to "characters heard".
  - a close with code 1000 is the normal end of a stream.
  - the first connection of a process is slow (~900 ms vs ~90 ms), so the
    agent warms it at startup (warm()).
"""

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable
from urllib.parse import urlencode

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from voiceagent import g711
from voiceagent.config import Config
from voiceagent.sk_numbers import SlovakNumberStream

logger = logging.getLogger(__name__)

SENTENCE_END = (".", "?", "!", "…")
_OPEN_ATTEMPTS = 3


class AlignmentTrack:
    """Which characters of a reply had been spoken after N ms of its audio."""

    def __init__(self) -> None:
        self._chars: list[tuple[float, str]] = []  # (absolute start ms, char)
        self._offset_ms = 0.0

    def add(self, alignment: dict | None, audio_ms: float) -> None:
        if alignment:
            chars = alignment.get("chars") or []
            starts = alignment.get("char_start_times_ms") or alignment.get("charStartTimesMs") or []
            for ch, start in zip(chars, starts):
                self._chars.append((self._offset_ms + float(start), ch))
        # Per-message times restart at 0: the next message starts where this
        # message's audio ended.
        self._offset_ms += audio_ms

    def heard(self, played_ms: float) -> str:
        return "".join(ch for start, ch in self._chars if start < played_ms).strip()

    @property
    def audio_ms(self) -> float:
        return self._offset_ms


def _close_code(exc: ConnectionClosed) -> int | None:
    for frame in (exc.rcvd, exc.sent):
        if frame is not None:
            return frame.code
    return None


class TTSStream:
    """One reply's speech. Text goes in as gpt-realtime writes it; 8 kHz PCM
    comes out through on_audio(pcm16) as ElevenLabs produces it."""

    def __init__(
        self,
        cfg: Config,
        voice_id: str,
        language: str,
        on_audio: Callable[[bytes], None],
    ) -> None:
        self.cfg = cfg
        self.voice_id = voice_id
        self.language = language
        self.on_audio = on_audio
        self.alignment = AlignmentTrack()
        self.sent: list[str] = []
        self.done = asyncio.Event()  # all audio received (or the stream failed)
        self.failed = False
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._open = asyncio.Event()
        self._numbers = SlovakNumberStream() if language == "sk" else None
        self._closed = False

    def _url(self) -> str:
        query = {
            "model_id": self.cfg.elevenlabs_model,
            "output_format": self.cfg.elevenlabs_output_format,
            "sync_alignment": "true",
        }
        if self.language:
            query["language_code"] = self.language
        return f"{self.cfg.elevenlabs_ws_url}?{urlencode(query)}"

    def _init_message(self) -> dict:
        init: dict = {"voices": [self.voice_id]}
        settings = {
            k: v
            for k, v in (
                ("stability", self.cfg.stability),
                ("speed", self.cfg.speed),
                ("similarity_boost", self.cfg.similarity_boost),
            )
            if v is not None
        }
        if settings:
            init["voice_settings"] = settings
        if self.cfg.pronunciation_dict_id and self.cfg.pronunciation_dict_version:
            init["pronunciation_dictionary_locators"] = [{
                "pronunciation_dictionary_id": self.cfg.pronunciation_dict_id,
                "version_id": self.cfg.pronunciation_dict_version,
            }]
        return init

    async def open(self) -> None:
        """Connect, retrying a concurrency 429 briefly: the plan's limit counts
        streams that are generating, so a slot frees up within moments."""
        delay = 0.2
        for attempt in range(1, _OPEN_ATTEMPTS + 1):
            try:
                self._ws = await connect(
                    self._url(),
                    additional_headers={"xi-api-key": self.cfg.elevenlabs_api_key},
                    max_size=None,
                    open_timeout=5,
                )
                break
            except InvalidStatus as exc:
                status = exc.response.status_code
                if status == 429 and attempt < _OPEN_ATTEMPTS:
                    logger.warning("ElevenLabs 429 (concurrency) — retry in %.1fs", delay)
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                self._fail(f"open failed: HTTP {status}")
                return
            except Exception as exc:
                self._fail(f"open failed: {exc!r}")
                return
        await self._ws.send(json.dumps(self._init_message()))
        self._reader = asyncio.create_task(self._read())
        self._open.set()

    def _fail(self, why: str) -> None:
        logger.error("ElevenLabs stream: %s", why)
        self.failed = True
        self._open.set()
        self.done.set()

    async def _read(self) -> None:
        fmt = self.cfg.elevenlabs_output_format
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if msg.get("error") or (msg.get("code") and not msg.get("audio")):
                    logger.error("ElevenLabs error: %s", msg)
                if msg.get("audio"):
                    audio = base64.b64decode(msg["audio"])
                    pcm = g711.ulaw_to_pcm16(audio) if fmt.startswith("ulaw") else audio
                    self.alignment.add(msg.get("alignment"), len(pcm) / 16)  # 16 bytes per ms
                    self.on_audio(pcm)
                if msg.get("is_final_audio_for_turn") or msg.get("is_final"):
                    break
        except ConnectionClosed as exc:
            if _close_code(exc) != 1000:
                logger.warning("ElevenLabs stream closed: %s", exc)
        except Exception:
            logger.exception("ElevenLabs reader failed")
        finally:
            self.done.set()

    async def _send(self, payload: dict) -> None:
        if self._ws is None or self.failed:
            return
        try:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
        except ConnectionClosed:
            pass

    async def feed(self, text: str) -> None:
        """A text delta from gpt-realtime. Flushes at a sentence end."""
        await self._open.wait()
        if self._numbers is not None:
            text = self._numbers.feed(text)
        if not text:
            return
        self.sent.append(text)
        payload: dict = {"inputs": [{"text": text, "voice_id": self.voice_id}]}
        if text.rstrip().endswith(SENTENCE_END):
            payload["flush"] = True
        await self._send(payload)

    async def finish(self, timeout: float = 15.0) -> None:
        """The reply's text is complete: flush the rest and wait for its audio."""
        await self._open.wait()
        rest = self._numbers.finish() if self._numbers is not None else ""
        if rest:
            self.sent.append(rest)
            await self._send({"inputs": [{"text": rest, "voice_id": self.voice_id}], "flush": True})
        else:
            await self._send({"flush": True})
        await self._send({"close_socket": True})
        try:
            await asyncio.wait_for(self.done.wait(), timeout)
        except asyncio.TimeoutError:
            logger.warning("ElevenLabs: no final audio within %.0fs", timeout)
        await self.close()

    async def close(self) -> None:
        """Stop now (barge-in, call over). Audio still in flight is dropped."""
        if self._closed:
            return
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._reader is not None:
            self._reader.cancel()
        self.done.set()


async def warm(cfg: Config, voice_id: str) -> float:
    """Open and close one stream so the first real call doesn't pay the cold
    connection (~900 ms in Phase 0). Returns the connect time in ms."""
    t0 = time.perf_counter()
    stream = TTSStream(cfg, voice_id, cfg.elevenlabs_language_code, lambda _pcm: None)
    await stream.open()
    await stream.close()
    return (time.perf_counter() - t0) * 1000

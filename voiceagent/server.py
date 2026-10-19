"""The agent's two loopback listeners.

127.0.0.1:9093 (HTTP): the dialplan registers a call before answering it.
    POST /calls  (form: from, to, diversion)  →  text/plain, one of
        decline       personal number: the dialplan answers 486
        realtime      not ours, or Render unreachable: fallback to OpenAI SIP
        ok:<uuid>     ours: Dial(AudioSocket/127.0.0.1:9092/<uuid>)
    GET /healthz  →  JSON status

127.0.0.1:9092 (AudioSocket): Asterisk connects with the UUID from ok:<uuid>,
    and the call runs (callflow.Call).

Both bind to loopback only: Asterisk runs with host networking on the same VM.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

from voiceagent import audiosocket
from voiceagent.callflow import Call
from voiceagent.config import Config
from voiceagent.render_api import RenderAPI, RenderError
from voiceagent.reporting import EndReporter

logger = logging.getLogger(__name__)

# The dialplan's CURL gives up after 2 s (vm-setup-runbook.md §6.3) and then
# sends the call to OpenAI SIP. Answer well inside that, whatever Render does.
_RENDER_DEADLINE_SECONDS = 1.5
# A registered call Asterisk never connects (caller hung up while ringing).
_PENDING_TTL_SECONDS = 30.0


@dataclass
class Pending:
    answer: dict[str, Any]
    caller: str
    dialed: str
    diversion: str
    created: float = field(default_factory=time.monotonic)


class Agent:
    def __init__(self, cfg: Config, render: RenderAPI, reporter: EndReporter) -> None:
        self.cfg = cfg
        self.render = render
        self.reporter = reporter
        self.pending: dict[str, Pending] = {}
        self.active: dict[str, Call] = {}

    # ── registration (before answer) ─────────────────────────────────────────
    async def register(self, caller: str, dialed: str, diversion: str) -> str:
        self._sweep()
        call_uuid = str(uuid.uuid4())
        try:
            answer = await asyncio.wait_for(
                self.render.start_call(call_uuid, caller, dialed, diversion),
                _RENDER_DEADLINE_SECONDS,
            )
        except (RenderError, asyncio.TimeoutError) as exc:
            # Fallback to OpenAI SIP, where today's code still declines
            # personal numbers (roadmap §4.3).
            logger.warning("Register %s → %s: Render unavailable (%s) → realtime", caller, dialed, exc)
            return "realtime"
        decision = answer.get("decision")
        if decision == "decline":
            logger.info("Register %s → %s: decline", caller, dialed)
            return "decline"
        if decision == "eleven" and answer.get("session"):
            self.pending[call_uuid] = Pending(answer, caller, dialed, diversion)
            logger.info("Register %s → %s: ours (%s)", caller, dialed, call_uuid)
            return f"ok:{call_uuid}"
        return "realtime"

    def _sweep(self) -> None:
        now = time.monotonic()
        for key in [k for k, p in self.pending.items() if now - p.created > _PENDING_TTL_SECONDS]:
            del self.pending[key]

    def health(self) -> dict[str, Any]:
        return {"ok": True, "active_calls": len(self.active), "pending": len(self.pending)}

    # ── HTTP ─────────────────────────────────────────────────────────────────
    async def handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status, body, ctype = 404, b"not found", "text/plain"
        try:
            request_line = (await asyncio.wait_for(reader.readline(), 5)).decode("latin-1")
            method, path, _ = (request_line.split(" ", 2) + ["", ""])[:3]
            length = 0
            while True:
                line = (await asyncio.wait_for(reader.readline(), 5)).decode("latin-1")
                if line in ("\r\n", "\n", ""):
                    break
                name, _, value = line.partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip() or 0)
            raw = await asyncio.wait_for(reader.readexactly(length), 5) if length else b""
            path = path.split("?", 1)[0]
            if method == "POST" and path == "/calls":
                form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}
                result = await self.register(
                    form.get("from", ""), form.get("to", ""), form.get("diversion", "")
                )
                status, body = 200, result.encode()
            elif method == "GET" and path == "/healthz":
                status, body, ctype = 200, json.dumps(self.health()).encode(), "application/json"
        except Exception:
            logger.exception("HTTP request failed")
            status, body = 500, b"error"
        reason = {200: "OK", 404: "Not Found", 500: "Internal Server Error"}[status]
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
        )
        try:
            await writer.drain()
        finally:
            writer.close()

    # ── AudioSocket ──────────────────────────────────────────────────────────
    async def handle_audiosocket(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        first = await audiosocket.read_message(reader)
        if first is None or first[0] != audiosocket.KIND_UUID:
            logger.warning("AudioSocket: no UUID first — closing")
            writer.close()
            return
        call_uuid = audiosocket.uuid_from_payload(first[1])
        pending = self.pending.pop(call_uuid, None)
        if pending is None:
            logger.warning("AudioSocket: unknown or expired call %s — hanging up", call_uuid)
            writer.write(audiosocket.pack(audiosocket.KIND_HANGUP))
            writer.close()
            return
        call = Call(
            self.cfg, self.render, self.reporter, call_uuid,
            {"answer": pending.answer, "caller": pending.caller,
             "dialed": pending.dialed, "diversion": pending.diversion},
            reader, writer,
        )
        self.active[call_uuid] = call
        try:
            await call.run()
        except Exception:
            logger.exception("Call %s crashed", call_uuid)
        finally:
            self.active.pop(call_uuid, None)

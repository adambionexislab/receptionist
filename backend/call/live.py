"""GPT-Live phone engine — the opt-in alternative to the Realtime call path.

A tenant with voice_engine='live' has its calls answered by OpenAI's GPT-Live
(`gpt-live-1`) instead of the Realtime model. The SIP side is identical: the
same DIDWW trunk, the same OpenAI SIP connector, the same webhook endpoint.
Once GPT-Live SIP is enabled on the project, every call fires both
`realtime.call.incoming` and `live.transport.incoming`; each handler acts only
on its own tenants' calls (router._uses_live_engine), so exactly one of them
accepts.

What is different is the model. GPT-Live is full duplex — it listens while it
speaks and decides for itself when to talk — and it has no tools. When it needs
a lookup or a write it DELEGATES to a Responses backend model configured on the
session, and that backend calls our six tools. So compared with call/router.py:

- The prompt is split in two (call/live_prompts.py): conversation for the
  voice, tool rules for the backend.
- Tool calls arrive wrapped in `response.event` envelopes from the backend and
  are answered with `response.item.create` + `response.create`. The tools
  themselves are the same code (router._execute_tool), so the session state
  and the lead email do not depend on the engine.
- There are no response or turn events at all: no response.create/done, no
  speech_started/stopped, no VAD settings. The Realtime path's response gate,
  reply nudge and silence check have nothing to hook into and have no
  counterpart here; GPT-Live owns turn-taking. Timing comes from the
  transcript deltas instead.
- Ending a call keeps the Realtime design: end_call is silent, and the goodbye
  is requested separately (here with session.instructions.append). end_call's
  result is deliberately never returned to the backend — returning it would
  make the backend write a reply, and delegated output text is spoken into
  the call, over the goodbye.

Docs: OpenAI "GPT-Live" guides (Telephony and SIP, Delegation and tools,
Managing sessions) — pasted into gpt-live-1.md at the repo root.
"""

import asyncio
import copy
import datetime
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import httpx
import websockets
from fastapi.responses import Response

from call import router as call_router
from call.live_prompts import SK_LIVE
from config import settings

logger = logging.getLogger(__name__)

_LIVE_BASE = "https://api.openai.com/v1/live/sessions"
_LIVE_WS_BASE = "wss://api.openai.com/v1/live/sessions"

# Locales with GPT-Live prompts. A live tenant in any other locale is answered
# on Realtime (router._uses_live_engine).
_LIVE_CONTENT: dict[str, dict[str, Any]] = {"sk": SK_LIVE}

# After end_call, the goodbye is requested and we wait for her to say it. The
# transcript runs slightly ahead of what the caller hears, so "said" means her
# output has been quiet for this long after she started speaking again.
_FAREWELL_QUIET_SECONDS = 3.0
# If the goodbye never comes, hang up anyway rather than leave the caller on an
# open, silent line. Same budget as the Realtime path.
_FAREWELL_TIMEOUT_SECONDS = call_router._FAREWELL_TIMEOUT_SECONDS
# Nobody has said anything, in either direction, for this long.
_SILENCE_HANGUP_SECONDS = 100.0
# The docs say to continue the backend after every function result, once the
# response that asked for it has finished (response.completed). If that
# terminal event never arrives, the delegated task would stall forever with the
# caller waiting on it — so continue anyway after this long.
_CONTINUE_FALLBACK_SECONDS = 3.0
# Transcript fragments are grouped into one log line per speaker turn; a pause
# this long also ends a line.
_TRANSCRIPT_FLUSH_SECONDS = 1.5
# Nested Responses events that end a backend response.
_TERMINAL_RESPONSE_EVENTS = {
    "response.completed",
    "response.done",
    "response.failed",
    "response.incomplete",
    "response.cancelled",
}

_GREETING_EVENT_ID = "greeting"
_FAREWELL_EVENT_ID = "farewell"


def has_content(locale: str) -> bool:
    return locale in _LIVE_CONTENT


def _names(ctx: "call_router._CallContext") -> tuple[str, str]:
    tenant = ctx.tenant or {}
    name = tenant.get("agent_name") or "Apollonia"
    agency = tenant.get("agency_name") or ctx.content["agency_fallback"]
    return name, agency


def build_voice_instructions(
    ctx: "call_router._CallContext", now: datetime.datetime | None = None
) -> str:
    """session.instructions: what the voice model the caller talks to reads."""
    live = _LIVE_CONTENT[ctx.locale]
    name, agency = _names(ctx)
    text = (
        live["voice_first_line"].format(name=name, agency=agency)
        + live["voice_body"]
        + live["opening_section"].format(name=name, agency=agency)
        + live["datetime_section"].format(now=call_router._now_line(ctx.content, now))
    )
    if not ctx.caller_number_known:
        text += live["voice_ask_for_number"]
    return text


def build_backend_instructions(
    ctx: "call_router._CallContext", now: datetime.datetime | None = None
) -> str:
    """delegation.responses.instructions: what the tool-calling backend reads."""
    live = _LIVE_CONTENT[ctx.locale]
    name, agency = _names(ctx)
    text = live["backend_prompt"].format(name=name, agency=agency) + live[
        "datetime_section"
    ].format(now=call_router._now_line(ctx.content, now))
    if not ctx.caller_number_known:
        text += live["backend_number_unknown"]
    return text


def build_session_config(
    ctx: "call_router._CallContext", now: datetime.datetime | None = None
) -> dict[str, Any]:
    """The `session` object for POST /v1/live/sessions/{id}/accept.

    The config is strict (unknown fields are rejected), and over SIP the audio
    format is negotiated with the carrier, so there is no audio.format — and
    nothing like turn_detection exists on GPT-Live at all.

    The tools are the Realtime agent's own, locale-matched schemas, copied per
    call so one tenant's office list can be injected into leave_message
    without touching the module-level definitions. They now sit on the
    backend, which is also where the office names belong: the voice model
    never sees them, so it cannot read them out."""
    tools = copy.deepcopy(ctx.content.get("tools") or call_router._IT_TOOLS)
    call_router._inject_branch_enum(tools, ctx.branch_names)
    responses: dict[str, Any] = {
        "model": settings.LIVE_BACKEND_MODEL,
        "instructions": build_backend_instructions(ctx, now),
        "tools": tools,
        "tool_choice": "auto",
        # One tool call per backend response. The prompt already asks for one
        # thing at a time, and our continuation logic relies on it.
        "parallel_tool_calls": False,
        "reasoning": {"effort": settings.LIVE_BACKEND_REASONING},
    }
    if settings.LIVE_BACKEND_SERVICE_TIER:
        responses["service_tier"] = settings.LIVE_BACKEND_SERVICE_TIER
    return {
        "type": "live",
        "model": settings.LIVE_MODEL,
        "instructions": build_voice_instructions(ctx, now),
        "audio": {"output": {"voice": settings.LIVE_VOICE}},
        "delegation": {"type": "responses", "responses": responses},
    }


# ── Webhook ──────────────────────────────────────────────────────────────────
async def handle_incoming(data: dict[str, Any]) -> Response:
    """`live.transport.incoming`: accept the call if it belongs to a GPT-Live
    tenant, and otherwise do nothing at all.

    "Nothing" matters in both directions. For a Realtime tenant, its
    realtime.call.incoming twin is the one that accepts. For an unknown number,
    that twin rejects it; rejecting here too would be a second decision on the
    same call."""
    session_id = data.get("session_id") or data.get("call_id")
    if not session_id:
        logger.warning("GPT-Live call webhook: no session_id")
        return Response(status_code=400)
    if data.get("type") not in (None, "sip"):
        logger.info("Ignoring GPT-Live transport %r for %s", data.get("type"), session_id)
        return Response(status_code=200)

    ctx = call_router._resolve_call(session_id, data.get("sip_headers") or [])
    if ctx is None or not call_router._uses_live_engine(ctx):
        return Response(status_code=200)
    if not call_router._claim_call(session_id):
        logger.info("Duplicate delivery for GPT-Live call %s — already handled", session_id)
        return Response(status_code=200)

    logger.info("GPT-Live answers call %s (tenant %s)", session_id, ctx.tenant["id"])
    session = call_router._new_call_session(session_id, ctx)
    task = asyncio.create_task(
        run_live_call(
            session_id,
            build_session_config(ctx),
            session,
            ctx.content,
            ctx.tenant_store,
        )
    )
    call_router._active_calls.add(task)
    task.add_done_callback(call_router._active_calls.discard)
    return Response(status_code=200)


# ── Call control (REST) ──────────────────────────────────────────────────────
def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }


async def _accept(session_id: str, config: dict[str, Any]) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_LIVE_BASE}/{session_id}/accept",
                headers=_auth_headers(),
                json={"session": config},
            )
        if resp.status_code >= 400:
            logger.error(
                "GPT-Live accept failed for %s: %s — %s",
                session_id, resp.status_code, resp.text,
            )
            return False
        logger.info("Accepted GPT-Live call %s", session_id)
        return True
    except Exception as exc:
        logger.error("GPT-Live accept error for %s: %s", session_id, exc)
        return False


async def _hangup(session_id: str) -> bool:
    """Hang up, retried, and never reported as done unless OpenAI accepted it —
    the same contract as router._hangup_call, for the same reasons."""
    for attempt in range(1, call_router._HANGUP_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{_LIVE_BASE}/{session_id}/hangup", headers=_auth_headers()
                )
            if resp.status_code == 404:
                logger.info("GPT-Live call %s was already ended", session_id)
                return True
            if resp.status_code < 400:
                logger.info("Hung up GPT-Live call %s", session_id)
                return True
            logger.error(
                "GPT-Live hangup rejected for %s (attempt %d/%d): %s — %s",
                session_id, attempt, call_router._HANGUP_ATTEMPTS,
                resp.status_code, resp.text,
            )
        except Exception as exc:
            logger.error(
                "GPT-Live hangup error for %s (attempt %d/%d): %s",
                session_id, attempt, call_router._HANGUP_ATTEMPTS, exc,
            )
        if attempt < call_router._HANGUP_ATTEMPTS:
            await asyncio.sleep(call_router._HANGUP_RETRY_DELAY)
    logger.error(
        "GPT-Live call %s is STILL UP after %d hangup attempts",
        session_id, call_router._HANGUP_ATTEMPTS,
    )
    return False


# ── The live call ────────────────────────────────────────────────────────────
class _TranscriptLog:
    """Joins transcript fragments into one log line per speaker turn.

    Fragments follow audio cadence, not sentences, and both speakers' can
    interleave (full duplex), so a line ends when the other side speaks or on a
    pause. Logging the caller's words is a deliberate debugging choice for the
    demo tenant; the Realtime path logs only hers."""

    _LABELS = {"caller": "Caller", "agent": "Apollonia"}

    def __init__(self) -> None:
        self._speaker: Optional[str] = None
        self._parts: list[str] = []
        self._last = 0.0

    def add(self, speaker: str, delta: str, now: float) -> None:
        if speaker != self._speaker:
            self.flush()
            self._speaker = speaker
        self._parts.append(delta)
        self._last = now

    def flush_if_idle(self, now: float) -> None:
        if self._parts and now - self._last > _TRANSCRIPT_FLUSH_SECONDS:
            self.flush()

    def flush(self) -> None:
        text = "".join(self._parts).strip()
        if text and self._speaker:
            logger.info("%s: %s", self._LABELS[self._speaker], text)
        self._parts = []
        self._speaker = None


class LiveCall:
    """One GPT-Live call's control loop, over its sideband WebSocket.

    Separated from the socket (events come in through handle(), commands go out
    through `send`, time comes from `clock`) so the protocol logic can be
    driven directly in tests."""

    def __init__(
        self,
        session_id: str,
        session: dict[str, Any],
        content: dict[str, Any],
        tenant_store: Any,
        send: Callable[[dict[str, Any]], Awaitable[None]],
        clock: Callable[[], float],
    ) -> None:
        self.session_id = session_id
        self.session = session
        self.content = content
        self.tenant_store = tenant_store
        self._send = send
        self._clock = clock
        self.listing_fields = content.get("model_listing_fields") or {
            k: k for k in call_router._MODEL_LISTING_FIELDS
        }
        now = clock()
        # Anything said, by either side. Drives the silence hang-up.
        self.last_activity = now
        # Her speech only. Drives the farewell hang-up.
        self.last_output_at = 0.0
        # When the goodbye instruction was acknowledged as injected.
        self.farewell_acked_at: Optional[float] = None
        # Set when a function result was submitted and the backend response
        # still has to be continued; cleared when it is.
        self.continue_pending_since: Optional[float] = None
        self._handled_calls: set[str] = set()
        self.transcript = _TranscriptLog()
        self.closed = False

    async def start(self) -> None:
        """Have her answer the phone. The session already carries the full
        voice prompt from the accept; this is only the cue to speak first."""
        await self._send({
            "type": "session.instructions.append",
            "event_id": _GREETING_EVENT_ID,
            "delegation_id": None,
            "content": self.content["greeting_prompt"],
        })

    async def handle(self, msg: dict[str, Any]) -> None:
        etype = msg.get("type")
        now = self._clock()

        if etype == "session.input_transcript.delta":
            self.last_activity = now
            self.transcript.add("caller", msg.get("delta") or "", now)

        elif etype == "session.output_transcript.delta":
            self.last_activity = now
            self.last_output_at = now
            self.transcript.add("agent", msg.get("delta") or "", now)

        elif etype == "session.delegation.created":
            d = msg.get("delegation") or {}
            logger.info(
                "Delegation %s → %s (response %s)",
                d.get("id"), d.get("target"), d.get("response_id"),
            )

        elif etype == "response.event":
            await self._on_backend_event(msg.get("delegation_id"), msg.get("event") or {})

        elif etype == "session.instructions.appended":
            if msg.get("client_event_id") == _FAREWELL_EVENT_ID:
                self.farewell_acked_at = now

        elif etype == "session.closed":
            self.transcript.flush()
            logger.info(
                "GPT-Live call %s closed: reason=%s usage=%s",
                self.session_id, msg.get("reason"), msg.get("usage"),
            )
            self.closed = True

        elif etype == "error":
            err = msg.get("error") or {}
            logger.error(
                "GPT-Live error [%s/%s] for %s: %s (command %s)",
                err.get("type"), err.get("code"), self.session_id,
                err.get("message"), err.get("client_event_id"),
            )

    async def _on_backend_event(self, delegation_id: Any, event: dict[str, Any]) -> None:
        etype = event.get("type")

        if etype == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "message":
                text = " ".join(
                    part.get("text", "")
                    for part in item.get("content") or []
                    if part.get("type") == "output_text"
                ).strip()
                if text:
                    logger.info("Backend [%s]: %s", delegation_id, text)
                return
            if item.get("type") != "function_call":
                return
            await self._on_function_call(item)

        elif etype in _TERMINAL_RESPONSE_EVENTS:
            response = event.get("response") or {}
            status = response.get("status")
            if status not in (None, "completed"):
                logger.warning(
                    "Backend response for delegation %s ended %s: %s",
                    delegation_id, status,
                    response.get("status_details") or response.get("error"),
                )
            if self.continue_pending_since is not None:
                await self._continue_backend()

        elif etype == "error":
            logger.error("Backend error for delegation %s: %s", delegation_id, event)

    async def _on_function_call(self, item: dict[str, Any]) -> None:
        call_id = item.get("call_id")
        name = item.get("name")
        if not call_id or call_id in self._handled_calls:
            return
        self._handled_calls.add(call_id)

        if name == "end_call":
            await self.begin_ending()
            return

        output = await call_router._execute_tool(
            name, item.get("arguments"), self.session, self.tenant_store,
            self.listing_fields,
        )
        if output is None:
            # Every pending call needs a result before the backend can go on.
            output = json.dumps({"error": "unknown tool"})
        await self._send({
            "type": "response.item.create",
            "event_id": f"out_{call_id}",
            "item": {"type": "function_call_output", "call_id": call_id, "output": output},
        })
        self.continue_pending_since = self._clock()

    async def _continue_backend(self) -> None:
        self.continue_pending_since = None
        await self._send({"type": "response.create", "event_id": f"continue_{uuid.uuid4().hex[:12]}"})

    async def begin_ending(self) -> None:
        """end_call arrived: ask for the goodbye, then let the watchdog hang up
        once it has been said. end_call's own result is never submitted — see
        the module docstring."""
        if self.session.get("ending_at") is not None:
            return
        logger.info("Apollonia ending call %s", self.session_id)
        self.session["ending_at"] = self._clock()
        self.session["ending"] = True
        await self._send({
            "type": "session.instructions.append",
            "event_id": _FAREWELL_EVENT_ID,
            "delegation_id": None,
            "content": self.content["farewell_instruction"],
        })

    def watchdog_action(self, now: float) -> Optional[str]:
        """What the watchdog should do now, if anything. Pure, for testing:
        'hangup' (goodbye said), 'hangup_timeout' (goodbye never came),
        'hangup_silence', 'continue' (backend stalled after a tool result)."""
        ending_at = self.session.get("ending_at")
        if ending_at is not None:
            acked = self.farewell_acked_at
            if (
                acked is not None
                and self.last_output_at > acked
                and now - self.last_output_at >= _FAREWELL_QUIET_SECONDS
            ):
                return "hangup"
            if now - ending_at > _FAREWELL_TIMEOUT_SECONDS:
                return "hangup_timeout"
            return None
        if (
            self.continue_pending_since is not None
            and now - self.continue_pending_since > _CONTINUE_FALLBACK_SECONDS
        ):
            return "continue"
        if now - self.last_activity > _SILENCE_HANGUP_SECONDS:
            return "hangup_silence"
        return None

    async def watchdog(self) -> None:
        """Runs until it has hung up the call."""
        while True:
            await asyncio.sleep(0.5)
            now = self._clock()
            self.transcript.flush_if_idle(now)
            action = self.watchdog_action(now)
            if action == "continue":
                logger.warning(
                    "No end of backend response %.0fs after a tool result — "
                    "continuing it anyway (%s)",
                    _CONTINUE_FALLBACK_SECONDS, self.session_id,
                )
                await self._continue_backend()
            elif action is not None:
                self.transcript.flush()
                if action == "hangup_timeout":
                    logger.warning(
                        "No goodbye within %.0fs of end_call — hanging up %s",
                        _FAREWELL_TIMEOUT_SECONDS, self.session_id,
                    )
                elif action == "hangup_silence":
                    logger.info(
                        "%.0fs of silence — hanging up %s",
                        _SILENCE_HANGUP_SECONDS, self.session_id,
                    )
                await _hangup(self.session_id)
                return


async def run_live_call(
    session_id: str,
    config: dict[str, Any],
    session: dict[str, Any],
    content: dict[str, Any],
    tenant_store: Any,
) -> None:
    """Accept a pending GPT-Live SIP call, drive it over the sideband until it
    ends, then send the lead email and persist the call — the same teardown as
    the Realtime path."""
    if not await _accept(session_id, config):
        return
    session["started_at"] = datetime.datetime.now(datetime.timezone.utc)

    loop = asyncio.get_running_loop()
    hung_up_by_us = False
    live: Optional[LiveCall] = None
    try:
        async with websockets.connect(
            f"{_LIVE_WS_BASE}/{session_id}/attach",
            additional_headers=[("Authorization", f"Bearer {settings.OPENAI_API_KEY}")],
        ) as ws:
            logger.info("Sideband attached to GPT-Live call %s", session_id)

            async def send(event: dict[str, Any]) -> None:
                await ws.send(json.dumps(event, ensure_ascii=False))

            live = LiveCall(session_id, session, content, tenant_store, send, loop.time)
            await live.start()

            async def events() -> None:
                try:
                    async for raw in ws:
                        await live.handle(json.loads(raw))
                        if live.closed:
                            return
                except websockets.exceptions.ConnectionClosed:
                    logger.info("GPT-Live call %s: sideband closed", session_id)

            events_task = asyncio.create_task(events())
            watchdog_task = asyncio.create_task(live.watchdog())
            done, _ = await asyncio.wait(
                {events_task, watchdog_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if watchdog_task in done:
                hung_up_by_us = True
                # Give session.closed a moment to arrive with the final usage.
                await asyncio.wait({events_task}, timeout=5)
            for task in (events_task, watchdog_task):
                if not task.done():
                    task.cancel()
            for task in (events_task, watchdog_task):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
    except Exception as exc:
        logger.exception("Unhandled error in GPT-Live call %s: %s", session_id, exc)
        # A broken sideband leaves an accepted call with no tools and no way to
        # end it. Don't leave the caller there.
        if not hung_up_by_us:
            await _hangup(session_id)
    finally:
        if live is not None:
            live.transcript.flush()
        session["ended_at"] = datetime.datetime.now(datetime.timezone.utc)
        await call_router._send_lead_email(session)
        try:
            await asyncio.to_thread(call_router._persist_call, session)
        except Exception:
            logger.exception("Failed to persist GPT-Live call %s", session_id)

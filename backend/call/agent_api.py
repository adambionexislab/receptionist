"""Internal API for the ElevenLabs voice agent (elevenlabs-voice-roadmap.md §4.1).

Tenants with voice_engine='eleven' have their calls answered on Apollonia's own
Hetzner VM: Asterisk takes the call, and an agent there runs gpt-realtime with
text output and speaks it with ElevenLabs. Everything that is not audio stays
here: who the tenant is, the prompt and tools, the tool handlers, and the
end-of-call lead email, WhatsApp alert and call row. So this module is a thin
HTTP layer over the same functions the OpenAI SIP path uses (call/router.py),
called as they are; a lead looks the same whichever engine took the call.

Only the agent calls this, authenticated with VOICE_AGENT_TOKEN. With the
token unset every endpoint is a 404, so the API does not exist until it is
configured.

  POST /internal/voice/calls                one decision before the call is
                                            answered: decline | realtime | eleven
  POST /internal/voice/calls/{uuid}/tool    run one of her data tools
  POST /internal/voice/calls/{uuid}/end     wrap up: lead email, WhatsApp, call row
"""

import asyncio
import datetime
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from call import router as call_router
from config import settings

logger = logging.getLogger(__name__)


def _require_agent(request: Request) -> None:
    """Bearer VOICE_AGENT_TOKEN. Unset token: the whole API answers 404, as if
    it were not deployed — nothing to probe until the VM is configured."""
    expected = settings.VOICE_AGENT_TOKEN
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth[:7].lower() == "bearer " else ""
    if not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


router = APIRouter(prefix="/internal/voice", dependencies=[Depends(_require_agent)])


# ── call state ───────────────────────────────────────────────────────────────
# A call's session lives in memory from POST /calls to POST /end, as it does
# for the OpenAI SIP path inside _run_call. Render restarts in between lose it;
# /end rebuilds it from what the agent sends (see _rebuild), so the lead is
# still produced.

# A call that never reaches /end (agent crashed, VM lost) must not pin its
# session forever. Long enough for any real call; the max-duration guard on the
# agent ends calls well before this.
_SESSION_TTL_SECONDS = 3 * 3600


@dataclass
class _AgentCall:
    session: dict[str, Any]
    content: dict[str, Any]
    tenant_store: Any
    listing_fields: dict[str, str]
    # What POST /calls hands the agent; empty on a session rebuilt at /end.
    session_config: dict[str, Any] = field(default_factory=dict)
    created: float = field(default_factory=time.monotonic)


_calls: dict[str, _AgentCall] = {}
# Calls whose /end was already accepted: a retried /end must not send the lead
# email twice. Lost on restart, which only matters if a retry straddles one.
_ended: dict[str, float] = {}
# Strong references to the post-call tasks, so they are not garbage-collected
# while the email is being written (same reason as router._active_calls).
_wrapups: set[asyncio.Task] = set()


def _sweep() -> None:
    now = time.monotonic()
    for store_ in (_calls, _ended):
        for uuid in [
            k for k, v in store_.items()
            if now - (v.created if isinstance(v, _AgentCall) else v) > _SESSION_TTL_SECONDS
        ]:
            del store_[uuid]


def _sip_value(value: Optional[str]) -> str:
    """A header value _resolve_call can read. The agent passes on what Asterisk
    saw: a bare number for From/To (DIDWW sends them without "+"), and either
    nothing or a full header for Diversion."""
    value = (value or "").strip()
    if not value:
        return ""
    if "sip:" in value.lower() or "tel:" in value.lower():
        return value
    return f"<sip:{value}@agent>"


def _resolve(call_uuid: str, caller: str, dialed: str, diversion: str):
    """The tenant and caller for this call, by the same rules the OpenAI SIP
    path uses: call_router._resolve_call, unchanged, fed synthetic headers."""
    headers = [
        {"name": "From", "value": _sip_value(caller)},
        {"name": "To", "value": _sip_value(dialed)},
    ]
    if diversion:
        headers.append({"name": "Diversion", "value": _sip_value(diversion)})
    return call_router._resolve_call(call_uuid, headers)


def _listing_fields(content: dict[str, Any]) -> dict[str, str]:
    # Same projection as _run_call: listing results reach the model under the
    # locale's field names.
    return content.get("model_listing_fields") or {
        k: k for k in call_router._MODEL_LISTING_FIELDS
    }


def _instructions(ctx) -> tuple[str, bool]:
    """The session prompt for this call, and whether she collects e-mail.

    Copied from router._handle_realtime_incoming rather than shared, so the
    production handler is not restructured for this (roadmap §4.1). Keep the
    two in step: a prompt rule added there belongs here too."""
    content = ctx.content
    if ctx.tenant is not None:
        instructions = call_router._build_system_prompt(
            content, ctx.tenant["agency_name"], ctx.tenant["agent_name"]
        )
    else:
        instructions = call_router._build_system_prompt(content, None, None)
    if not ctx.caller_number_known:
        instructions = instructions + content["ask_for_number"]
    collect_email = call_router._collects_email(ctx.tenant)
    if collect_email:
        instructions = instructions + content["demo_email_section"]
    return instructions, collect_email


# ── POST /calls ──────────────────────────────────────────────────────────────
class CallStart(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    call_uuid: str = Field(min_length=1, max_length=100)
    caller: str = Field(default="", alias="from")
    dialed: str = Field(default="", alias="to")
    diversion: str = ""


@router.post("/calls")
async def start_call(body: CallStart) -> dict[str, Any]:
    """Everything decided before the call is answered, in one answer:

    - decline:  the caller is on the tenant's personal-numbers list. The
                dialplan answers 486, exactly what the OpenAI path does.
    - realtime: not this engine's call (tenant on realtime, or a number
                with no tenant). The dialplan hands it to OpenAI SIP, where
                today's code takes it — including rejecting unknown numbers.
    - eleven:   take it. The session config, greeting and farewell come back
                with it, and a session is opened for the tools and /end.
    """
    _sweep()
    existing = _calls.get(body.call_uuid)
    if existing is not None:
        # A retried registration (the agent timed out waiting for the first
        # answer): same call, same session. Never two sessions for one call.
        return _eleven_answer(existing)

    ctx = await asyncio.to_thread(
        _resolve, body.call_uuid, body.caller, body.dialed, body.diversion
    )
    if ctx is None or ctx.tenant is None:
        return {"decision": "realtime"}
    if ctx.no_answer:
        logger.info("Agent call %s: caller is on the no-answer list — decline", body.call_uuid)
        return {"decision": "decline"}
    if (ctx.tenant.get("voice_engine") or "realtime") != "eleven":
        return {"decision": "realtime"}

    instructions, collect_email = _instructions(ctx)
    content = ctx.content
    session = call_router._new_call_session(body.call_uuid, ctx)
    call = _AgentCall(
        session=session,
        content=content,
        tenant_store=ctx.tenant_store,
        listing_fields=_listing_fields(content),
        session_config=call_router._build_accept_config(
            instructions, content, ctx.branch_names, collect_email
        ),
    )
    _calls[body.call_uuid] = call
    logger.info(
        "Agent call %s: tenant %s on ElevenLabs", body.call_uuid, ctx.tenant["id"]
    )
    return _eleven_answer(call)


def _eleven_answer(call: _AgentCall) -> dict[str, Any]:
    content = call.content
    return {
        "decision": "eleven",
        "locale": call.session.get("locale") or "it",
        # The OpenAI SIP session config, as is: instructions, tools,
        # tool_choice, reasoning and the tuned turn detection. The agent sets
        # the output to text and the audio formats it needs on top.
        "session": call.session_config,
        "greeting_prompt": content["greeting_prompt"],
        "farewell_response": call_router._farewell_response_event(content),
    }


# ── POST /calls/{uuid}/tool ──────────────────────────────────────────────────
class ToolCall(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # The model's arguments string as received; a parsed object is accepted too.
    arguments: Union[str, dict[str, Any], None] = None


@router.post("/calls/{call_uuid}/tool")
async def run_tool(call_uuid: str, body: ToolCall) -> dict[str, Any]:
    """One of her data tools, run by the same code as on the OpenAI SIP path
    (router._execute_tool), against this call's session. `output` is the
    function_call_output to send back to the model; null for a name that is
    not a data tool (end_call is the agent's own)."""
    call = _calls.get(call_uuid)
    if call is None:
        raise HTTPException(status_code=404, detail="Unknown call")
    raw = body.arguments
    if isinstance(raw, dict):
        raw = json.dumps(raw, ensure_ascii=False)
    output = await call_router._execute_tool(
        body.name, raw, call.session, call.tenant_store, call.listing_fields
    )
    return {"output": output}


# ── POST /calls/{uuid}/end ───────────────────────────────────────────────────
class ToolRecord(BaseModel):
    name: str
    arguments: Union[str, dict[str, Any], None] = None


class CallEnd(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    started_at: Optional[datetime.datetime] = None
    ended_at: Optional[datetime.datetime] = None
    ended_by: str = ""
    # Enough to rebuild the session if this process lost it: who called which
    # number, and every data tool call in order (see _rebuild).
    caller: str = Field(default="", alias="from")
    dialed: str = Field(default="", alias="to")
    diversion: str = ""
    tool_calls: list[ToolRecord] = []
    # Her lines and the caller's, for the log only — the lead is built from
    # the session, exactly as on the OpenAI SIP path.
    transcript: list[dict[str, Any]] = []


def _utc(value: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """A timestamp without a zone is taken as UTC, so it can be subtracted from
    the aware ones the rest of the call path uses."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


async def _rebuild(call_uuid: str, body: CallEnd) -> Optional[_AgentCall]:
    """Recreate a session this process no longer has (Render restarted mid-call)
    by resolving the call again and replaying its tool calls in order. The tools
    are what fill the session, so the replayed session produces the same lead.
    None when the number no longer resolves to a tenant."""
    ctx = await asyncio.to_thread(
        _resolve, call_uuid, body.caller, body.dialed, body.diversion
    )
    if ctx is None or ctx.tenant is None:
        return None
    content = ctx.content
    call = _AgentCall(
        session=call_router._new_call_session(call_uuid, ctx),
        content=content,
        tenant_store=ctx.tenant_store,
        listing_fields=_listing_fields(content),
    )
    for tc in body.tool_calls:
        raw = tc.arguments
        if isinstance(raw, dict):
            raw = json.dumps(raw, ensure_ascii=False)
        try:
            await call_router._execute_tool(
                tc.name, raw, call.session, call.tenant_store, call.listing_fields
            )
        except Exception:
            logger.exception("Replaying %s for call %s failed — skipped", tc.name, call_uuid)
    logger.warning(
        "Agent call %s: session rebuilt from %d tool calls (process restarted mid-call)",
        call_uuid, len(body.tool_calls),
    )
    return call


async def _wrap_up(call_uuid: str, session: dict[str, Any]) -> None:
    """The OpenAI SIP path's teardown (router._run_call's finally), in the same
    order: the lead email first, then the call row, which reuses its summary."""
    try:
        await call_router._send_lead_email(session)
    except Exception:
        logger.exception("Lead email failed for agent call %s", call_uuid)
    try:
        await asyncio.to_thread(call_router._persist_call, session)
    except Exception:
        logger.exception("Failed to persist agent call %s", call_uuid)


@router.post("/calls/{call_uuid}/end", status_code=202)
async def end_call(call_uuid: str, body: CallEnd) -> Response:
    """The call is over: send the lead and record the call, as the OpenAI SIP
    path does at teardown. Accepted at once and finished in the background
    (writing the summary takes seconds), so the agent's retry loop only has
    to get a 202. A repeat for the same call is a no-op."""
    if call_uuid in _ended:
        return Response(status_code=202)

    call = _calls.pop(call_uuid, None)
    if call is None:
        call = await _rebuild(call_uuid, body)
        if call is None:
            logger.warning("Agent call %s ended but has no tenant — nothing to send", call_uuid)
            _ended[call_uuid] = time.monotonic()
            return Response(status_code=202)

    now = datetime.datetime.now(datetime.timezone.utc)
    session = call.session
    # The agent knows when the call was answered; this process never saw it.
    # Stamped like _run_call does (aware UTC), so the duration subtracts cleanly.
    session["started_at"] = session.get("started_at") or _utc(body.started_at)
    session["ended_at"] = _utc(body.ended_at) or now
    for line in body.transcript:
        logger.info(
            "Agent call %s — %s: %s", call_uuid, line.get("role", "?"), line.get("text", "")
        )
    logger.info("Agent call %s ended (%s)", call_uuid, body.ended_by or "unknown")

    _ended[call_uuid] = time.monotonic()
    task = asyncio.create_task(_wrap_up(call_uuid, session))
    _wrapups.add(task)
    task.add_done_callback(_wrapups.discard)
    return Response(status_code=202)

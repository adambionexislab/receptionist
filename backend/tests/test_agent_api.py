"""The internal API the ElevenLabs voice agent calls (call/agent_api.py).

Tenants on voice_engine='eleven' are answered on Apollonia's own VM, but their
tenant lookup, personal-numbers decline, tools and lead email are still this
backend's — the same functions the OpenAI SIP path runs. These tests pin the
contract the VM's dialplan and agent rely on (elevenlabs-voice-roadmap.md §4.1).
"""

import asyncio
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from call import agent_api
from call import router as call_router
from config import settings
from noanswer import db as noanswer_db
from tenants import db as tenants_db

TOKEN = "agent-test-token"
DID = "421482304191"  # DIDWW sends numbers without "+"
CALLER = "421903123456"

TENANT = {
    "id": "tenant-eleven", "agency_name": "Štúdio Test", "agent_name": "Apollonia",
    "locale": "sk", "real_number": "+421900000000", "lead_email": "lead@studio.sk",
    "twilio_number": "+" + DID, "voice_engine": "eleven",
}

LISTING = {
    "id": 7, "agent_id": None, "address": "Hlavná 12", "zone": "Pezinok",
    "type": "vendita", "rooms": 3, "size_sqm": 72, "price": 189000,
    "currency": "EUR", "available": True, "text": "Svetlý byt",
}


class FakeStore:
    def search(self, **kwargs):
        return [dict(LISTING)]

    def get_by_address(self, query):
        return [dict(LISTING)] if "Hlavn" in query else []


@pytest.fixture(autouse=True)
def world(monkeypatch):
    """A tenant on +421482304191, an in-memory personal-numbers list, and a
    clean agent call registry for every test."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    monkeypatch.setattr(noanswer_db, "_initialized", False)
    noanswer_db.init()

    tenant = dict(TENANT)
    monkeypatch.setattr(
        call_router, "_find_tenant_by_dialed",
        lambda n: tenant if call_router._same_number(n, DID) else None,
    )
    monkeypatch.setattr(call_router.tenant_stores, "get_or_create", lambda tid: FakeStore())
    monkeypatch.setattr(call_router.branches_db, "list_for_tenant", lambda tid: [])
    monkeypatch.setattr(settings, "VOICE_AGENT_TOKEN", TOKEN)
    monkeypatch.setattr(agent_api, "_calls", {})
    monkeypatch.setattr(agent_api, "_ended", {})
    yield tenant
    conn.close()


@pytest.fixture
def wrapups(monkeypatch):
    """Record what the end-of-call path would send instead of sending it."""
    sent = {"email": [], "persisted": []}

    async def fake_email(session):
        sent["email"].append(session)

    monkeypatch.setattr(call_router, "_send_lead_email", fake_email)
    monkeypatch.setattr(call_router, "_persist_call", lambda s: sent["persisted"].append(s))
    return sent


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(agent_api.router)
    return TestClient(app)


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _start(client, uuid="u1", caller=CALLER, dialed=DID, diversion=""):
    return client.post(
        "/internal/voice/calls",
        json={"call_uuid": uuid, "from": caller, "to": dialed, "diversion": diversion},
        headers=_auth(),
    )


async def _drain():
    await asyncio.gather(*list(agent_api._wrapups))


# ── auth ─────────────────────────────────────────────────────────────────────
def test_without_a_configured_token_the_api_does_not_exist(client, monkeypatch):
    monkeypatch.setattr(settings, "VOICE_AGENT_TOKEN", None)
    assert _start(client).status_code == 404


def test_a_wrong_or_missing_token_is_refused(client):
    body = {"call_uuid": "u1", "from": CALLER, "to": DID}
    assert client.post("/internal/voice/calls", json=body).status_code == 401
    assert client.post(
        "/internal/voice/calls", json=body, headers={"Authorization": "Bearer nope"}
    ).status_code == 401


# ── POST /calls: one decision before answer ──────────────────────────────────
def test_an_eleven_tenant_gets_its_session(client):
    resp = _start(client)
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "eleven"
    assert data["locale"] == "sk"
    # The same prompt and tools the OpenAI SIP path would accept the call with.
    assert "Štúdio Test" in data["session"]["instructions"]
    assert {t["name"] for t in data["session"]["tools"]} >= {"search_listings", "end_call"}
    assert data["session"]["audio"]["input"]["turn_detection"] == call_router._TURN_DETECTION
    assert data["greeting_prompt"]
    assert data["farewell_response"]["response"]["tool_choice"] == "none"
    assert "u1" in agent_api._calls


def test_the_did_also_resolves_from_diversion(client):
    # A forwarded call: To is the originally dialed number, the DID is in Diversion.
    resp = _start(client, dialed="421211111111", diversion=f"<sip:{DID}@91.99.238.181>")
    assert resp.json()["decision"] == "eleven"


def test_a_listed_caller_is_declined(client, world):
    noanswer_db.add(world["id"], "+" + CALLER, "Mama")
    assert _start(client).json() == {"decision": "decline"}
    assert agent_api._calls == {}


def test_a_listed_caller_is_declined_even_for_a_realtime_tenant(client, world):
    # The one request decides the decline for every engine, so the dialplan
    # never has to send a personal number anywhere.
    world["voice_engine"] = "realtime"
    noanswer_db.add(world["id"], "+" + CALLER, "Mama")
    assert _start(client).json() == {"decision": "decline"}


@pytest.mark.parametrize("engine", ["realtime", "live", None])
def test_a_tenant_on_another_engine_is_handed_back(client, world, engine):
    world["voice_engine"] = engine
    assert _start(client).json() == {"decision": "realtime"}
    assert agent_api._calls == {}


def test_an_unknown_number_is_handed_back_not_declined(client):
    # OpenAI SIP's own handler rejects unknown numbers; this API never decides that.
    assert _start(client, dialed="421299999999").json() == {"decision": "realtime"}


def test_a_retried_registration_reuses_the_session(client):
    first = _start(client).json()
    session = agent_api._calls["u1"].session
    second = _start(client).json()
    assert second == first
    assert agent_api._calls["u1"].session is session


# ── POST /calls/{uuid}/tool ──────────────────────────────────────────────────
def test_a_tool_runs_against_the_call_session(client):
    _start(client)
    resp = client.post(
        "/internal/voice/calls/u1/tool",
        json={"name": "search_listings", "arguments": json.dumps({"zone": "Pezinok"})},
        headers=_auth(),
    )
    assert resp.status_code == 200
    output = json.loads(resp.json()["output"])
    assert len(output) == 1
    # Projected for the model: bookkeeping columns never reach her.
    assert "agent_id" not in output[0] and "id" not in output[0]
    assert agent_api._calls["u1"].session["listings_shown"][0]["address"] == "Hlavná 12"


def test_parsed_arguments_are_accepted_too(client):
    _start(client)
    resp = client.post(
        "/internal/voice/calls/u1/tool",
        json={"name": "record_caller_info", "arguments": {"name": "Ján"}},
        headers=_auth(),
    )
    assert json.loads(resp.json()["output"]) == {"recorded": True}
    assert agent_api._calls["u1"].session["caller_info"] == {"name": "Ján"}


def test_end_call_is_not_a_backend_tool(client):
    _start(client)
    resp = client.post(
        "/internal/voice/calls/u1/tool", json={"name": "end_call"}, headers=_auth()
    )
    assert resp.json() == {"output": None}


def test_a_tool_for_an_unknown_call_is_a_404(client):
    resp = client.post(
        "/internal/voice/calls/nope/tool", json={"name": "search_listings"}, headers=_auth()
    )
    assert resp.status_code == 404


# ── POST /calls/{uuid}/end ───────────────────────────────────────────────────
def _end(client, uuid="u1", **extra):
    body = {
        "started_at": "2026-10-20T10:00:00Z",
        "ended_at": "2026-10-20T10:02:30Z",
        "ended_by": "apollonia",
        **extra,
    }
    return client.post(f"/internal/voice/calls/{uuid}/end", json=body, headers=_auth())


async def test_ending_sends_the_lead_and_records_the_call(client, wrapups):
    _start(client)
    client.post(
        "/internal/voice/calls/u1/tool",
        json={"name": "search_listings", "arguments": "{}"},
        headers=_auth(),
    )
    assert _end(client).status_code == 202
    await _drain()

    assert len(wrapups["email"]) == 1 and len(wrapups["persisted"]) == 1
    session = wrapups["persisted"][0]
    assert session["tenant_id"] == TENANT["id"]
    assert session["listings_shown"][0]["address"] == "Hlavná 12"
    # The agent's timestamps, as aware datetimes: the call row's duration.
    assert (session["ended_at"] - session["started_at"]).total_seconds() == 150
    assert "u1" not in agent_api._calls


async def test_a_retried_end_sends_nothing_twice(client, wrapups):
    _start(client)
    assert _end(client).status_code == 202
    assert _end(client).status_code == 202
    await _drain()
    assert len(wrapups["email"]) == 1


async def test_a_session_lost_to_a_restart_is_rebuilt_from_the_tool_calls(client, wrapups):
    # No POST /calls in this process: Render restarted mid-call.
    resp = _end(
        client,
        uuid="lost",
        **{"from": CALLER, "to": DID},
        tool_calls=[
            {"name": "get_listing_by_address", "arguments": json.dumps({"address_query": "Hlavná"})},
            {"name": "mark_listing_interest", "arguments": {"address": "Hlavná 12"}},
            {"name": "record_caller_info", "arguments": {"name": "Ján"}},
        ],
        transcript=[{"role": "caller", "text": "Dobrý deň"}],
    )
    assert resp.status_code == 202
    await _drain()

    session = wrapups["email"][0]
    assert session["interested_listings"][0]["address"] == "Hlavná 12"
    assert session["caller_info"] == {"name": "Ján"}
    assert len(wrapups["persisted"]) == 1


async def test_a_lost_session_with_no_tenant_sends_nothing(client, wrapups):
    assert _end(client, uuid="lost", **{"from": CALLER, "to": "421299999999"}).status_code == 202
    await _drain()
    assert wrapups["email"] == [] and wrapups["persisted"] == []


# ── the other engines are unaffected ─────────────────────────────────────────
def test_an_eleven_tenant_reaching_openai_sip_is_answered_on_realtime(world):
    # The Asterisk fallback hands an 'eleven' tenant's call to OpenAI SIP; it
    # must be answered there like any Realtime call, never left to GPT-Live.
    ctx = call_router._resolve_call(
        "c1",
        [
            {"name": "From", "value": f"<sip:{CALLER}@carrier>"},
            {"name": "Diversion", "value": f"<sip:{DID}@91.99.238.181>"},
        ],
    )
    assert ctx.tenant["voice_engine"] == "eleven"
    assert not call_router._uses_live_engine(ctx)

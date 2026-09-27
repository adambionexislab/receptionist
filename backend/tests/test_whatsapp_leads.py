"""Tests for the WhatsApp lead alert (services/whatsapp.py + call/router.py).

The rule: a lead routed to agents also goes, as a WhatsApp template message, to
every one of those agents who has a WhatsApp number — on top of the email,
never instead of it. A failing or unconfigured WhatsApp must never cost the
email.
"""

import asyncio
import sqlite3

import httpx
import pytest

from agents import db as agents_db
from call import router
from listings import db as listings_db
from services import whatsapp
from tenants import db as tenants_db

TENANT = "tenant-a"
INBOX = "agenzia@studio.it"


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    monkeypatch.setattr(agents_db, "_initialized", False)
    monkeypatch.setattr(listings_db, "_initialized", False)
    agents_db.init()
    listings_db.init()
    yield conn
    conn.close()


class FakeClient:
    """Stands in for httpx.AsyncClient and records every POST."""

    posts: list[tuple[str, dict]] = []
    fail_whatsapp = False

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        FakeClient.posts.append((url, json))
        request = httpx.Request("POST", url)
        status = 500 if FakeClient.fail_whatsapp and "graph.facebook" in url else 200
        return httpx.Response(status, request=request, text="{}")


@pytest.fixture
def fake_http(monkeypatch):
    FakeClient.posts = []
    FakeClient.fail_whatsapp = False
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(router.settings, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(router.settings, "WHATSAPP_TOKEN", "wa_test")
    monkeypatch.setattr(router.settings, "WHATSAPP_PHONE_NUMBER_ID", "12345")

    async def summary(*_args, **_kwargs):
        return "Mario wants to view Via Roma 1\nthis week."

    monkeypatch.setattr(router, "_generate_lead_summary", summary)
    monkeypatch.setattr(router, "_format_lead_body", lambda *a, **k: "detail")
    monkeypatch.setattr(router, "_resolve_callback_number", lambda s: "+393331112222")
    return FakeClient


def _session(listings, locale="it"):
    return {
        "tenant_id": TENANT,
        "lead_email": INBOX,
        "locale": locale,
        "interested_listings": listings,
        "listings_shown": list(listings),
    }


def _whatsapp_posts(client):
    return [body for url, body in client.posts if "graph.facebook.com" in url]


def _email_posts(client):
    return [body for url, body in client.posts if "resend.com" in url]


# ── number normalization ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+39 333 123 4567", "+393331234567"),
        ("0039 333-123-4567", "+393331234567"),
        ("+421 (903) 123.456", "+421903123456"),
        ("", ""),
        ("   ", ""),
        ("333 1234567", None),  # national form: country unknown
        ("+0 333 1234567", None),
        ("+39 abc", None),
        ("+1234", None),
    ],
)
def test_normalize_number(raw, expected):
    assert whatsapp.normalize_number(raw) == expected


# ── the send ────────────────────────────────────────────────────────────────
def test_routed_lead_reaches_the_agent_on_whatsapp_and_by_email(fake_http):
    mario = agents_db.create(TENANT, "Mario", "mario@studio.it", whatsapp="+393330000001")
    listing = listings_db.create_manual(TENANT, {"address": "Via Roma 1"}, agent_id=mario["id"])

    asyncio.run(router._send_lead_email(_session([listing], locale="sk")))

    (alert,) = _whatsapp_posts(fake_http)
    assert alert["to"] == "393330000001"
    assert alert["type"] == "template"
    assert alert["template"]["language"] == {"code": "sk"}
    params = alert["template"]["components"][0]["parameters"]
    # Meta rejects newlines inside template parameters, and named parameters
    # sent without their name.
    assert [(p["parameter_name"], p["text"]) for p in params] == [
        ("a", "+393331112222"), ("b", "Mario wants to view Via Roma 1 this week."),
    ]
    (email,) = _email_posts(fake_http)
    assert email["to"] == ["mario@studio.it"]


def test_agent_without_whatsapp_gets_only_the_email(fake_http):
    mario = agents_db.create(TENANT, "Mario", "mario@studio.it", whatsapp="+393330000001")
    lucia = agents_db.create(TENANT, "Lucia", "lucia@studio.it")
    first = listings_db.create_manual(TENANT, {"address": "Via Roma 1"}, agent_id=mario["id"])
    second = listings_db.create_manual(TENANT, {"address": "Via Po 2"}, agent_id=lucia["id"])

    asyncio.run(router._send_lead_email(_session([first, second])))

    assert [a["to"] for a in _whatsapp_posts(fake_http)] == ["393330000001"]
    assert _email_posts(fake_http)[0]["to"] == ["mario@studio.it", "lucia@studio.it"]


AGENCY_WA = "+393339999999"


def test_call_touching_no_agent_goes_to_the_agency_number(fake_http):
    session = {**_session([]), "lead_whatsapp": AGENCY_WA}

    asyncio.run(router._send_lead_email(session))

    assert [a["to"] for a in _whatsapp_posts(fake_http)] == ["393339999999"]
    assert _email_posts(fake_http)[0]["to"] == [INBOX]


def test_unassigned_listing_goes_to_the_agency_number(fake_http):
    listing = listings_db.create_manual(TENANT, {"address": "Via Roma 1"})
    session = {**_session([listing]), "lead_whatsapp": AGENCY_WA}

    asyncio.run(router._send_lead_email(session))

    assert [a["to"] for a in _whatsapp_posts(fake_http)] == ["393339999999"]


def test_agency_number_is_not_copied_on_an_agent_lead(fake_http):
    mario = agents_db.create(TENANT, "Mario", "mario@studio.it", whatsapp="+393330000001")
    listing = listings_db.create_manual(TENANT, {"address": "Via Roma 1"}, agent_id=mario["id"])
    session = {**_session([listing]), "lead_whatsapp": AGENCY_WA}

    asyncio.run(router._send_lead_email(session))

    assert [a["to"] for a in _whatsapp_posts(fake_http)] == ["393330000001"]


def test_agent_without_a_number_falls_back_to_the_agency_number(fake_http):
    mario = agents_db.create(TENANT, "Mario", "mario@studio.it")
    listing = listings_db.create_manual(TENANT, {"address": "Via Roma 1"}, agent_id=mario["id"])
    session = {**_session([listing]), "lead_whatsapp": AGENCY_WA}

    asyncio.run(router._send_lead_email(session))

    assert [a["to"] for a in _whatsapp_posts(fake_http)] == ["393339999999"]


def test_no_agency_number_and_no_agent_sends_no_whatsapp(fake_http):
    asyncio.run(router._send_lead_email(_session([])))

    assert _whatsapp_posts(fake_http) == []
    assert _email_posts(fake_http)[0]["to"] == [INBOX]


def test_failed_whatsapp_still_sends_the_email(fake_http):
    fake_http.fail_whatsapp = True
    mario = agents_db.create(TENANT, "Mario", "mario@studio.it", whatsapp="+393330000001")
    listing = listings_db.create_manual(TENANT, {"address": "Via Roma 1"}, agent_id=mario["id"])

    asyncio.run(router._send_lead_email(_session([listing])))

    assert len(_whatsapp_posts(fake_http)) == 1
    assert len(_email_posts(fake_http)) == 1


def test_whatsapp_goes_out_even_without_resend(fake_http, monkeypatch):
    monkeypatch.setattr(router.settings, "RESEND_API_KEY", None)
    mario = agents_db.create(TENANT, "Mario", "mario@studio.it", whatsapp="+393330000001")
    listing = listings_db.create_manual(TENANT, {"address": "Via Roma 1"}, agent_id=mario["id"])

    asyncio.run(router._send_lead_email(_session([listing])))

    assert len(_whatsapp_posts(fake_http)) == 1
    assert _email_posts(fake_http) == []


def test_unconfigured_whatsapp_is_skipped(fake_http, monkeypatch):
    monkeypatch.setattr(router.settings, "WHATSAPP_TOKEN", None)
    mario = agents_db.create(TENANT, "Mario", "mario@studio.it", whatsapp="+393330000001")
    listing = listings_db.create_manual(TENANT, {"address": "Via Roma 1"}, agent_id=mario["id"])

    asyncio.run(router._send_lead_email(_session([listing])))

    assert _whatsapp_posts(fake_http) == []
    assert len(_email_posts(fake_http)) == 1


def test_whatsapp_number_is_editable_and_clearable():
    mario = agents_db.create(TENANT, "Mario", "mario@studio.it")
    assert mario["whatsapp"] == ""

    updated = agents_db.update(mario["id"], TENANT, {"whatsapp": "+393330000001"})
    assert updated["whatsapp"] == "+393330000001"
    cleared = agents_db.update(mario["id"], TENANT, {"whatsapp": ""})
    assert cleared["whatsapp"] == ""


# ── the dashboard API ────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from branches import db as branches_db
    from dashboard import router as dashboard_router

    monkeypatch.setattr(branches_db, "_initialized", False)
    branches_db.init()
    app = FastAPI()
    app.include_router(dashboard_router.router)
    app.dependency_overrides[dashboard_router.current_tenant] = lambda: {
        "id": TENANT, "agency_name": "Studio A", "plan": "Base", "active": 1,
    }
    return TestClient(app)


def test_dashboard_stores_the_number_normalized(client):
    created = client.post(
        "/dashboard/api/agents",
        json={"name": "Mario", "email": "mario@studio.it", "whatsapp": "0039 333 000 0001"},
    ).json()
    assert created["whatsapp"] == "+393330000001"

    patched = client.patch(
        "/dashboard/api/agents/" + created["id"], json={"whatsapp": ""}
    ).json()
    assert patched["whatsapp"] == ""


def test_dashboard_rejects_a_national_number(client):
    resp = client.post(
        "/dashboard/api/agents",
        json={"name": "Mario", "email": "mario@studio.it", "whatsapp": "333 0000001"},
    )
    assert resp.status_code == 422


def test_dashboard_agent_without_whatsapp_still_works(client):
    created = client.post(
        "/dashboard/api/agents", json={"name": "Mario", "email": "mario@studio.it"}
    ).json()
    assert created["whatsapp"] == ""


# ── the caller's e-mail ─────────────────────────────────────────────────────
@pytest.mark.parametrize("tool", ["caller_info", "left_message"])
def test_caller_email_rides_along_with_the_number(fake_http, tool):
    mario = agents_db.create(TENANT, "Mario", "mario@studio.it", whatsapp="+393330000001")
    listing = listings_db.create_manual(TENANT, {"address": "Via Roma 1"}, agent_id=mario["id"])
    session = _session([listing])
    session[tool] = {"email": "jan.novak@gmail.com"}

    asyncio.run(router._send_lead_email(session))

    (alert,) = _whatsapp_posts(fake_http)
    contact = alert["template"]["components"][0]["parameters"][0]["text"]
    assert contact == "+393331112222 · jan.novak@gmail.com"

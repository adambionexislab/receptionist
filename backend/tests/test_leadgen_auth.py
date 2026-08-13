"""Tests for the staff login on the internal lead-gen dashboard
(leadgen/router.py, leadgen/session.py).

What's behind this login isn't just data to read: it starts Google Places
scrapes, sends cold email from our own domain, and mints OpenAI credentials for
the notes tool. So the questions worth pinning are "can anyone reach it without
logging in", "does an unset password fail open", and "does guessing stay cheap"
— not just "does the right password work".
"""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import settings
from dashboard import session as agency_session
from leadgen import db as leadgen_db
from leadgen import router as leadgen_router
from leadgen import session as sess
from routers import leads as leads_module
from salesnotes import db as notes_db
from salesnotes import router as notes_router
from tenants import db as tenants_db

PASSWORD = "un-buon-segreto-condiviso"


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    monkeypatch.setattr(leadgen_db, "_initialized", False)
    monkeypatch.setattr(notes_db, "_initialized", False)
    leadgen_db.init()
    notes_db.init()
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "LEADGEN_PASSWORD", PASSWORD)
    monkeypatch.setattr(settings, "SESSION_SECRET", "test-signing-key")
    # The real 1s penalty per wrong password would make the lockout test crawl.
    monkeypatch.setattr(leadgen_router, "_FAIL_DELAY_SECONDS", 0)
    # Module-level and shared between tests, so a lockout can't leak forward.
    leadgen_router._failures.clear()
    yield
    leadgen_router._failures.clear()


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(leadgen_router.router)
    app.include_router(leads_module.router)
    app.include_router(leads_module.webhook_router)
    app.include_router(notes_router.router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _login(client, password=PASSWORD):
    return client.post("/leadgen/login", json={"password": password})


# ── the door ─────────────────────────────────────────────────────────────────
def test_the_right_password_opens_a_session(client):
    resp = _login(client)

    assert resp.status_code == 200
    assert sess.COOKIE_NAME in resp.cookies
    # And that session is what the dashboard's boot check reads.
    assert client.get("/leadgen/api/me").json() == {"authenticated": True}


def test_a_logged_in_session_reaches_the_data(client):
    _login(client)

    assert client.get("/campaigns").status_code == 200
    assert client.get("/notes").status_code == 200


def test_the_wrong_password_opens_nothing(client):
    resp = _login(client, "quasi-giusta")

    assert resp.status_code == 401
    assert sess.COOKIE_NAME not in resp.cookies
    assert client.get("/leadgen/api/me").json() == {"authenticated": False}


def test_logging_out_ends_the_session(client):
    _login(client)

    client.post("/leadgen/logout")

    assert client.get("/leadgen/api/me").json() == {"authenticated": False}
    assert client.get("/campaigns").status_code == 401


# ── nothing is reachable without it ──────────────────────────────────────────
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/campaigns"),
        ("post", "/campaigns"),
        ("get", "/campaigns/1/leads"),
        ("get", "/campaigns/1/logs"),
        ("patch", "/campaigns/1/status"),
        ("post", "/leads/response"),
        ("get", "/outreach/template"),
        ("put", "/outreach/template"),
        ("post", "/outreach/test"),
        ("get", "/outreach/exclusions"),
        ("get", "/notes"),
        ("post", "/notes"),
        ("get", "/notes/resumable"),
    ],
)
def test_every_endpoint_is_private_without_a_session(client, method, path):
    # A body is sent regardless of verb: the 401 has to come from the missing
    # session, not from a validation error that would mask an open endpoint.
    resp = client.request(method.upper(), path, json={})
    assert resp.status_code == 401, f"{method.upper()} {path} was reachable"


def test_minting_openai_credentials_needs_a_session(client):
    """The costliest endpoint of the lot: it hands the browser a live OpenAI
    client secret."""
    assert client.post("/notes/any-id/session-token").status_code == 401


def test_a_forged_cookie_is_not_a_session(client):
    client.cookies.set(sess.COOKIE_NAME, "bm90LWEtcGF5bG9hZA.bm90LWEtc2ln")

    assert client.get("/leadgen/api/me").json() == {"authenticated": False}
    assert client.get("/campaigns").status_code == 401


def test_an_agency_cookie_is_not_a_staff_cookie(client):
    """Both cookies are signed with the same key. An agency session must still
    never be accepted here — that would hand a customer the whole prospecting
    pipeline, including which of their competitors we're emailing."""
    client.cookies.set(sess.COOKIE_NAME, agency_session.issue("tenant-a"))

    assert client.get("/leadgen/api/me").json() == {"authenticated": False}
    assert client.get("/campaigns").status_code == 401


def test_an_expired_session_stops_working(client, monkeypatch):
    monkeypatch.setattr(sess, "_MAX_AGE", -1)
    cookie = sess.issue()
    client.cookies.set(sess.COOKIE_NAME, cookie)

    assert client.get("/campaigns").status_code == 401


# ── failure modes ────────────────────────────────────────────────────────────
def test_an_unset_password_locks_everyone_out_rather_than_letting_them_in(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "LEADGEN_PASSWORD", None)

    assert _login(client, "").status_code == 503
    assert _login(client, "qualsiasi").status_code == 503
    assert client.get("/campaigns").status_code == 401


def test_guessing_gets_locked_out(client):
    for _ in range(leadgen_router._MAX_FAILURES):
        assert _login(client, "sbagliata").status_code == 401

    # Locked out — and the lockout holds even for the correct password, so a
    # guesser can't tell from the response whether they finally got it right.
    assert _login(client, "sbagliata").status_code == 429
    assert _login(client).status_code == 429


def test_a_successful_login_clears_the_failure_count(client):
    for _ in range(leadgen_router._MAX_FAILURES - 1):
        _login(client, "sbagliata")

    assert _login(client).status_code == 200

    # A typo before a successful login must not leave the next session one
    # mistake away from a lockout.
    assert _login(client, "sbagliata").status_code == 401


# ── the deliberate exceptions ────────────────────────────────────────────────
def test_the_page_itself_loads_logged_out(client):
    """It has to: it's the thing that shows the login form. It carries no data."""
    resp = client.get("/leadgen")

    assert resp.status_code == 200
    assert "loginScreen" in resp.text


def test_the_resend_webhook_stays_open(client):
    """Resend has no session and can't get one. It authenticates by signing the
    body instead (see _verify_resend_signature), so this route must stay off
    the staff-login router — but it must also never be reachable by accident,
    which is why it lives on its own router."""
    resp = client.post("/leads/inbound-email", json={"type": "email.delivered"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}

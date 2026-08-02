"""Tests for assigning a listing's agent from the dashboard (dashboard/router.py).

The endpoint has to keep two things straight: an assignment is not an edit (it
must not freeze the row against the portal scrape), and an agent id is only
accepted from the tenant that owns it.
"""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents import db as agents_db
from dashboard import router as dashboard_router
from listings import db as listings_db
from tenants import db as tenants_db

TENANT = {"id": "tenant-a", "agency_name": "Studio A", "active": 1}
OTHER_TENANT = "tenant-b"


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


@pytest.fixture
def client():
    """The dashboard routes with authentication stubbed to one fixed tenant —
    login itself is covered by the session layer, not here."""
    app = FastAPI()
    app.include_router(dashboard_router.router)
    app.dependency_overrides[dashboard_router.current_tenant] = lambda: TENANT
    return TestClient(app)


def _patch(client, listing_id, payload):
    return client.patch(f"/dashboard/api/listings/{listing_id}", json=payload)


def test_assigning_an_agent_does_not_mark_the_listing_edited(client):
    agent = agents_db.create(TENANT["id"], "Mario Rossi", "mario@studio.it")
    listings_db.replace_scraped(TENANT["id"], [{"address": "Via Roma 5", "price": 100}])
    listing = listings_db.list_for_tenant(TENANT["id"])[0]

    resp = _patch(client, listing["id"], {"agent_id": agent["id"]})

    assert resp.status_code == 200
    assert resp.json()["agent_id"] == agent["id"]
    # edited stays False, so the scrape keeps refreshing this listing's data.
    assert resp.json()["edited"] is False


def test_editing_fields_and_assigning_at_once_applies_both(client):
    agent = agents_db.create(TENANT["id"], "Mario Rossi", "mario@studio.it")
    listing = listings_db.create_manual(TENANT["id"], {"address": "Via Roma 1"})

    resp = _patch(client, listing["id"], {"price": 250000, "agent_id": agent["id"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["price"] == 250000
    assert body["agent_id"] == agent["id"]
    assert body["edited"] is True  # the price change IS an edit


def test_empty_agent_id_unassigns(client):
    agent = agents_db.create(TENANT["id"], "Mario Rossi", "mario@studio.it")
    listing = listings_db.create_manual(
        TENANT["id"], {"address": "Via Roma 1"}, agent_id=agent["id"]
    )

    resp = _patch(client, listing["id"], {"agent_id": ""})

    assert resp.status_code == 200
    assert resp.json()["agent_id"] is None


def test_another_tenants_agent_is_rejected(client):
    outsider = agents_db.create(OTHER_TENANT, "Ján Novák", "jan@studio.sk")
    listing = listings_db.create_manual(TENANT["id"], {"address": "Via Roma 1"})

    resp = _patch(client, listing["id"], {"agent_id": outsider["id"]})

    assert resp.status_code == 422
    assert listings_db.get(listing["id"], TENANT["id"])["agent_id"] is None


def test_an_edit_cannot_blank_the_address(client):
    """Without an address the phone agent can never match a caller to it."""
    listing = listings_db.create_manual(TENANT["id"], {"address": "Via Roma 1"})

    assert _patch(client, listing["id"], {"address": "  "}).status_code == 422
    assert listings_db.get(listing["id"], TENANT["id"])["address"] == "Via Roma 1"


def test_unknown_listing_is_a_404(client):
    agent = agents_db.create(TENANT["id"], "Mario Rossi", "mario@studio.it")
    assert _patch(client, "no-such-listing", {"agent_id": agent["id"]}).status_code == 404
    assert _patch(client, "no-such-listing", {"price": 1}).status_code == 404


def test_agents_list_carries_each_agents_listing_count(client):
    agent = agents_db.create(TENANT["id"], "Mario Rossi", "mario@studio.it")
    listings_db.create_manual(TENANT["id"], {"address": "Via A 1"}, agent_id=agent["id"])
    listings_db.create_manual(TENANT["id"], {"address": "Via B 2"}, agent_id=agent["id"])
    listings_db.create_manual(TENANT["id"], {"address": "Via C 3"})

    rows = client.get("/dashboard/api/agents").json()["agents"]

    assert [(r["number"], r["listing_count"]) for r in rows] == [(1, 2)]

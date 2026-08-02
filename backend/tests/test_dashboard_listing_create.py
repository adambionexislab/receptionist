"""Tests for adding a listing by hand from the dashboard (dashboard/router.py).

A manually added listing is the agency's own: it isn't on the portal, so it is
stored source='manual' and no scrape may ever overwrite or remove it. The
address is the one required field — it is what the phone agent matches a
caller's spoken street against.
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
    app = FastAPI()
    app.include_router(dashboard_router.router)
    app.dependency_overrides[dashboard_router.current_tenant] = lambda: TENANT
    return TestClient(app)


def _create(client, **fields):
    payload = {"address": "Via Roma 5", **fields}
    return client.post("/dashboard/api/listings", json=payload)


def test_a_hand_added_listing_is_manual_and_shows_up_for_the_phone_agent(client):
    resp = _create(
        client, zone="lodi", type="affitto", rooms=3, size_sqm=80,
        price=900, text="Trilocale luminoso.",
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["address"] == "Via Roma 5"
    assert body["type"] == "affitto"
    assert body["price"] == 900
    # source='manual' is what makes a scrape leave it alone (listings/db.py).
    assert body["source"] == "manual"
    assert [r["address"] for r in listings_db.list_for_tenant(TENANT["id"])] == ["Via Roma 5"]


def test_only_the_address_is_required(client):
    resp = client.post("/dashboard/api/listings", json={"address": "Via Sola 1"})

    assert resp.status_code == 201
    body = resp.json()
    assert (body["zone"], body["rooms"], body["price"], body["text"]) == ("", 0, 0, "")
    assert body["type"] == "vendita"      # the common case
    assert body["available"] is True      # offerable the moment it's saved
    assert body["agent_id"] is None       # leads go to the agency inbox


def test_a_blank_address_is_rejected(client):
    assert client.post("/dashboard/api/listings", json={}).status_code == 422
    assert _create(client, address="").status_code == 422
    assert _create(client, address="   ").status_code == 422
    assert listings_db.list_for_tenant(TENANT["id"]) == []


def test_it_can_be_assigned_to_an_agent_on_creation(client):
    agent = agents_db.create(TENANT["id"], "Mario Rossi", "mario@studio.it")

    resp = _create(client, agent_id=agent["id"])

    assert resp.status_code == 201
    assert resp.json()["agent_id"] == agent["id"]
    assert listings_db.count_for_agent(TENANT["id"], agent["id"]) == 1


def test_an_empty_agent_id_just_means_unassigned(client):
    resp = _create(client, agent_id="")

    assert resp.status_code == 201
    assert resp.json()["agent_id"] is None


def test_another_tenants_agent_is_rejected(client):
    outsider = agents_db.create(OTHER_TENANT, "Ján Novák", "jan@studio.sk")

    resp = _create(client, agent_id=outsider["id"])

    assert resp.status_code == 422
    # Nothing half-created.
    assert listings_db.list_for_tenant(TENANT["id"]) == []


def test_an_unknown_type_is_rejected_rather_than_silently_coerced(client):
    assert _create(client, type="baratto").status_code == 422


def test_a_scrape_never_touches_a_hand_added_listing(client):
    """The reason manual listings exist: the agency's own property must not be
    wiped by the next Immobiliare.it run just for being absent from it."""
    created = _create(client, price=250000).json()

    listings_db.replace_scraped(
        TENANT["id"],
        [{"address": "Via Portale 9", "zone": "lodi", "type": "vendita", "rooms": 2,
          "size_sqm": 60, "price": 120000, "currency": "EUR", "available": True,
          "text": "dal portale"}],
    )

    still_there = listings_db.get(created["id"], TENANT["id"])
    assert still_there is not None
    assert still_there["price"] == 250000

"""Tests for choosing the handling agent when confirming a seller meeting
(acquisizione/router.py).

The meeting is the moment it's known who will handle the property, so the
choice is required and the published listing is born assigned — its first
caller then reaches that agent rather than the agency inbox. Confirming is a
one-shot state transition, so a bad agent id must be rejected before it burns
the record.
"""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from acquisizione import db as acq_db
from acquisizione import router as acq_router
from agents import db as agents_db
from dashboard.router import current_tenant
from listings import db as listings_db
from tenants import db as tenants_db

TENANT = {"id": "tenant-a", "agency_name": "Studio A", "active": 1, "locale": "it"}
OTHER_TENANT = "tenant-b"

_FIELDS = {
    "tipo_annuncio": "vendita",
    "indirizzo_o_zona": "Via Roma 5, Lodi",
    "superficie_mq": 85,
    "prezzo_richiesto": 250000,
}


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    for module in (agents_db, listings_db, acq_db):
        monkeypatch.setattr(module, "_initialized", False)
    agents_db.init()
    listings_db.init()
    acq_db.init()
    # The confirm response emails the agency its copy of the meeting; that's
    # covered by test_acquisizione_notify and needs no network here.
    async def _no_email(tenant, record):
        return True
    monkeypatch.setattr(acq_router.notify, "send_meeting_summary", _no_email)
    yield conn
    conn.close()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(acq_router.router)
    app.dependency_overrides[current_tenant] = lambda: TENANT
    return TestClient(app)


def _record_awaiting_confirmation():
    """An intake record in the 'review' state the confirm step expects."""
    record = acq_db.create(TENANT["id"], "it", "checkbox")
    acq_db.set_review_result(
        record["id"], TENANT["id"], _FIELDS, [], "Bell'appartamento.", []
    )
    return record


def _confirm(client, record_id, **extra):
    payload = {
        "listing_fields": _FIELDS,
        "listing_text": "Bell'appartamento.",
        "tasks": [],
        **extra,
    }
    return client.patch(f"/acquisizione/{record_id}/confirm", json=payload)


def test_confirmed_listing_is_published_assigned_to_the_chosen_agent(client):
    agent = agents_db.create(TENANT["id"], "Mario Rossi", "mario@studio.it")
    record = _record_awaiting_confirmation()

    resp = _confirm(client, record["id"], agent_id=agent["id"])

    assert resp.status_code == 200
    assert resp.json()["published"] is True
    (listing,) = listings_db.list_for_tenant(TENANT["id"])
    assert listing["address"] == "Via Roma 5, Lodi"
    assert listing["agent_id"] == agent["id"]


def test_confirming_without_an_agent_is_rejected(client):
    agents_db.create(TENANT["id"], "Mario Rossi", "mario@studio.it")
    record = _record_awaiting_confirmation()

    assert _confirm(client, record["id"]).status_code == 422              # omitted
    assert _confirm(client, record["id"], agent_id="").status_code == 422  # blank
    assert _confirm(client, record["id"], agent_id="   ").status_code == 422

    # Nothing was consumed: the record is still confirmable and no half-made
    # listing was published.
    assert acq_db.get(record["id"], TENANT["id"])["status"] == "review"
    assert listings_db.list_for_tenant(TENANT["id"]) == []


def test_another_tenants_agent_is_rejected(client):
    outsider = agents_db.create(OTHER_TENANT, "Ján Novák", "jan@studio.sk")
    record = _record_awaiting_confirmation()

    resp = _confirm(client, record["id"], agent_id=outsider["id"])

    assert resp.status_code == 422
    assert acq_db.get(record["id"], TENANT["id"])["status"] == "review"
    assert listings_db.list_for_tenant(TENANT["id"]) == []


def test_unknown_agent_id_is_rejected(client):
    record = _record_awaiting_confirmation()
    assert _confirm(client, record["id"], agent_id="no-such-agent").status_code == 422
    assert acq_db.get(record["id"], TENANT["id"])["status"] == "review"

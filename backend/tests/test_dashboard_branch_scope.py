"""Tests for the dashboard's branch scope (dashboard/router.py).

The rules that matter here: the branch arrives as a header and is always
resolved against the logged-in tenant (so another agency's id is a 404, not a
filter), every list the page shows follows it, and the billing block belongs to
the agency alone — a branch view reports what one office used, never what it
owes.
"""

import datetime
import sqlite3
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents import db as agents_db
from branches import db as branches_db
from calls import db as calls_db
from dashboard import router as dashboard_router
from listings import db as listings_db
from tenants import db as tenants_db
from usage import db as usage_db

# No billing_anchor and no created_at: the period falls back to the calendar
# month, which is all these tests need — everything they record is stamped now.
TENANT = {"id": "tenant-a", "agency_name": "Studio A", "plan": "Base", "active": 1}
OTHER_TENANT = "tenant-b"


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    for module in (agents_db, branches_db, calls_db, listings_db, usage_db):
        monkeypatch.setattr(module, "_initialized", False)
        module.init()
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


def scoped(branch_id):
    """The header the page sends for the office it is showing."""
    return {dashboard_router.BRANCH_HEADER: branch_id}


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def record_call(branch_id=None, seconds=60, name="Chiara"):
    """One handled call plus the contact it produced, both stamped now so they
    land inside the current billing period."""
    stamp = now_iso()
    call_id = calls_db.add_call_session(
        tenant_id=TENANT["id"], call_id="c-" + str(uuid.uuid4())[:8],
        caller_number="+39000", started_at=stamp, ended_at=stamp,
        duration_seconds=seconds, locale="it", outcome="lead", summary="...",
        branch_id=branch_id,
    )
    calls_db.add_contact(
        tenant_id=TENANT["id"], call_session_id=call_id, name=name, phone="+39000",
        interest=None, summary="...", details=None, created_at=stamp,
        branch_id=branch_id,
    )


# -- resolving the header ---------------------------------------------------
def test_an_unknown_branch_id_is_rejected(client):
    resp = client.get("/dashboard/api/summary", headers=scoped(str(uuid.uuid4())))
    assert resp.status_code == 404


def test_another_agencys_branch_cannot_be_used_as_a_filter(client):
    theirs = branches_db.create(OTHER_TENANT, "Bratislava")
    resp = client.get("/dashboard/api/summary", headers=scoped(theirs["id"]))
    assert resp.status_code == 404


def test_no_header_means_the_whole_agency(client):
    branches_db.create(TENANT["id"], "Milano")
    record_call(seconds=120)

    body = client.get("/dashboard/api/summary").json()

    assert body["scope"] == "agency"
    # The billing block is unchanged for the agency view — this is the one that
    # bills, and its shape is what the credits cards read.
    assert body["credits"]["minutes"]["included"] == 500
    assert body["minutes"] == 2


# -- what a branch view counts ----------------------------------------------
def test_a_branch_summary_counts_only_that_offices_calls(client):
    milano = branches_db.create(TENANT["id"], "Milano")
    roma = branches_db.create(TENANT["id"], "Roma")
    record_call(branch_id=milano["id"], seconds=120)
    record_call(branch_id=roma["id"], seconds=300)
    # A call nobody's listing was involved in belongs to no office.
    record_call(branch_id=None, seconds=60)

    body = client.get("/dashboard/api/summary", headers=scoped(milano["id"])).json()

    assert body["scope"] == "branch"
    assert body["branch"]["name"] == "Milano"
    assert (body["minutes"], body["calls"], body["contacts"]) == (2, 1, 1)
    # The agency's own totals ride along so the card can say "2 of 8".
    assert body["agency"]["minutes"] == 8


def test_a_branch_view_reports_spend_and_never_an_allowance(client):
    milano = branches_db.create(TENANT["id"], "Milano")
    usage_db.record(TENANT["id"], "photo", branch_id=milano["id"])
    usage_db.record(TENANT["id"], "photo", branch_id=milano["id"])
    usage_db.record(TENANT["id"], "meeting", "rec-1")  # agency-wide, no office

    body = client.get("/dashboard/api/summary", headers=scoped(milano["id"])).json()

    assert body["tools"]["used_cents"] == 100
    assert body["tools"]["uses"]["photo"] == 2
    assert body["agency"]["tools_used_cents"] == 200
    # No allowance, no remaining, no overage: those belong to the subscription,
    # which is the agency's. Inventing a per-office one would read as a bill.
    assert "credits" not in body


def test_contacts_are_filtered_by_branch(client):
    milano = branches_db.create(TENANT["id"], "Milano")
    record_call(branch_id=milano["id"], name="Chiara")
    record_call(branch_id=None, name="Davide")

    scoped_resp = client.get("/dashboard/api/contacts", headers=scoped(milano["id"]))
    all_resp = client.get("/dashboard/api/contacts")

    assert [c["name"] for c in scoped_resp.json()["contacts"]] == ["Chiara"]
    assert sorted(c["name"] for c in all_resp.json()["contacts"]) == ["Chiara", "Davide"]


def test_agents_are_filtered_by_branch(client):
    milano = branches_db.create(TENANT["id"], "Milano")
    agents_db.create(TENANT["id"], "Mario Rossi", "mario@studio.it", milano["id"])
    agents_db.create(TENANT["id"], "Lucia Bianchi", "lucia@studio.it")

    rows = client.get(
        "/dashboard/api/agents", headers=scoped(milano["id"])
    ).json()["agents"]

    assert [a["name"] for a in rows] == ["Mario Rossi"]
    assert len(client.get("/dashboard/api/agents").json()["agents"]) == 2


def test_listings_follow_the_branch_of_the_agent_handling_them(client):
    milano = branches_db.create(TENANT["id"], "Milano")
    mario = agents_db.create(TENANT["id"], "Mario Rossi", "mario@studio.it", milano["id"])
    lucia = agents_db.create(TENANT["id"], "Lucia Bianchi", "lucia@studio.it")
    listings_db.create_manual(TENANT["id"], {"address": "Via Milano 1"}, mario["id"])
    listings_db.create_manual(TENANT["id"], {"address": "Via Roma 2"}, lucia["id"])
    listings_db.create_manual(TENANT["id"], {"address": "Via Senza Agente 3"})

    rows = client.get(
        "/dashboard/api/listings", headers=scoped(milano["id"])
    ).json()["listings"]

    assert [l["address"] for l in rows] == ["Via Milano 1"]
    # An unassigned listing belongs to no office, and is still there for the
    # agency view — it must not be filtered out of existence.
    assert len(client.get("/dashboard/api/listings").json()["listings"]) == 3


# -- putting an agent in an office ------------------------------------------
def test_an_agent_added_from_a_branch_view_joins_that_branch(client):
    milano = branches_db.create(TENANT["id"], "Milano")

    created = client.post(
        "/dashboard/api/agents",
        json={"name": "Mario Rossi", "email": "mario@studio.it"},
        headers=scoped(milano["id"]),
    ).json()

    # Otherwise they would vanish from the very view they were added in.
    assert created["branch_id"] == milano["id"]


def test_an_explicit_branch_beats_the_selected_one(client):
    milano = branches_db.create(TENANT["id"], "Milano")
    roma = branches_db.create(TENANT["id"], "Roma")

    created = client.post(
        "/dashboard/api/agents",
        json={
            "name": "Mario Rossi",
            "email": "mario@studio.it",
            "branch_id": roma["id"],
        },
        headers=scoped(milano["id"]),
    ).json()

    assert created["branch_id"] == roma["id"]


def test_an_agent_can_be_moved_out_of_every_branch(client):
    milano = branches_db.create(TENANT["id"], "Milano")
    agent = agents_db.create(TENANT["id"], "Mario Rossi", "mario@studio.it", milano["id"])

    resp = client.patch(
        "/dashboard/api/agents/" + agent["id"], json={"branch_id": ""}
    )

    assert resp.status_code == 200
    assert resp.json()["branch_id"] is None


def test_a_patch_that_does_not_mention_the_branch_leaves_it_alone(client):
    milano = branches_db.create(TENANT["id"], "Milano")
    agent = agents_db.create(TENANT["id"], "Mario Rossi", "mario@studio.it", milano["id"])

    resp = client.patch(
        "/dashboard/api/agents/" + agent["id"], json={"name": "Mario Rossini"}
    )

    assert resp.json()["name"] == "Mario Rossini"
    assert resp.json()["branch_id"] == milano["id"]


def test_an_agent_cannot_be_put_in_another_agencys_branch(client):
    theirs = branches_db.create(OTHER_TENANT, "Bratislava")

    resp = client.post(
        "/dashboard/api/agents",
        json={
            "name": "Mario Rossi",
            "email": "mario@studio.it",
            "branch_id": theirs["id"],
        },
    )

    assert resp.status_code == 422


# -- the branches themselves ------------------------------------------------
def test_branches_carry_their_headcount_and_are_never_scope_filtered(client):
    milano = branches_db.create(TENANT["id"], "Milano")
    branches_db.create(TENANT["id"], "Roma")
    agents_db.create(TENANT["id"], "Mario Rossi", "mario@studio.it", milano["id"])

    # Asked for while looking at Milano: the switcher still has to offer Roma.
    rows = client.get(
        "/dashboard/api/branches", headers=scoped(milano["id"])
    ).json()["branches"]

    assert [(b["name"], b["agent_count"]) for b in rows] == [("Milano", 1), ("Roma", 0)]


def test_a_branch_needs_a_name(client):
    assert client.post("/dashboard/api/branches", json={"name": "  "}).status_code == 422


def test_closing_a_branch_leaves_its_agents_on_the_roster(client):
    milano = branches_db.create(TENANT["id"], "Milano")
    agents_db.create(TENANT["id"], "Mario Rossi", "mario@studio.it", milano["id"])

    assert client.delete("/dashboard/api/branches/" + milano["id"]).status_code == 200

    rows = client.get("/dashboard/api/agents").json()["agents"]
    assert [(a["name"], a["branch_id"]) for a in rows] == [("Mario Rossi", None)]


def test_another_agencys_branch_cannot_be_deleted(client):
    theirs = branches_db.create(OTHER_TENANT, "Bratislava")
    assert client.delete("/dashboard/api/branches/" + theirs["id"]).status_code == 404

"""Tests for the branches store (branches/db.py) and the agent side of it.

The rules that matter here: a branch is scoped to one tenant like everything
else, closing one detaches its agents rather than deleting them, and filtering
agents by branch never changes their agency-wide numbering.
"""

import sqlite3

import pytest

from agents import db as agents_db
from branches import db as branches_db
from listings import db as listings_db
from tenants import db as tenants_db

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    """Point the shared tenants connection at a throwaway in-memory DB, so
    these tests never touch the real data on disk.

    Listings are set up too: deleting an agent unassigns their listings, and
    that path runs through both tables.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    monkeypatch.setattr(agents_db, "_initialized", False)
    monkeypatch.setattr(branches_db, "_initialized", False)
    monkeypatch.setattr(listings_db, "_initialized", False)
    agents_db.init()
    branches_db.init()
    listings_db.init()
    yield conn
    conn.close()


def test_branches_are_scoped_to_one_tenant():
    mine = branches_db.create(TENANT, "Milano")
    branches_db.create(OTHER_TENANT, "Bratislava")

    assert [b["name"] for b in branches_db.list_for_tenant(TENANT)] == ["Milano"]
    # The other agency's id resolves to nothing here — this is what stops it
    # being usable as a filter (see dashboard.router.current_branch).
    theirs = branches_db.list_for_tenant(OTHER_TENANT)[0]
    assert branches_db.get(theirs["id"], TENANT) is None
    assert branches_db.get(mine["id"], TENANT)["name"] == "Milano"


def test_renaming_a_branch_keeps_its_agents():
    branch = branches_db.create(TENANT, "Milano")
    agent = agents_db.create(TENANT, "Mario Rossi", "mario@studio.it", branch["id"])

    renamed = branches_db.update(branch["id"], TENANT, {"name": "Milano Centro"})

    assert renamed["name"] == "Milano Centro"
    assert agents_db.get(agent["id"], TENANT)["branch_id"] == branch["id"]


def test_another_tenant_cannot_rename_or_delete_a_branch():
    branch = branches_db.create(TENANT, "Milano")

    assert branches_db.update(branch["id"], OTHER_TENANT, {"name": "Hijacked"}) is None
    assert branches_db.delete(branch["id"], OTHER_TENANT) is False
    assert branches_db.get(branch["id"], TENANT)["name"] == "Milano"


def test_closing_a_branch_detaches_its_agents_without_deleting_them():
    branch = branches_db.create(TENANT, "Milano")
    other = branches_db.create(TENANT, "Roma")
    stays = agents_db.create(TENANT, "Mario Rossi", "mario@studio.it", branch["id"])
    elsewhere = agents_db.create(TENANT, "Lucia Bianchi", "lucia@studio.it", other["id"])
    listing = listings_db.create_manual(TENANT, {"address": "Via Roma 1"}, stays["id"])

    assert branches_db.delete(branch["id"], TENANT) is True

    # The agent survives with no office, and keeps their listing — closing an
    # office must not silently reroute its leads to the agency inbox.
    detached = agents_db.get(stays["id"], TENANT)
    assert detached is not None
    assert detached["branch_id"] is None
    assert listings_db.get(listing["id"], TENANT)["agent_id"] == stays["id"]
    # The other office is untouched.
    assert agents_db.get(elsewhere["id"], TENANT)["branch_id"] == other["id"]


def test_agents_can_be_listed_per_branch_without_renumbering():
    milano = branches_db.create(TENANT, "Milano")
    roma = branches_db.create(TENANT, "Roma")
    agents_db.create(TENANT, "Mario Rossi", "mario@studio.it", milano["id"])
    second = agents_db.create(TENANT, "Lucia Bianchi", "lucia@studio.it", roma["id"])
    agents_db.create(TENANT, "Paolo Verdi", "paolo@studio.it", roma["id"])

    rows = agents_db.list_for_tenant(TENANT, branch_id=roma["id"])

    # #2 and #3 of the agency, not #1 and #2 of Roma: the number identifies the
    # agent agency-wide and a filtered view is a subset, never a renumbering.
    assert [(r["number"], r["name"]) for r in rows] == [
        (2, "Lucia Bianchi"), (3, "Paolo Verdi"),
    ]
    assert set(agents_db.ids_for_branch(TENANT, roma["id"])) == {
        second["id"], rows[1]["id"],
    }


def test_an_agent_can_be_moved_between_branches_and_out_of_all_of_them():
    milano = branches_db.create(TENANT, "Milano")
    roma = branches_db.create(TENANT, "Roma")
    agent = agents_db.create(TENANT, "Mario Rossi", "mario@studio.it", milano["id"])

    moved = agents_db.update(agent["id"], TENANT, {"branch_id": roma["id"]})
    assert moved["branch_id"] == roma["id"]
    assert agents_db.ids_for_branch(TENANT, milano["id"]) == []

    detached = agents_db.update(agent["id"], TENANT, {"branch_id": None})
    assert detached["branch_id"] is None
    assert agents_db.ids_for_branch(TENANT, roma["id"]) == []
    # Still on the agency's roster, just not in any office.
    assert len(agents_db.list_for_tenant(TENANT)) == 1


def test_an_agent_created_without_a_branch_has_none():
    agent = agents_db.create(TENANT, "Mario Rossi", "mario@studio.it")
    assert agent["branch_id"] is None
    assert agents_db.get(agent["id"], TENANT)["branch_id"] is None

"""Tests for the agency-agents store (agents/db.py).

The rules that matter here: numbering is per tenant and follows the order
agents are added, a deleted agent's number is freed for the next hire without
renumbering anyone else, and every read or write is scoped to one tenant.
"""

import sqlite3

import pytest

from agents import db as agents_db
from listings import db as listings_db
from tenants import db as tenants_db

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    """Point the shared tenants connection at a throwaway in-memory DB, so
    these tests never touch the real data on disk.

    Listings are set up here too: deleting an agent has to unassign their
    listings, so that path spans both tables.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    monkeypatch.setattr(agents_db, "_initialized", False)
    monkeypatch.setattr(listings_db, "_initialized", False)
    agents_db.init()
    listings_db.init()
    yield conn
    conn.close()


def test_numbers_follow_the_order_agents_are_added():
    agents_db.create(TENANT, "Mario Rossi", "mario@studio.it")
    agents_db.create(TENANT, "Lucia Bianchi", "lucia@studio.it")
    agents_db.create(TENANT, "Paolo Verdi", "paolo@studio.it")
    rows = agents_db.list_for_tenant(TENANT)
    assert [(r["number"], r["name"]) for r in rows] == [
        (1, "Mario Rossi"), (2, "Lucia Bianchi"), (3, "Paolo Verdi"),
    ]


def test_each_tenant_numbers_its_own_agents_from_one():
    agents_db.create(TENANT, "Mario Rossi", "mario@studio.it")
    first_of_other = agents_db.create(OTHER_TENANT, "Ján Novák", "jan@studio.sk")
    assert first_of_other["number"] == 1


def test_a_new_agent_fills_the_lowest_free_number():
    agents_db.create(TENANT, "Mario Rossi", "mario@studio.it")
    second = agents_db.create(TENANT, "Lucia Bianchi", "lucia@studio.it")
    agents_db.create(TENANT, "Paolo Verdi", "paolo@studio.it")

    assert agents_db.delete(second["id"], TENANT) is True

    # The survivors keep the numbers the agency already knows them by...
    assert [r["number"] for r in agents_db.list_for_tenant(TENANT)] == [1, 3]
    # ...and the hole at #2 is what the next hire gets, not #4.
    assert agents_db.create(TENANT, "Anna Neri", "anna@studio.it")["number"] == 2
    # Once the run is dense again, numbering carries on from the end.
    assert agents_db.create(TENANT, "Ugo Gialli", "ugo@studio.it")["number"] == 4


def test_update_changes_name_and_email_but_never_the_number():
    agent = agents_db.create(TENANT, "Mario Rossi", "mario@studio.it")
    updated = agents_db.update(
        agent["id"], TENANT, {"name": "Mario Rossi Jr", "email": "mr@studio.it", "number": 99}
    )
    assert updated["name"] == "Mario Rossi Jr"
    assert updated["email"] == "mr@studio.it"
    assert updated["number"] == agent["number"]


def test_get_many_resolves_ids_and_ignores_other_tenants():
    mario = agents_db.create(TENANT, "Mario Rossi", "mario@studio.it")
    lucia = agents_db.create(TENANT, "Lucia Bianchi", "lucia@studio.it")
    outsider = agents_db.create(OTHER_TENANT, "Ján Novák", "jan@studio.sk")

    found = agents_db.get_many([mario["id"], lucia["id"], outsider["id"], "ghost"], TENANT)

    # An id from another agency resolves to nothing, so a stale agent_id can
    # never route one tenant's lead to another tenant's agent.
    assert set(found) == {mario["id"], lucia["id"]}
    assert found[mario["id"]]["email"] == "mario@studio.it"
    assert agents_db.get_many([], TENANT) == {}


def test_deleting_an_agent_unassigns_their_listings():
    mario = agents_db.create(TENANT, "Mario Rossi", "mario@studio.it")
    lucia = agents_db.create(TENANT, "Lucia Bianchi", "lucia@studio.it")
    his = listings_db.create_manual(TENANT, {"address": "Via Roma 1"}, agent_id=mario["id"])
    hers = listings_db.create_manual(TENANT, {"address": "Via Po 2"}, agent_id=lucia["id"])

    assert agents_db.delete(mario["id"], TENANT) is True

    # His listing survives but is unassigned, so its leads fall back to the
    # agency inbox rather than pointing at a row that no longer exists.
    assert listings_db.get(his["id"], TENANT)["agent_id"] is None
    # Nobody else's assignment is disturbed.
    assert listings_db.get(hers["id"], TENANT)["agent_id"] == lucia["id"]


def test_a_recycled_number_does_not_inherit_the_old_agents_listings():
    """The reason listings store agent_id and not the number: #2 is handed to
    the next hire, and their predecessor's properties must not follow it."""
    agents_db.create(TENANT, "Mario Rossi", "mario@studio.it")
    lucia = agents_db.create(TENANT, "Lucia Bianchi", "lucia@studio.it")
    listing = listings_db.create_manual(
        TENANT, {"address": "Via Roma 1"}, agent_id=lucia["id"]
    )

    agents_db.delete(lucia["id"], TENANT)
    replacement = agents_db.create(TENANT, "Anna Neri", "anna@studio.it")

    assert replacement["number"] == 2  # same number Lucia had
    assert listings_db.get(listing["id"], TENANT)["agent_id"] is None


def test_reads_and_writes_are_scoped_to_one_tenant():
    agent = agents_db.create(TENANT, "Mario Rossi", "mario@studio.it")
    assert agents_db.list_for_tenant(OTHER_TENANT) == []
    assert agents_db.get(agent["id"], OTHER_TENANT) is None
    assert agents_db.update(agent["id"], OTHER_TENANT, {"name": "hijacked"}) is None
    assert agents_db.delete(agent["id"], OTHER_TENANT) is False
    # Untouched by the other tenant's attempts.
    assert agents_db.get(agent["id"], TENANT)["name"] == "Mario Rossi"

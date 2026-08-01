"""Tests for the agency-agents store (agents/db.py).

The rules that matter here: numbering is per tenant and follows the order
agents are added, a deleted agent's number is freed for the next hire without
renumbering anyone else, and every read or write is scoped to one tenant.
"""

import sqlite3

import pytest

from agents import db as agents_db
from tenants import db as tenants_db

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    """Point the shared tenants connection at a throwaway in-memory DB, so
    these tests never touch the real data on disk."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    monkeypatch.setattr(agents_db, "_initialized", False)
    agents_db.init()
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


def test_reads_and_writes_are_scoped_to_one_tenant():
    agent = agents_db.create(TENANT, "Mario Rossi", "mario@studio.it")
    assert agents_db.list_for_tenant(OTHER_TENANT) == []
    assert agents_db.get(agent["id"], OTHER_TENANT) is None
    assert agents_db.update(agent["id"], OTHER_TENANT, {"name": "hijacked"}) is None
    assert agents_db.delete(agent["id"], OTHER_TENANT) is False
    # Untouched by the other tenant's attempts.
    assert agents_db.get(agent["id"], TENANT)["name"] == "Mario Rossi"

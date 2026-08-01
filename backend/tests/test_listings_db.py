"""Tests for the listings persistence + scrape-merge rules (listings/db.py).

These cover the guarantees that stop a periodic Immobiliare.it scrape from
fighting the agency: agent edits aren't reverted, agent deletions aren't
resurrected, and Acquisizione-created listings are never touched at all.
"""

import sqlite3

import pytest

from listings import db as listings_db
from tenants import db as tenants_db

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    """Point the shared tenants connection at a throwaway in-memory DB, so
    these tests never touch the real listings on disk."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    monkeypatch.setattr(listings_db, "_initialized", False)
    listings_db.init()
    yield conn
    conn.close()


def _scraped(address, price=100000, text="from portal"):
    return {
        "address": address, "zone": "lodi", "type": "vendita", "rooms": 3,
        "size_sqm": 80, "price": price, "currency": "EUR", "available": True,
        "text": text,
    }


def test_scrape_inserts_then_updates_the_same_row():
    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5", price=100000)])
    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5", price=90000)])
    rows = listings_db.list_for_tenant(TENANT)
    assert len(rows) == 1  # matched by source_key, not duplicated
    assert rows[0]["price"] == 90000


def test_source_key_ignores_formatting_differences():
    """The same portal listing must map to the same row even if the scrape
    returns its address with different case/punctuation/accents."""
    assert listings_db.source_key("Via Roma 5") == listings_db.source_key("via roma, 5")
    assert listings_db.source_key("Košická 12") == listings_db.source_key("kosicka 12")


def test_scrape_removes_listings_that_vanished_from_the_portal():
    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5"), _scraped("Via Po 9")])
    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5")])
    addresses = [r["address"] for r in listings_db.list_for_tenant(TENANT)]
    assert addresses == ["Via Roma 5"]


def test_scrape_does_not_revert_an_agent_edit():
    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5", price=100000)])
    listing_id = listings_db.list_for_tenant(TENANT)[0]["id"]
    listings_db.update(listing_id, TENANT, {"price": 123456, "text": "agency copy"})

    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5", price=90000, text="from portal")])

    row = listings_db.get(listing_id, TENANT)
    assert row["price"] == 123456
    assert row["text"] == "agency copy"


def test_scrape_does_not_remove_an_edited_listing_that_left_the_portal():
    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5")])
    listing_id = listings_db.list_for_tenant(TENANT)[0]["id"]
    listings_db.update(listing_id, TENANT, {"text": "agency copy"})

    listings_db.replace_scraped(TENANT, [_scraped("Via Po 9")])

    assert listings_db.get(listing_id, TENANT) is not None


def test_scrape_does_not_resurrect_a_deleted_listing():
    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5")])
    listing_id = listings_db.list_for_tenant(TENANT)[0]["id"]
    assert listings_db.delete(listing_id, TENANT) is True

    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5")])

    assert listings_db.list_for_tenant(TENANT) == []


def test_manual_listings_survive_a_scrape():
    """An Acquisizione-created listing isn't on the portal, so a scrape that
    doesn't mention it must not delete it."""
    created = listings_db.create_manual(TENANT, {
        "address": "Via Nuova 1", "type": "affitto", "price": 900,
        "text": "Captured in a seller meeting.",
    })
    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5")])

    row = listings_db.get(created["id"], TENANT)
    assert row is not None
    assert row["source"] == "manual"
    assert row["text"] == "Captured in a seller meeting."


def test_empty_scrape_is_ignored_rather_than_wiping_inventory():
    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5")])
    listings_db.replace_scraped(TENANT, [])
    assert len(listings_db.list_for_tenant(TENANT)) == 1


def test_listings_are_scoped_per_tenant():
    listings_db.create_manual(TENANT, {"address": "Via A 1"})
    listings_db.create_manual(OTHER_TENANT, {"address": "Via B 2"})

    assert [r["address"] for r in listings_db.list_for_tenant(TENANT)] == ["Via A 1"]
    assert [r["address"] for r in listings_db.list_for_tenant(OTHER_TENANT)] == ["Via B 2"]

    # And one tenant can neither read nor mutate another's row.
    other_id = listings_db.list_for_tenant(OTHER_TENANT)[0]["id"]
    assert listings_db.get(other_id, TENANT) is None
    assert listings_db.update(other_id, TENANT, {"price": 1}) is None
    assert listings_db.delete(other_id, TENANT) is False


def test_a_scrape_for_one_tenant_leaves_another_tenants_rows_alone():
    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5")])
    listings_db.replace_scraped(OTHER_TENANT, [_scraped("Via Po 9")])
    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5")])
    assert len(listings_db.list_for_tenant(OTHER_TENANT)) == 1


def test_update_ignores_fields_that_are_not_agent_editable():
    created = listings_db.create_manual(TENANT, {"address": "Via A 1"})
    updated = listings_db.update(created["id"], TENANT, {
        "price": 500, "tenant_id": OTHER_TENANT, "source": "scrape", "deleted": 1,
    })
    assert updated["price"] == 500
    assert updated["source"] == "manual"
    assert listings_db.get(created["id"], TENANT) is not None


def test_update_coerces_numeric_strings_from_the_form():
    created = listings_db.create_manual(TENANT, {"address": "Via A 1"})
    updated = listings_db.update(created["id"], TENANT, {"price": "1200", "rooms": ""})
    assert updated["price"] == 1200
    assert updated["rooms"] == 0


# ── agent assignment ─────────────────────────────────────────────────────────
# Which agent handles a listing decides where its phone leads are emailed
# (call/router._resolve_lead_recipients), so the assignment has to survive the
# scrape without freezing the row the way an agent edit does.

def test_assigning_an_agent_does_not_freeze_the_row_against_the_scrape():
    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5", price=100000)])
    listing = listings_db.list_for_tenant(TENANT)[0]

    assigned = listings_db.set_agent(listing["id"], TENANT, "agent-1")
    assert assigned["agent_id"] == "agent-1"
    # set_agent must not set edited=1: that would stop the portal from ever
    # refreshing this listing's price and description again.
    assert assigned["edited"] is False

    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5", price=95000)])
    after = listings_db.list_for_tenant(TENANT)[0]
    assert after["price"] == 95000       # the scrape still owns portal data...
    assert after["agent_id"] == "agent-1"  # ...but never touches the assignment


def test_assignment_survives_an_agent_edit_and_a_later_scrape():
    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5")])
    listing = listings_db.list_for_tenant(TENANT)[0]
    listings_db.set_agent(listing["id"], TENANT, "agent-1")
    listings_db.update(listing["id"], TENANT, {"price": 123456})

    listings_db.replace_scraped(TENANT, [_scraped("Via Roma 5", price=999)])

    after = listings_db.list_for_tenant(TENANT)[0]
    assert after["price"] == 123456       # agent edit still wins
    assert after["agent_id"] == "agent-1"


def test_set_agent_clears_the_assignment_and_is_tenant_scoped():
    created = listings_db.create_manual(TENANT, {"address": "Via A 1"}, agent_id="agent-1")
    assert created["agent_id"] == "agent-1"

    assert listings_db.set_agent(created["id"], OTHER_TENANT, "agent-9") is None
    assert listings_db.get(created["id"], TENANT)["agent_id"] == "agent-1"

    assert listings_db.set_agent(created["id"], TENANT, None)["agent_id"] is None


def test_unassign_agent_only_frees_that_agents_rows():
    his = listings_db.create_manual(TENANT, {"address": "Via A 1"}, agent_id="agent-1")
    hers = listings_db.create_manual(TENANT, {"address": "Via B 2"}, agent_id="agent-2")
    theirs = listings_db.create_manual(OTHER_TENANT, {"address": "Via C 3"}, agent_id="agent-1")

    assert listings_db.unassign_agent(TENANT, "agent-1") == 1

    assert listings_db.get(his["id"], TENANT)["agent_id"] is None
    assert listings_db.get(hers["id"], TENANT)["agent_id"] == "agent-2"
    # Same agent id under another tenant is a different agent — leave it alone.
    assert listings_db.get(theirs["id"], OTHER_TENANT)["agent_id"] == "agent-1"


def test_count_for_agent_ignores_deleted_listings():
    first = listings_db.create_manual(TENANT, {"address": "Via A 1"}, agent_id="agent-1")
    listings_db.create_manual(TENANT, {"address": "Via B 2"}, agent_id="agent-1")
    assert listings_db.count_for_agent(TENANT, "agent-1") == 2

    listings_db.delete(first["id"], TENANT)
    assert listings_db.count_for_agent(TENANT, "agent-1") == 1


def test_migration_adds_agent_id_to_a_pre_existing_table(in_memory_db, monkeypatch):
    """A listings table created before this column shipped (the deployed Render
    disk) must gain agent_id on startup, not error on every read."""
    conn = in_memory_db
    conn.execute("DROP TABLE listings")
    conn.execute(
        "CREATE TABLE listings (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
        " source TEXT NOT NULL DEFAULT 'scrape', source_key TEXT, "
        " address TEXT NOT NULL DEFAULT '', zone TEXT NOT NULL DEFAULT '', "
        " type TEXT NOT NULL DEFAULT 'vendita', rooms INTEGER DEFAULT 0, "
        " size_sqm INTEGER DEFAULT 0, price INTEGER DEFAULT 0, "
        " currency TEXT NOT NULL DEFAULT 'EUR', available INTEGER NOT NULL DEFAULT 1, "
        " text TEXT NOT NULL DEFAULT '', edited INTEGER NOT NULL DEFAULT 0, "
        " deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT, updated_at TEXT)"
    )
    conn.commit()
    monkeypatch.setattr(listings_db, "_initialized", False)

    listings_db.init()

    created = listings_db.create_manual(TENANT, {"address": "Via A 1"}, agent_id="agent-1")
    assert listings_db.get(created["id"], TENANT)["agent_id"] == "agent-1"

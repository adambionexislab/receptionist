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

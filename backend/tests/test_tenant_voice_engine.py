"""Tests for the tenants.voice_engine column (tenants/db.py).

Every tenant that existed before GPT-Live must keep being answered by the
Realtime model after the migration: the column defaults to 'realtime', and only
a tenant explicitly set to 'live' changes engine (call/router._uses_live_engine).
"""

import sqlite3

import pytest

from tenants import db as tenants_db

# The tenants table as it stood before voice_engine existed.
_LEGACY_SCHEMA = """
CREATE TABLE tenants (
  id TEXT PRIMARY KEY, created_at TEXT, agency_name TEXT NOT NULL,
  agent_name TEXT NOT NULL DEFAULT 'Apollonia', twilio_number TEXT,
  real_number TEXT, immobiliare_url TEXT, lead_email TEXT NOT NULL,
  plan TEXT, billing_period TEXT, management_mode TEXT, active INTEGER DEFAULT 1
)
"""


@pytest.fixture
def conn(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "_conn", c)
    yield c
    c.close()


def test_existing_tenants_stay_on_realtime_after_migration(conn):
    conn.execute(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO tenants (id, agency_name, lead_email) VALUES ('old', 'Studio', 'a@b.it')"
    )
    tenants_db._migrate(conn)

    assert tenants_db.get_by_id("old")["voice_engine"] == "realtime"


def test_migration_is_idempotent(conn):
    conn.execute(_LEGACY_SCHEMA)
    tenants_db._migrate(conn)
    tenants_db._migrate(conn)  # would raise "duplicate column" if not guarded


def test_a_tenant_can_be_created_on_live_and_switched_back(conn):
    conn.execute(tenants_db._SCHEMA)
    tenants_db._migrate(conn)
    tenant = tenants_db.create(
        agency_name="Štúdio Demo Live", lead_email="a@b.sk", locale="sk", voice_engine="live"
    )
    assert tenants_db.get_by_id(tenant["id"])["voice_engine"] == "live"

    tenants_db.update_fields(tenant["id"], voice_engine="realtime")
    assert tenants_db.get_by_id(tenant["id"])["voice_engine"] == "realtime"


def test_update_fields_rejects_unknown_columns_and_the_id(conn):
    conn.execute(tenants_db._SCHEMA)
    tenants_db._migrate(conn)
    with pytest.raises(ValueError):
        tenants_db.update_fields("x", engine="live")
    with pytest.raises(ValueError):
        tenants_db.update_fields("x", id="y")

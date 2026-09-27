"""POST /admin/tenants/{id}/settings — the owner's per-client switches
(main.py). Guarded by ADMIN_TOKEN like the other admin endpoints."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import main
from config import settings
from tenants import db as tenants_db

TOKEN = "admin-test"


@pytest.fixture
def client(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(tenants_db._SCHEMA)
    tenants_db._migrate(conn)
    monkeypatch.setattr(tenants_db, "_conn", conn)
    monkeypatch.setattr(settings, "ADMIN_TOKEN", TOKEN)
    # No lifespan: startup would seed demo tenants and scrape listings.
    yield TestClient(main.app)
    conn.close()


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_collect_email_can_be_switched_on_and_off(client):
    tenant = tenants_db.create(agency_name="Studio", lead_email="a@b.it")
    assert tenant.get("collect_email", 0) == 0

    url = f"/admin/tenants/{tenant['id']}/settings"
    on = client.post(url, json={"collect_email": True}, headers=_auth())
    assert on.status_code == 200
    assert on.json()["collect_email"] == 1

    off = client.post(url, json={"collect_email": False}, headers=_auth())
    assert off.json()["collect_email"] == 0


def test_agency_whatsapp_number_is_stored_normalized(client):
    tenant = tenants_db.create(agency_name="Studio", lead_email="a@b.it")
    url = f"/admin/tenants/{tenant['id']}/settings"

    resp = client.post(url, json={"lead_whatsapp": "0039 333 999 9999"}, headers=_auth())
    assert resp.json()["lead_whatsapp"] == "+393339999999"
    # Setting one field leaves the others alone.
    assert resp.json()["collect_email"] == 0

    bad = client.post(url, json={"lead_whatsapp": "333 9999999"}, headers=_auth())
    assert bad.status_code == 422

    cleared = client.post(url, json={"lead_whatsapp": ""}, headers=_auth())
    assert cleared.json()["lead_whatsapp"] == ""


def test_unknown_tenant_is_a_404(client):
    resp = client.post(
        "/admin/tenants/nope/settings", json={"collect_email": True}, headers=_auth()
    )
    assert resp.status_code == 404


def test_requires_the_admin_token(client):
    tenant = tenants_db.create(agency_name="Studio", lead_email="a@b.it")
    resp = client.post(
        f"/admin/tenants/{tenant['id']}/settings", json={"collect_email": True}
    )
    assert resp.status_code == 401

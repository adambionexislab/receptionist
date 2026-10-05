"""Tests for who may reach the ApollonIA Video tool (videotour/router.py).

Unlike the photo and meeting tools, one video tour costs several euros of
Runway credits. So it is let out one client at a time: VIDEO_TOUR_ENABLED
mounts the router, and the per-tenant `video_tour_enabled` column decides who
is allowed to call it.

The rule these tests pin down is that the per-tenant gate is a real permission
boundary and not just a hidden button. The dashboard's /me payload decides
whether the wizard renders, but a logged-in agency could call the endpoints
directly, so the same check has to run server-side on every route — otherwise
any tenant could spend Runway credits the moment the environment flag went on.
"""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import settings
from dashboard import router as dashboard_router
from tenants import db as tenants_db
from videotour import db as vt_db
from videotour import router as vt_router

OPTED_IN = {
    "id": "tenant-in", "agency_name": "Studio In", "active": 1,
    "locale": "it", "video_tour_enabled": 1,
}
NOT_OPTED_IN = {
    "id": "tenant-out", "agency_name": "Studio Out", "active": 1,
    "locale": "it", "video_tour_enabled": 0,
}


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    monkeypatch.setattr(vt_db, "_initialized", False)
    vt_db.init()
    yield conn
    conn.close()


def client_for(tenant):
    app = FastAPI()
    app.include_router(vt_router.router)
    app.dependency_overrides[dashboard_router.current_tenant] = lambda: tenant
    app.dependency_overrides[dashboard_router.current_branch] = lambda: None
    return TestClient(app)


# ── the per-tenant gate ─────────────────────────────────────────────────────
def test_an_opted_in_tenant_reaches_the_tool():
    resp = client_for(OPTED_IN).get("/video-tour")
    assert resp.status_code == 200
    assert resp.json() == {"jobs": []}


def test_a_tenant_without_the_tool_is_refused():
    resp = client_for(NOT_OPTED_IN).get("/video-tour")
    assert resp.status_code == 403


def test_a_tenant_missing_the_column_entirely_is_refused():
    """A tenant row created before the migration has no flag at all. It must
    read as 'off', never as 'unset, so allow'."""
    legacy = {"id": "tenant-old", "agency_name": "Legacy", "active": 1}
    assert client_for(legacy).get("/video-tour").status_code == 403


def test_every_route_is_gated_not_just_the_listing():
    """The expensive routes are the ones that start generations, so a gap on
    any single one of them would be the whole point of the gate missed."""
    client = client_for(NOT_OPTED_IN)
    attempts = [
        client.post("/video-tour/geocode", json={"address": "Via Roma 1"}),
        client.post("/video-tour", json={"address": "Via Roma 1", "lat": 45.4, "lng": 9.1}),
        client.get("/video-tour/config"),
        client.get("/video-tour/any-job-id"),
        client.get("/video-tour/any-job-id/video"),
        client.post("/video-tour/any-job-id/retry"),
        client.post("/video-tour/any-job-id/abandon"),
    ]
    assert [r.status_code for r in attempts] == [403] * len(attempts)


def test_the_gate_runs_before_any_work_is_done():
    """Refused with no job row written — the check cannot happen after the
    pipeline has already been kicked off."""
    client = client_for(NOT_OPTED_IN)
    resp = client.post(
        "/video-tour", json={"address": "Via Roma 1", "lat": 45.4, "lng": 9.1}
    )
    assert resp.status_code == 403
    assert vt_db.list_for_tenant(NOT_OPTED_IN["id"]) == []


# ── the dashboard payload ───────────────────────────────────────────────────
def test_the_wizard_is_hidden_unless_both_gates_are_open(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_TOUR_ENABLED", True)
    assert dashboard_router._me_payload(OPTED_IN)["features"]["video_tour"] is True
    assert dashboard_router._me_payload(NOT_OPTED_IN)["features"]["video_tour"] is False


def test_the_environment_flag_alone_grants_nothing(monkeypatch):
    """Turning VIDEO_TOUR_ENABLED on in production must not hand the tool to
    every existing tenant at once."""
    monkeypatch.setattr(settings, "VIDEO_TOUR_ENABLED", True)
    assert dashboard_router._me_payload(NOT_OPTED_IN)["features"]["video_tour"] is False


def test_the_per_tenant_flag_alone_grants_nothing(monkeypatch):
    """And a tenant opted in while the feature is off globally sees nothing,
    so the column can be set ahead of the deploy that enables it."""
    monkeypatch.setattr(settings, "VIDEO_TOUR_ENABLED", False)
    assert dashboard_router._me_payload(OPTED_IN)["features"]["video_tour"] is False


# ── the column itself ───────────────────────────────────────────────────────
def test_the_flag_defaults_to_off_for_a_new_tenant():
    tenant = tenants_db.create(agency_name="Fresh", lead_email="f@example.com")
    assert tenants_db.get_by_id(tenant["id"])["video_tour_enabled"] == 0


def test_the_owner_can_flip_the_flag_per_tenant():
    tenant = tenants_db.create(agency_name="Fresh", lead_email="f@example.com")
    tenants_db.update_fields(tenant["id"], video_tour_enabled=1)
    assert tenants_db.get_by_id(tenant["id"])["video_tour_enabled"] == 1
    tenants_db.update_fields(tenant["id"], video_tour_enabled=0)
    assert tenants_db.get_by_id(tenant["id"])["video_tour_enabled"] == 0

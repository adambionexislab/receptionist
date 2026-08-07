"""Tests for surviving a meeting that ends without anyone pressing Termina
riunione (acquisizione/router.py, acquisizione/db.py).

A seller meeting runs on a phone in someone else's flat: the browser gets
closed, the tab gets killed under memory pressure, the battery dies. The
transcript is autosaved throughout, so the words are on disk — the risk is
that nothing ever offers them back, leaving the record stranded in
'transcribing' forever and the meeting lost from the agent's point of view.

Deliberate exits are the mirror image: the agent who backs out of a meeting
on purpose must not be nagged to resume it.
"""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from acquisizione import db as acq_db
from acquisizione import router as acq_router
from dashboard.router import current_tenant
from tenants import db as tenants_db

TENANT = {"id": "tenant-a", "agency_name": "Studio A", "active": 1, "locale": "it"}
OTHER_TENANT = "tenant-b"


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    monkeypatch.setattr(acq_db, "_initialized", False)
    acq_db.init()
    yield conn
    conn.close()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(acq_router.router)
    app.dependency_overrides[current_tenant] = lambda: TENANT
    return TestClient(app)


def _stranded(tenant_id=TENANT["id"], transcript="Il venditore chiede 250 mila."):
    """A meeting still in 'transcribing' with words already captured — what a
    closed browser leaves behind."""
    record = acq_db.create(tenant_id, "it", "checkbox")
    acq_db.update_transcript(record["id"], tenant_id, transcript)
    return record


def test_a_meeting_the_browser_left_behind_is_offered_back(client):
    record = _stranded()

    resp = client.get("/acquisizione/resumable")

    assert resp.status_code == 200
    offered = resp.json()["record"]
    assert offered["id"] == record["id"]
    # The transcript comes back with it: the browser lost its buffer when the
    # page died, so this is the only copy left to restore.
    assert offered["transcript"] == "Il venditore chiede 250 mila."


def test_nothing_to_resume_is_an_answer_not_an_error(client):
    # Also pins the route order: /resumable must not be read as a record id,
    # which would 404 out of get_record instead of answering.
    resp = client.get("/acquisizione/resumable")
    assert resp.status_code == 200
    assert resp.json() == {"record": None}


def test_a_deliberately_abandoned_meeting_is_never_offered_back(client):
    record = _stranded()

    assert client.post(f"/acquisizione/{record['id']}/abandon").json() == {"ok": True}

    assert client.get("/acquisizione/resumable").json()["record"] is None
    # Abandoned means "don't offer it back", not "delete it" — the transcript
    # is still there for the agency.
    kept = acq_db.get(record["id"], TENANT["id"])
    assert kept["status"] == "abandoned"
    assert kept["transcript"] == "Il venditore chiede 250 mila."


def test_a_meeting_with_no_words_is_not_worth_resuming(client):
    acq_db.create(TENANT["id"], "it", "checkbox")
    assert client.get("/acquisizione/resumable").json()["record"] is None


def test_a_meeting_already_being_processed_is_not_offered(client):
    record = _stranded()
    acq_db.set_processing(record["id"], TENANT["id"])

    assert client.get("/acquisizione/resumable").json()["record"] is None


def test_a_stale_meeting_is_not_offered(in_memory_db, client):
    record = _stranded()
    in_memory_db.execute(
        "UPDATE intake_records SET updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (record["id"],),
    )
    in_memory_db.commit()

    assert client.get("/acquisizione/resumable").json()["record"] is None


def test_the_newest_stranded_meeting_wins(in_memory_db, client):
    older = _stranded(transcript="prima riunione")
    in_memory_db.execute(
        "UPDATE intake_records SET updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (older["id"],),
    )
    in_memory_db.commit()
    newer = _stranded(transcript="seconda riunione")

    assert client.get("/acquisizione/resumable").json()["record"]["id"] == newer["id"]


def test_another_tenants_stranded_meeting_is_invisible(client):
    _stranded(tenant_id=OTHER_TENANT)

    assert client.get("/acquisizione/resumable").json()["record"] is None


def test_abandon_leaves_a_meeting_past_the_live_stage_alone(client):
    record = _stranded()
    acq_db.set_review_result(record["id"], TENANT["id"], {}, [], "testo", "note")

    assert client.post(f"/acquisizione/{record['id']}/abandon").json() == {"ok": False}
    assert acq_db.get(record["id"], TENANT["id"])["status"] == "review"


def test_abandoning_another_tenants_meeting_does_nothing(client):
    record = _stranded(tenant_id=OTHER_TENANT)

    assert client.post(f"/acquisizione/{record['id']}/abandon").json() == {"ok": False}
    assert acq_db.get(record["id"], OTHER_TENANT)["status"] == "transcribing"


def test_the_last_gasp_beacon_can_post_the_transcript(client):
    """The flush as the tab closes goes out via navigator.sendBeacon, which can
    only POST — the same save the periodic autosave does with PATCH."""
    record = acq_db.create(TENANT["id"], "it", "checkbox")

    resp = client.post(
        f"/acquisizione/{record['id']}/transcript",
        json={"transcript": "le ultime parole della riunione"},
    )

    assert resp.status_code == 200
    assert acq_db.get(record["id"], TENANT["id"])["transcript"] == "le ultime parole della riunione"

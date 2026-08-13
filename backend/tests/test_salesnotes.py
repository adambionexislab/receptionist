"""Tests for the sales rep's meeting notes (salesnotes/*).

A note is one spoken debrief: she talks, it's transcribed, one extraction call
turns it into structured fields, and she edits and saves it. What has to hold:
nothing she said is ever thrown away by a failure, a recording the browser lost
is offered back, and the note stays editable afterwards — the detail that
matters often surfaces later.
"""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from leadgen.router import require_staff
from salesnotes import db as notes_db
from salesnotes import extraction
from salesnotes import router as notes_router
from salesnotes import schema
from tenants import db as tenants_db


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    monkeypatch.setattr(notes_db, "_initialized", False)
    notes_db.init()
    yield conn
    conn.close()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(notes_router.router)
    # Logged in: the staff login itself is covered in test_leadgen_auth.py.
    app.dependency_overrides[require_staff] = lambda: None
    return TestClient(app)


EXTRACTED = {
    "title": "Demo con Studio Rossi",
    "customer": "Studio Rossi",
    "outcome": "negative",
    "summary": "Demo di 40 minuti. Interessati ma bloccati dal prezzo.",
    "went_well": ["La demo dal vivo ha convinto"],
    "went_wrong": ["Sono arrivata tardi"],
    "objections": ["Costa troppo rispetto al gestionale che hanno"],
    "next_steps": ["Mandare il preventivo annuale"],
}


@pytest.fixture
def fake_extraction(monkeypatch):
    """Stand in for the OpenAI call, returning already-normalized fields."""
    calls = []

    async def _extract(transcript, language):
        calls.append((transcript, language))
        return dict(EXTRACTED)

    monkeypatch.setattr(extraction, "extract", _extract)
    return calls


def _recorded(client, transcript="La riunione è andata male sul prezzo.", language="it"):
    """A note with words captured, ready to be finished."""
    note = client.post("/notes", json={"language": language}).json()
    client.patch(f"/notes/{note['id']}/transcript", json={"transcript": transcript})
    return note


# ── the happy path ───────────────────────────────────────────────────────────
def test_a_debrief_becomes_a_structured_note(client, fake_extraction):
    note = _recorded(client)

    resp = client.post(f"/notes/{note['id']}/finish")

    assert resp.status_code == 200
    finished = resp.json()
    assert finished["status"] == "review"
    assert finished["title"] == "Demo con Studio Rossi"
    assert finished["outcome"] == "negative"
    assert finished["objections"] == ["Costa troppo rispetto al gestionale che hanno"]
    # The transcript is what was extracted, in the language it was recorded in.
    assert fake_extraction == [("La riunione è andata male sul prezzo.", "it")]


def test_her_edits_are_what_gets_saved(client, fake_extraction):
    note = _recorded(client)
    client.post(f"/notes/{note['id']}/finish")

    resp = client.patch(
        f"/notes/{note['id']}",
        json={
            "title": "Demo Studio Rossi (2° incontro)",
            "customer": "Studio Rossi",
            "outcome": "neutral",
            "summary": "Riscritto a mano.",
            "went_well": ["La demo dal vivo ha convinto"],
            "went_wrong": [],
            "objections": ["Prezzo", "Vogliono parlarne col socio"],
            "next_steps": ["Preventivo annuale entro venerdì"],
        },
    )

    assert resp.status_code == 200
    saved = resp.json()
    assert saved["status"] == "saved"
    assert saved["saved_at"]
    assert saved["title"] == "Demo Studio Rossi (2° incontro)"
    assert saved["objections"] == ["Prezzo", "Vogliono parlarne col socio"]
    assert saved["went_wrong"] == []


def test_a_saved_note_can_still_be_corrected(client, fake_extraction):
    """The detail that matters often surfaces on the drive back."""
    note = _recorded(client)
    client.post(f"/notes/{note['id']}/finish")
    client.patch(f"/notes/{note['id']}", json={"title": "Prima versione"})

    resp = client.patch(
        f"/notes/{note['id']}",
        json={"title": "Prima versione", "objections": ["Anche i tempi di attivazione"]},
    )

    assert resp.status_code == 200
    assert resp.json()["objections"] == ["Anche i tempi di attivazione"]


def test_notes_are_listed_newest_first(client, fake_extraction):
    first = _recorded(client, transcript="prima riunione")
    second = _recorded(client, transcript="seconda riunione")

    listed = client.get("/notes").json()["notes"]

    assert [n["id"] for n in listed][:2] == [second["id"], first["id"]]
    # The list fields are always present, even on a note that never reached
    # extraction — the dashboard renders them without guarding.
    assert listed[0]["objections"] == []


# ── nothing she said is thrown away ──────────────────────────────────────────
def test_a_failed_extraction_keeps_the_transcript_and_allows_a_retry(client, monkeypatch):
    note = _recorded(client)

    async def _boom(transcript, language):
        raise extraction.ExtractionError("model said no")

    monkeypatch.setattr(extraction, "extract", _boom)

    assert client.post(f"/notes/{note['id']}/finish").status_code == 502

    kept = notes_db.get(note["id"])
    assert kept["transcript"] == "La riunione è andata male sul prezzo."
    # Back to 'recording', which is what makes the retry button work.
    assert kept["status"] == "recording"


def test_a_silent_recording_is_refused_before_it_costs_anything(client, fake_extraction):
    note = client.post("/notes", json={"language": "it"}).json()

    resp = client.post(f"/notes/{note['id']}/finish")

    assert resp.status_code == 400
    assert fake_extraction == []


def test_the_last_gasp_beacon_can_post_the_transcript(client):
    """The flush as the tab closes goes out via navigator.sendBeacon, which can
    only POST — the same save the periodic autosave does with PATCH."""
    note = client.post("/notes", json={"language": "it"}).json()

    resp = client.post(
        f"/notes/{note['id']}/transcript", json={"transcript": "le ultime parole"}
    )

    assert resp.status_code == 200
    assert notes_db.get(note["id"])["transcript"] == "le ultime parole"


def test_a_finished_note_stops_accepting_audio(client, fake_extraction):
    note = _recorded(client)
    client.post(f"/notes/{note['id']}/finish")

    assert client.patch(
        f"/notes/{note['id']}/transcript", json={"transcript": "troppo tardi"}
    ).status_code == 404
    assert notes_db.get(note["id"])["transcript"] == "La riunione è andata male sul prezzo."


# ── a recording the browser lost ─────────────────────────────────────────────
def test_a_recording_the_browser_left_behind_is_offered_back(client):
    note = _recorded(client)

    offered = client.get("/notes/resumable").json()["note"]

    assert offered["id"] == note["id"]
    # The transcript comes back with it: the browser lost its buffer when the
    # page died, so this is the only copy left.
    assert offered["transcript"] == "La riunione è andata male sul prezzo."


def test_nothing_to_resume_is_an_answer_not_an_error(client):
    # Also pins the route order: /resumable must not be read as a note id.
    resp = client.get("/notes/resumable")
    assert resp.status_code == 200
    assert resp.json() == {"note": None}


def test_a_deliberately_abandoned_recording_is_never_offered_back(client):
    note = _recorded(client)

    assert client.post(f"/notes/{note['id']}/abandon").json() == {"ok": True}

    assert client.get("/notes/resumable").json()["note"] is None
    # Abandoned means "stop offering it", not "delete it".
    kept = notes_db.get(note["id"])
    assert kept["status"] == "abandoned"
    assert kept["transcript"] == "La riunione è andata male sul prezzo."
    # ...but it stays out of the list, which is for notes she still wants.
    assert client.get("/notes").json()["notes"] == []


def test_a_recording_with_no_words_is_not_worth_resuming(client):
    client.post("/notes", json={"language": "it"})
    assert client.get("/notes/resumable").json()["note"] is None


def test_a_stale_recording_is_not_offered(in_memory_db, client):
    note = _recorded(client)
    in_memory_db.execute(
        "UPDATE sales_notes SET updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (note["id"],),
    )
    in_memory_db.commit()

    assert client.get("/notes/resumable").json()["note"] is None


def test_a_mistaken_recording_can_be_deleted_outright(client):
    note = _recorded(client)

    assert client.delete(f"/notes/{note['id']}").status_code == 200

    assert notes_db.get(note["id"]) is None
    assert client.delete(f"/notes/{note['id']}").status_code == 404


# ── shape ────────────────────────────────────────────────────────────────────
def test_an_unknown_language_falls_back_rather_than_failing(client):
    note = client.post("/notes", json={"language": "klingon"}).json()
    assert note["language"] == "it"


def test_a_made_up_outcome_never_reaches_storage(client, fake_extraction):
    """The dashboard filters and counts on `outcome`, so it is either one of
    the three tokens or nothing at all."""
    note = _recorded(client)
    client.post(f"/notes/{note['id']}/finish")

    saved = client.patch(f"/notes/{note['id']}", json={"outcome": "fantastico"}).json()

    assert saved["outcome"] is None


def test_blank_list_entries_are_dropped(client, fake_extraction):
    note = _recorded(client)
    client.post(f"/notes/{note['id']}/finish")

    saved = client.patch(
        f"/notes/{note['id']}",
        json={"objections": ["  Prezzo  ", "", "   ", "Tempi"]},
    ).json()

    assert saved["objections"] == ["Prezzo", "Tempi"]


def test_the_extraction_schema_is_strict_mode_compatible():
    """Structured Outputs requires every property listed in `required` and
    additionalProperties: false — Pydantic's own schema wouldn't satisfy it."""
    envelope = schema.envelope_schema()

    assert envelope["additionalProperties"] is False
    assert set(envelope["required"]) == set(envelope["properties"])
    assert set(envelope["properties"]) == set(schema.TEXT_FIELDS) | set(schema.LIST_FIELDS)
    assert None in envelope["properties"]["outcome"]["enum"]

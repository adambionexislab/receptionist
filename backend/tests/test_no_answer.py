"""Tests for the personal-numbers list (noanswer/, dashboard/router.py,
call/router.py).

A tenant's phone forwards every unanswered call to Apollonia, so family and
friends reach her too. A call from a number on the list is declined before
she picks up: the caller hears a busy tone, and no session is ever opened.
"""

import asyncio
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from call import live, router
from dashboard import router as dashboard_router
from noanswer import contacts_file
from noanswer import db as noanswer_db
from tenants import db as tenants_db

TENANT = {"id": "tenant-a", "agency_name": "Studio A", "active": 1}
OTHER = "tenant-b"


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    monkeypatch.setattr(noanswer_db, "_initialized", False)
    noanswer_db.init()
    yield conn
    conn.close()


# ── numbers ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+39 333 123 4567", "+393331234567"),
        ("0039 333-123-4567", "+393331234567"),
        ("333 1234567", "3331234567"),  # national form kept as typed
        ("02 1234 5678", "0212345678"),
        ("12345", None),
        ("", None),
        ("mamma", None),
    ],
)
def test_normalize(raw, expected):
    assert noanswer_db.normalize(raw) == expected


def test_a_contact_saved_without_country_code_matches_the_caller_id():
    noanswer_db.add(TENANT["id"], "3331234567", "Mamma")
    assert noanswer_db.find(TENANT["id"], "+393331234567")["name"] == "Mamma"


def test_the_same_line_cannot_be_listed_twice_under_two_spellings():
    assert noanswer_db.add(TENANT["id"], "+393331234567", "Mamma")
    assert noanswer_db.add(TENANT["id"], "3331234567", "Mom") is None
    assert len(noanswer_db.list_for_tenant(TENANT["id"])) == 1


def test_lists_are_per_tenant():
    noanswer_db.add(OTHER, "+393331234567", "Not yours")
    assert noanswer_db.find(TENANT["id"], "+393331234567") is None
    assert noanswer_db.list_for_tenant(TENANT["id"]) == []


def test_a_short_caller_never_matches_by_accident():
    noanswer_db.add(TENANT["id"], "+393331234567")
    assert noanswer_db.find(TENANT["id"], "") is None
    assert noanswer_db.find(TENANT["id"], "4567") is None


# ── contacts files ───────────────────────────────────────────────────────────
IPHONE_VCF = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Rossi;Maria;;;\r\nFN:Maria Rossi\r\n"
    "item1.TEL;type=CELL;type=pref:+39 333 123 4567\r\n"
    "TEL;type=HOME:02 1234 5678\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Bianchi;Luca;;;\r\n"
    "FN:Luca Bia\r\n nchi\r\nTEL;type=CELL:+39 347 000 1111\r\nEND:VCARD\r\n"
)


def test_iphone_vcard():
    assert contacts_file.parse(IPHONE_VCF) == [
        {"name": "Maria Rossi", "number": "+393331234567"},
        {"name": "Maria Rossi", "number": "0212345678"},
        {"name": "Luca Bianchi", "number": "+393470001111"},
    ]


def test_android_quoted_printable_name_and_vcard4_uri():
    vcf = (
        "BEGIN:VCARD\nVERSION:2.1\n"
        "FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=C5=A0tef=C3=A1n\n"
        "TEL;CELL:+421 903 123 456\nEND:VCARD\n"
        "BEGIN:VCARD\nVERSION:4.0\nN:Kováč;Ján;;;\n"
        "TEL;VALUE=uri;TYPE=cell:tel:+421-905-111-222\nEND:VCARD\n"
    )
    assert contacts_file.parse(vcf) == [
        {"name": "Štefán", "number": "+421903123456"},
        {"name": "Ján Kováč", "number": "+421905111222"},
    ]


def test_google_csv_with_packed_numbers():
    csv_text = (
        "First Name,Middle Name,Last Name,Phone 1 - Label,Phone 1 - Value,Phone 2 - Label,Phone 2 - Value\n"
        "Maria,,Rossi,Mobile,+39 333 123 4567 ::: +39 02 1234 5678,,\n"
        "Luca,,,Home,,Work,+39 347 000 1111\n"
        "Nobody,,,,,,\n"
    )
    assert contacts_file.parse(csv_text) == [
        {"name": "Maria Rossi", "number": "+393331234567"},
        {"name": "Maria Rossi", "number": "+390212345678"},
        {"name": "Luca", "number": "+393470001111"},
    ]


def test_outlook_csv_semicolon():
    csv_text = (
        "﻿First Name;Last Name;Mobile Phone;Home Phone\n"
        "Anna;Verdi;333 222 1111;\n"
    )
    assert contacts_file.parse(csv_text) == [
        {"name": "Anna Verdi", "number": "3332221111"},
    ]


def test_duplicates_and_junk_are_dropped():
    vcf = (
        "BEGIN:VCARD\nFN:A\nTEL:+39 333 123 4567\nTEL:3331234567\nTEL:112\nEND:VCARD\n"
        "garbage line\n"
    )
    assert contacts_file.parse(vcf) == [{"name": "A", "number": "+393331234567"}]


# ── dashboard ────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(dashboard_router.router)
    app.dependency_overrides[dashboard_router.current_tenant] = lambda: TENANT
    return TestClient(app)


def test_add_list_delete(client):
    resp = client.post("/dashboard/api/no-answer", json={"number": "+39 333 123 4567", "name": " Mamma "})
    assert resp.status_code == 201
    entry = resp.json()
    assert entry["number"] == "+393331234567" and entry["name"] == "Mamma"

    assert client.post("/dashboard/api/no-answer", json={"number": "3331234567"}).status_code == 409
    assert client.post("/dashboard/api/no-answer", json={"number": "abc"}).status_code == 422

    assert [n["id"] for n in client.get("/dashboard/api/no-answer").json()["numbers"]] == [entry["id"]]
    assert client.delete(f"/dashboard/api/no-answer/{entry['id']}").status_code == 200
    assert client.get("/dashboard/api/no-answer").json()["numbers"] == []


def test_another_agencys_entry_cannot_be_deleted(client):
    theirs = noanswer_db.add(OTHER, "+393331234567", "X")
    assert client.delete(f"/dashboard/api/no-answer/{theirs['id']}").status_code == 404
    assert noanswer_db.find(OTHER, "+393331234567")


def test_parse_previews_without_saving_and_marks_listed(client):
    noanswer_db.add(TENANT["id"], "+393331234567", "Maria")
    resp = client.post("/dashboard/api/no-answer/parse", json={"text": IPHONE_VCF})
    contacts = resp.json()["contacts"]
    assert [c["listed"] for c in contacts] == [True, False, False]
    assert len(noanswer_db.list_for_tenant(TENANT["id"])) == 1


def test_bulk_add_skips_listed_and_invalid(client):
    noanswer_db.add(TENANT["id"], "+393331234567", "Maria")
    resp = client.post("/dashboard/api/no-answer/bulk", json={"entries": [
        {"number": "3331234567", "name": "Maria"},
        {"number": "+39 347 000 1111", "name": "Luca"},
        {"number": "nope"},
    ]})
    assert resp.json() == {"added": 1, "skipped": 2}
    assert noanswer_db.find(TENANT["id"], "+393470001111")["name"] == "Luca"


# ── the call ─────────────────────────────────────────────────────────────────
PHONE_TENANT = {
    "id": TENANT["id"], "agency_name": "Studio A", "agent_name": "Apollonia",
    "locale": "it", "real_number": "+390311234567", "lead_email": "a@studio.it",
    "voice_engine": "realtime",
}


@pytest.fixture
def phone(monkeypatch):
    monkeypatch.setattr(router, "_find_tenant_by_dialed", lambda n: dict(PHONE_TENANT) if n else None)
    monkeypatch.setattr(router.tenant_stores, "get_or_create", lambda tid: object())
    monkeypatch.setattr(router.branches_db, "list_for_tenant", lambda tid: [])


def _headers(caller):
    return [
        {"name": "From", "value": f"<sip:{caller}@carrier>"},
        {"name": "Diversion", "value": "<sip:+43720000000@carrier>"},
    ]


def test_a_listed_caller_is_recognised(phone):
    noanswer_db.add(TENANT["id"], "3331234567", "Mamma")
    ctx = router._resolve_call("c1", _headers("+393331234567"))
    assert ctx.no_answer


def test_an_unlisted_caller_gets_the_receptionist(phone):
    ctx = router._resolve_call("c1", _headers("+393470001111"))
    assert not ctx.no_answer


def test_a_clobbered_caller_id_never_matches(phone):
    # The carrier replaced the caller with the owner's own line: even if the
    # owner listed their own number, that is not who is calling.
    noanswer_db.add(TENANT["id"], PHONE_TENANT["real_number"], "Me")
    ctx = router._resolve_call("c1", _headers(PHONE_TENANT["real_number"]))
    assert not ctx.no_answer


def test_a_broken_list_never_fails_the_call(phone, monkeypatch):
    def boom(*a):
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(router.noanswer_db, "find", boom)
    assert not router._resolve_call("c1", _headers("+393331234567")).no_answer




def _ctx(engine="realtime", no_answer=True):
    tenant = {**PHONE_TENANT, "voice_engine": engine}
    return router._CallContext(
        tenant=tenant, locale="it", content=router._content("it"), tenant_store=object(),
        lead_email="a@studio.it", branch_names=[], caller="+393331234567",
        caller_number_known=True, no_answer=no_answer,
    )


@pytest.fixture
def no_side_effects(monkeypatch):
    """Record what each handler would do instead of calling OpenAI."""
    calls = {"reject": [], "realtime_tasks": [], "live_tasks": []}

    async def fake_reject(call_id, status_code=603):
        calls["reject"].append((call_id, status_code))

    async def fake_run_call(call_id, *args, **kwargs):
        calls["realtime_tasks"].append(call_id)

    async def fake_run_live_call(session_id, *args, **kwargs):
        calls["live_tasks"].append(session_id)

    monkeypatch.setattr(router, "_reject_call", fake_reject)
    monkeypatch.setattr(router, "_run_call", fake_run_call)
    monkeypatch.setattr(live, "run_live_call", fake_run_live_call)
    router._claimed_calls.clear()
    yield calls
    router._claimed_calls.clear()


async def _both_webhooks(ctx, monkeypatch, call_id="c1"):
    """Both OpenAI webhooks fire for every call; exactly one may decide."""
    monkeypatch.setattr(router, "_resolve_call", lambda cid, headers: ctx)
    await router._handle_realtime_incoming({"call_id": call_id, "sip_headers": []})
    await live.handle_incoming({"type": "sip", "session_id": call_id, "sip_headers": []})
    await asyncio.sleep(0)


@pytest.mark.parametrize("engine", ["realtime", "live"])
def test_a_listed_caller_is_declined_busy_and_never_answered(engine, no_side_effects, monkeypatch):
    asyncio.run(_both_webhooks(_ctx(engine=engine), monkeypatch))
    assert no_side_effects == {"reject": [("c1", 486)], "realtime_tasks": [], "live_tasks": []}


def test_a_redelivered_webhook_declines_once(no_side_effects, monkeypatch):
    monkeypatch.setattr(router, "_resolve_call", lambda cid, headers: _ctx())

    async def twice():
        for _ in range(2):
            await router._handle_realtime_incoming({"call_id": "c1", "sip_headers": []})

    asyncio.run(twice())
    assert no_side_effects["reject"] == [("c1", 486)]


def test_an_unlisted_caller_is_still_answered(no_side_effects, monkeypatch):
    asyncio.run(_both_webhooks(_ctx(no_answer=False), monkeypatch))
    assert no_side_effects == {"reject": [], "realtime_tasks": ["c1"], "live_tasks": []}


def test_a_whole_phone_book_is_added_in_one_go(client):
    """A 6,000-contact import used to hit the per-request cap; the page now
    sends batches, and each batch is one transaction."""
    noanswer_db.add(TENANT["id"], "+393330000000", "Already")
    entries = [{"number": f"+39333{i:07d}", "name": f"C{i}"} for i in range(1000)]
    entries.append({"number": "333 000 0001", "name": "Same line as C1"})
    entries.append({"number": "junk"})

    resp = client.post("/dashboard/api/no-answer/bulk", json={"entries": entries})

    # C0 is "Already"'s line; the repeat of C1 and the junk entry are skipped.
    assert resp.json() == {"added": 999, "skipped": 3}
    assert len(noanswer_db.list_for_tenant(TENANT["id"])) == 1000
    assert noanswer_db.find(TENANT["id"], "+393330000001")["name"] == "C1"

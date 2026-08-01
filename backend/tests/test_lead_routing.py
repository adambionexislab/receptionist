"""Tests for who the post-call lead email is addressed to (call/router.py).

The rule: a lead about a property goes to the agent who handles that property,
with the agency inbox in Cc. Everything else — a message (TYPE C), a seller
call (TYPE D), an unassigned listing, an agent with no email — falls back to
the agency inbox alone. The fallbacks matter more than the routing: a gap in
them would silently drop a lead the agency never learns about.
"""

import sqlite3

import pytest

from agents import db as agents_db
from call import router
from listings import db as listings_db
from tenants import db as tenants_db

TENANT = "tenant-a"
INBOX = "agenzia@studio.it"


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    monkeypatch.setattr(agents_db, "_initialized", False)
    monkeypatch.setattr(listings_db, "_initialized", False)
    agents_db.init()
    listings_db.init()
    yield conn
    conn.close()


def _session(interested, tenant_id=TENANT, lead_email=INBOX):
    return {
        "tenant_id": tenant_id,
        "lead_email": lead_email,
        "locale": "it",
        "interested_listings": interested,
        "listings_shown": list(interested),
    }


def _resolve(session):
    agents = router._interest_agents(session)
    to, cc, routed = router._resolve_lead_recipients(session, agents)
    return agents, to, cc, routed


def test_lead_goes_to_the_agent_who_handles_the_listing():
    mario = agents_db.create(TENANT, "Mario Rossi", "mario@studio.it")
    listing = listings_db.create_manual(
        TENANT, {"address": "Via Roma 1"}, agent_id=mario["id"]
    )

    agents, to, cc, routed = _resolve(_session([listing]))

    assert routed is True
    assert to == ["mario@studio.it"]
    # The agency keeps a complete record of every lead.
    assert cc == [INBOX]
    assert [a["id"] for a in agents] == [mario["id"]]


def test_one_call_spanning_two_agents_reaches_both_in_a_single_email():
    mario = agents_db.create(TENANT, "Mario Rossi", "mario@studio.it")
    lucia = agents_db.create(TENANT, "Lucia Bianchi", "lucia@studio.it")
    first = listings_db.create_manual(TENANT, {"address": "Via Roma 1"}, agent_id=mario["id"])
    second = listings_db.create_manual(TENANT, {"address": "Via Po 2"}, agent_id=lucia["id"])

    _agents, to, cc, routed = _resolve(_session([first, second]))

    assert routed is True
    assert to == ["mario@studio.it", "lucia@studio.it"]  # interest order
    assert cc == [INBOX]


def test_two_listings_of_the_same_agent_are_not_addressed_twice():
    mario = agents_db.create(TENANT, "Mario Rossi", "mario@studio.it")
    first = listings_db.create_manual(TENANT, {"address": "Via Roma 1"}, agent_id=mario["id"])
    second = listings_db.create_manual(TENANT, {"address": "Via Po 2"}, agent_id=mario["id"])

    _agents, to, _cc, _routed = _resolve(_session([first, second]))

    assert to == ["mario@studio.it"]


def test_unassigned_listing_falls_back_to_the_agency_inbox():
    listing = listings_db.create_manual(TENANT, {"address": "Via Roma 1"})

    agents, to, cc, routed = _resolve(_session([listing]))

    assert routed is False
    assert to == [INBOX]
    assert cc == []
    assert agents == []


def test_no_interest_marked_goes_to_the_agency_inbox():
    """TYPE C (message) and TYPE D (seller) calls touch no listing at all, as
    does a caller who was shown properties but picked none."""
    _agents, to, cc, routed = _resolve(_session([]))

    assert (to, cc, routed) == ([INBOX], [], False)


def test_agent_without_an_email_falls_back_without_losing_the_lead():
    mario = agents_db.create(TENANT, "Mario Rossi", "mario@studio.it")
    agents_db.update(mario["id"], TENANT, {"name": "Mario Rossi"})
    # Blank the address the way a pre-validation row could have it.
    in_memory = tenants_db.get_connection()
    in_memory.execute("UPDATE agency_agents SET email = '' WHERE id = ?", (mario["id"],))
    in_memory.commit()
    listing = listings_db.create_manual(
        TENANT, {"address": "Via Roma 1"}, agent_id=mario["id"]
    )

    agents, to, cc, routed = _resolve(_session([listing]))

    assert routed is False
    assert to == [INBOX]
    # The agent is still known, so the email can say why it came to the agency.
    assert [a["id"] for a in agents] == [mario["id"]]


def test_an_agent_deleted_mid_call_falls_back_to_the_agency_inbox():
    mario = agents_db.create(TENANT, "Mario Rossi", "mario@studio.it")
    listing = listings_db.create_manual(
        TENANT, {"address": "Via Roma 1"}, agent_id=mario["id"]
    )
    session = _session([listing])  # snapshot taken during the call
    agents_db.delete(mario["id"], TENANT)

    _agents, to, cc, routed = _resolve(session)

    assert (to, cc, routed) == ([INBOX], [], False)


def test_no_cc_when_the_agent_is_the_agency_inbox():
    mario = agents_db.create(TENANT, "Mario Rossi", INBOX.upper())
    listing = listings_db.create_manual(
        TENANT, {"address": "Via Roma 1"}, agent_id=mario["id"]
    )

    _agents, to, cc, _routed = _resolve(_session([listing]))

    assert to == [INBOX.upper()]
    assert cc == []  # would otherwise send the agency the same mail twice


def test_demo_fallback_without_a_tenant_never_routes():
    """The env-var demo path has no tenant row, so there are no agents to look
    up — and no tenant_id to scope a lookup by."""
    listing = {"address": "Via Roma 1", "agent_id": "agent-1"}

    _agents, to, cc, routed = _resolve(_session([listing], tenant_id=None))

    assert (to, cc, routed) == ([INBOX], [], False)


def test_agent_line_states_where_the_lead_went():
    content = router._content("it")
    mario = {"id": "a1", "number": 2, "name": "Mario Rossi", "email": "m@studio.it"}
    lucia = {"id": "a2", "number": 3, "name": "Lucia Bianchi", "email": "l@studio.it"}

    assert router._format_agent_line(content, [mario], True) == "Agente: #2 Mario Rossi"
    assert router._format_agent_line(content, [mario, lucia], True) == (
        "Agenti: #2 Mario Rossi, #3 Lucia Bianchi"
    )
    # Unassigned and email-less both explain why the agency got it instead.
    assert router._format_agent_line(content, [], False) == (
        "Agente: nessun agente assegnato all'immobile — inoltrato all'agenzia"
    )
    assert router._format_agent_line(content, [mario], False) == (
        "Agente: #2 Mario Rossi (nessun indirizzo email) — inoltrato all'agenzia"
    )


def test_every_locale_has_the_agent_email_strings():
    """The lead email is built from the tenant's locale dict; a missing key
    would raise mid-send and lose the lead."""
    keys = (
        "email_agent_label", "email_agent_label_plural", "email_agent_unassigned",
        "email_agent_no_email", "email_agent_to_agency",
    )
    for locale in ("it", "sk"):
        content = router._content(locale)
        assert all(content.get(k) for k in keys), locale


def test_tool_results_hide_bookkeeping_fields_from_the_phone_agent():
    """Apollonia must not see (and read out) row ids or agent assignments."""
    row = {
        "id": "row-1", "agent_id": "agent-1", "source": "scrape", "edited": True,
        "address": "Via Roma 1", "zone": "lodi", "type": "vendita", "rooms": 3,
        "size_sqm": 80, "price": 100000, "currency": "EUR", "available": True,
        "text": "descrizione",
    }
    (projected,) = router._for_model([row])

    assert set(projected) == {
        "address", "zone", "type", "rooms", "size_sqm", "price", "currency",
        "available", "text",
    }

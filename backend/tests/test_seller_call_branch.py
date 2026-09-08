"""Tests for routing a seller call to an office (call/router.py).

A seller call touches no listing, so before this it had no office and every one
of these leads landed in the shared agency inbox. What it does have is where
the caller said their property is — something the TYPE D script already asked
for and then dissolved into the free-text note.

Two rules are load-bearing here:
  - the office names live in the tool schema and NOWHERE in the prompt, because
    prompt text gets spoken aloud and tool schemas do not (see
    test_question_style.py for what it cost to learn that);
  - nothing on this path may raise. A caller is on the line, and a lead in the
    agency inbox beats a broken call.
"""

import sqlite3

import pytest

from agents import db as agents_db
from branches import db as branches_db
from branches import routing
from call import router
from listings import db as listings_db
from tenants import db as tenants_db

TENANT = "tenant-a"
INBOX = "agenzia@studio.it"
LOCALES = ("it", "sk")

# Deliberately unpronounceable rather than "Milano"/"Roma": the prompts are
# full of real place names as example addresses ("in Via Roma 5"), so a
# realistic office name would make the never-in-the-prompt assertion pass or
# fail for reasons that have nothing to do with the injection.
OFFICES = ["Zzyzx", "Qwertz"]


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(tenants_db, "get_connection", lambda: conn)
    for module in (agents_db, branches_db, listings_db):
        monkeypatch.setattr(module, "_initialized", False)
        module.init()
    routing.reset_cache()
    yield conn
    routing.reset_cache()
    conn.close()


def _session(**overrides):
    session = {
        "tenant_id": TENANT,
        "lead_email": INBOX,
        "locale": "it",
        "listings_shown": [],
        "interested_listings": [],
        "caller_info": {},
        "left_message": None,
        "branch_id": None,
    }
    session.update(overrides)
    return session


# ── resolving the office mid-call ───────────────────────────────────────────
async def test_the_area_the_caller_gave_puts_the_call_in_an_office(monkeypatch):
    branch = branches_db.create(TENANT, "Banská Bystrica")
    monkeypatch.setattr(
        routing, "resolve", lambda *a, **k: _async(branches_db.get(branch["id"], TENANT))
    )
    session = _session()

    await router._resolve_call_branch(session, {"area": "Badín"})

    assert session["branch_id"] == branch["id"]
    # And that is what gets stamped on both persisted rows.
    assert router._call_branch_id(session) == branch["id"]


async def test_routing_blowing_up_leaves_the_call_with_the_agency(monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(routing, "resolve", explode)
    session = _session()

    await router._resolve_call_branch(session, {"area": "Badín"})

    assert session["branch_id"] is None


async def test_the_env_var_demo_has_no_tenant_and_is_skipped(monkeypatch):
    called = []
    monkeypatch.setattr(routing, "resolve", lambda *a, **k: called.append(a))
    session = _session(tenant_id=None)

    await router._resolve_call_branch(session, {"area": "Badín"})

    assert called == []


def test_a_listing_still_decides_the_office_when_there_is_one():
    """A call that touched a listing never goes through the seller path, so the
    two can't fight — but the precedence is pinned anyway."""
    session = _session(
        branch_id="from-the-seller-path",
        interest_agents=[{"branch_id": "from-the-listing"}],
    )
    assert router._call_branch_id(session) == "from-the-seller-path"

    assert router._call_branch_id(
        _session(interest_agents=[{"branch_id": "from-the-listing"}])
    ) == "from-the-listing"


# ── where the lead is emailed ───────────────────────────────────────────────
def test_a_routed_seller_lead_reaches_that_offices_agents():
    branch = branches_db.create(TENANT, "Banská Bystrica")
    other = branches_db.create(TENANT, "Bratislava")
    agents_db.create(TENANT, "Ján Novák", "jan@studio.sk", branch["id"])
    agents_db.create(TENANT, "Eva Kováčová", "eva@studio.sk", other["id"])
    session = _session(branch_id=branch["id"])

    agents = router._branch_agents(session)
    to, cc, routed = router._resolve_lead_recipients(session, agents)

    assert to == ["jan@studio.sk"]
    # The agency keeps a complete record of its own seller leads.
    assert cc == [INBOX]
    assert routed is True


def test_an_office_with_nobody_in_it_falls_back_to_the_inbox():
    branch = branches_db.create(TENANT, "Banská Bystrica")
    session = _session(branch_id=branch["id"])

    agents = router._branch_agents(session)
    to, cc, routed = router._resolve_lead_recipients(session, agents)

    assert (to, cc, routed) == ([INBOX], [], False)


def test_an_unrouted_seller_call_behaves_exactly_as_before():
    session = _session()

    agents = router._branch_agents(session)
    to, cc, routed = router._resolve_lead_recipients(session, agents)

    assert (agents, to, cc, routed) == ([], [INBOX], [], False)


# ── what the model is allowed to see and say ────────────────────────────────
@pytest.mark.parametrize("locale", LOCALES)
def test_office_names_are_offered_in_the_schema_and_never_in_the_prompt(locale):
    content = router._content(locale)
    tools = router._build_accept_config("instructions", content, OFFICES)["tools"]
    leave = next(t for t in tools if t["name"] == "leave_message")

    assert leave["parameters"]["properties"]["branch"]["enum"] == OFFICES
    # A tool schema is never spoken. Prompt text is — and a parenthesised list
    # in the prompt is exactly what once taught her to read example answers
    # aloud after every question.
    prompt = content["system_prompt_body"].lower()
    assert not any(office.lower() in prompt for office in OFFICES)


@pytest.mark.parametrize("locale", LOCALES)
def test_a_single_office_agency_never_learns_the_concept_exists(locale):
    content = router._content(locale)
    for names in ([], ["Milano"]):
        tools = router._build_accept_config("instructions", content, names)["tools"]
        leave = next(t for t in tools if t["name"] == "leave_message")
        # Nothing to choose between, so no property to invent a question about.
        assert "branch" not in leave["parameters"]["properties"]


@pytest.mark.parametrize("locale", LOCALES)
def test_injecting_the_enum_never_mutates_the_shared_tool_definitions(locale):
    content = router._content(locale)
    router._build_accept_config("instructions", content, ["Milano", "Roma"])

    shared = next(t for t in content["tools"] if t["name"] == "leave_message")
    assert "branch" not in shared["parameters"]["properties"]


@pytest.mark.parametrize("locale", LOCALES)
def test_every_locale_can_carry_the_property_location(locale):
    content = router._content(locale)
    leave = next(t for t in content["tools"] if t["name"] == "leave_message")

    # Without this field there is nothing to route on: it is the answer to a
    # question the seller script already asks.
    assert "area" in leave["parameters"]["properties"]
    # Still optional — a caller who never says where is not a failed call.
    assert "area" not in leave["parameters"].get("required", [])


@pytest.mark.parametrize("locale", LOCALES)
def test_the_seller_script_says_to_pass_the_location_it_already_asked_for(locale):
    prompt = router._content(locale)["system_prompt_body"]
    flat = " ".join(prompt.split()).lower()

    assert "'area'" in flat
    # And says not to ask twice — the whole point is that this costs no extra
    # turn on the call.
    assert ("non chiederlo una seconda volta" in flat
            or "nepýtajte sa naň druhýkrát" in flat)


def _async(value):
    """A coroutine returning `value`, for stubbing an async dependency."""
    async def run():
        return value
    return run()

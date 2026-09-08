"""Tests for turning a spoken place into one of the agency's offices
(branches/routing.py).

The case this whole module exists for is the village: a seller says "Badín",
which shares no letters with "Banská Bystrica", so no amount of string matching
finds the office that covers it. Both sit in okres Banská Bystrica, and that is
what resolves it.

The other rule that matters: nothing here may raise or return a wrong office
just to return something. An unrouted lead reaches the agency inbox and is
recoverable; a lead sent to the wrong office is not.
"""

import sqlite3

import pytest

from agents import db as agents_db
from branches import db as branches_db
from branches import routing
from config import settings
from geo import geocode
from listings import db as listings_db
from tenants import db as tenants_db

TENANT = "tenant-a"

# Real coordinates, so the distance arithmetic is exercised against real
# distances rather than made-up ones.
BANSKA_BYSTRICA = (48.7359, 19.1462)
BRATISLAVA = (48.1486, 17.1077)
BADIN = (48.6742, 19.1300)          # ~7 km from Banská Bystrica, okres BB
KOSICE = (48.7164, 21.2611)         # ~150 km from BB, okres Košice


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


def make_branch(name, coords=None, district="", areas=""):
    branch = branches_db.create(TENANT, name, address=name, areas=areas)
    lat, lng = coords or (None, None)
    branches_db.set_location(branch["id"], TENANT, lat, lng, district)
    return branches_db.get(branch["id"], TENANT)


def fake_geocode(monkeypatch, by_place):
    """Stand in for Google. `by_place` maps a spoken place to (coords,
    district), or to None for something Google can't resolve."""
    calls = []

    async def lookup(address, locale="it", timeout=None):
        calls.append(address)
        entry = by_place.get(address)
        if entry is None:
            raise geocode.GeocodeError("No match for that address")
        (lat, lng), district = entry
        return {
            "address": address, "lat": lat, "lng": lng, "place_id": "x",
            "district": district, "locality": address, "precise": False,
        }

    monkeypatch.setattr(geocode, "lookup", lookup)
    return calls


# ── the village case ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_village_resolves_to_the_office_covering_its_district(monkeypatch):
    bb = make_branch("Banská Bystrica", BANSKA_BYSTRICA, "okres Banská Bystrica")
    make_branch("Bratislava", BRATISLAVA, "okres Bratislava III")
    fake_geocode(monkeypatch, {"Badín": (BADIN, "okres Banská Bystrica")})

    branch = await routing.resolve(TENANT, "Badín")

    # The whole point: "Badín" and "Banská Bystrica" have nothing in common as
    # strings, and the district is what bridges them.
    assert branch["id"] == bb["id"]


@pytest.mark.asyncio
async def test_a_village_still_resolves_when_no_office_lists_its_district(monkeypatch):
    """Nobody filled in a district — the nearest office inside the radius
    still catches it."""
    bb = make_branch("Banská Bystrica", BANSKA_BYSTRICA)
    make_branch("Bratislava", BRATISLAVA)
    fake_geocode(monkeypatch, {"Badín": (BADIN, "okres Banská Bystrica")})

    assert (await routing.resolve(TENANT, "Badín"))["id"] == bb["id"]


@pytest.mark.asyncio
async def test_a_property_nobody_is_near_stays_with_the_agency(monkeypatch):
    make_branch("Banská Bystrica", BANSKA_BYSTRICA, "okres Banská Bystrica")
    make_branch("Bratislava", BRATISLAVA, "okres Bratislava III")
    fake_geocode(monkeypatch, {"Košice": (KOSICE, "okres Košice I")})

    # ~150 km from the nearest office. "Nearest" is not the same as "covered",
    # and filing this under Banská Bystrica would be a fiction.
    assert await routing.resolve(TENANT, "Košice") is None


# ── the agency's own word wins ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_coverage_list_beats_geography_and_costs_no_lookup(monkeypatch):
    make_branch("Banská Bystrica", BANSKA_BYSTRICA, "okres Banská Bystrica")
    zvolen = make_branch("Zvolen", BANSKA_BYSTRICA, "okres Zvolen", areas="Badín\nSliač")
    calls = fake_geocode(monkeypatch, {"Badín": (BADIN, "okres Banská Bystrica")})

    branch = await routing.resolve(TENANT, "Badín")

    assert branch["id"] == zvolen["id"]
    assert calls == []  # resolved before Google was ever asked


@pytest.mark.asyncio
async def test_the_coverage_list_tolerates_slovak_declension(monkeypatch):
    zvolen = make_branch("Zvolen", BANSKA_BYSTRICA, areas="Badín")
    make_branch("Bratislava", BRATISLAVA)
    fake_geocode(monkeypatch, {})

    # What a caller actually says, and what speech-to-text hands over.
    assert (await routing.resolve(TENANT, "v Badíne"))["id"] == zvolen["id"]


@pytest.mark.asyncio
async def test_two_offices_claiming_the_same_place_defer_to_the_agency(monkeypatch):
    make_branch("Milano Centro", BANSKA_BYSTRICA, areas="Milano")
    make_branch("Milano Nord", BRATISLAVA, areas="Milano")
    fake_geocode(monkeypatch, {})

    # The agency has said something contradictory; picking one at random would
    # send seller leads to a coin flip.
    assert await routing.resolve(TENANT, "Milano") is None


@pytest.mark.asyncio
async def test_an_office_the_caller_named_wins_outright(monkeypatch):
    make_branch("Banská Bystrica", BANSKA_BYSTRICA, "okres Banská Bystrica")
    bratislava = make_branch("Bratislava", BRATISLAVA, "okres Bratislava III")
    calls = fake_geocode(monkeypatch, {"Badín": (BADIN, "okres Banská Bystrica")})

    branch = await routing.resolve(TENANT, "Badín", branch_name="Bratislava")

    assert branch["id"] == bratislava["id"]
    assert calls == []


# ── failure is always survivable ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_place_google_cannot_resolve_stays_with_the_agency(monkeypatch):
    make_branch("Banská Bystrica", BANSKA_BYSTRICA, "okres Banská Bystrica")
    make_branch("Bratislava", BRATISLAVA, "okres Bratislava III")
    fake_geocode(monkeypatch, {})

    assert await routing.resolve(TENANT, "mmmh niekde pri lese") is None


@pytest.mark.asyncio
async def test_a_geocoder_that_blows_up_does_not_take_the_call_with_it(monkeypatch):
    make_branch("Banská Bystrica", BANSKA_BYSTRICA, "okres Banská Bystrica")
    make_branch("Bratislava", BRATISLAVA, "okres Bratislava III")

    async def explode(*args, **kwargs):
        raise RuntimeError("Google is having a day")

    monkeypatch.setattr(geocode, "lookup", explode)

    # A caller is on the line. No exception may escape.
    assert await routing.resolve(TENANT, "Badín") is None


@pytest.mark.asyncio
async def test_no_location_at_all_stays_with_the_agency(monkeypatch):
    make_branch("Banská Bystrica", BANSKA_BYSTRICA, "okres Banská Bystrica")
    make_branch("Bratislava", BRATISLAVA, "okres Bratislava III")
    fake_geocode(monkeypatch, {})

    assert await routing.resolve(TENANT, None) is None
    assert await routing.resolve(TENANT, "   ") is None


# ── agencies that have no choice to make ────────────────────────────────────
@pytest.mark.asyncio
async def test_an_agency_with_one_office_routes_everything_to_it(monkeypatch):
    only = make_branch("Banská Bystrica", BANSKA_BYSTRICA, "okres Banská Bystrica")
    calls = fake_geocode(monkeypatch, {"Košice": (KOSICE, "okres Košice I")})

    # No choice to make, so no lookup to pay for — and the call still gets
    # attributed, which is the point of not short-circuiting to None.
    branch = await routing.resolve(TENANT, "Košice")

    assert branch["id"] == only["id"]
    assert calls == []


@pytest.mark.asyncio
async def test_an_agency_with_no_offices_resolves_to_nothing(monkeypatch):
    fake_geocode(monkeypatch, {})
    assert await routing.resolve(TENANT, "Badín") is None


# ── the cache ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_same_village_is_only_geocoded_once(monkeypatch):
    bb = make_branch("Banská Bystrica", BANSKA_BYSTRICA, "okres Banská Bystrica")
    make_branch("Bratislava", BRATISLAVA, "okres Bratislava III")
    calls = fake_geocode(monkeypatch, {
        "Badín": (BADIN, "okres Banská Bystrica"),
        "v Badíne": (BADIN, "okres Banská Bystrica"),
    })

    for spoken in ("Badín", "Badín", "badin", "v Badíne", "v Badíne"):
        assert (await routing.resolve(TENANT, spoken))["id"] == bb["id"]

    # Cached on the normalised string, so repeats and diacritic differences are
    # free. A declined form is a separate entry rather than being stemmed into
    # the same one: a fuzzy cache key could collide two genuinely different
    # towns, and Google resolves the declined form correctly anyway. Two
    # wordings, two lookups, one office.
    assert calls == ["Badín", "v Badíne"]


@pytest.mark.asyncio
async def test_a_place_that_failed_is_not_retried_on_the_next_call(monkeypatch):
    make_branch("Banská Bystrica", BANSKA_BYSTRICA, "okres Banská Bystrica")
    make_branch("Bratislava", BRATISLAVA, "okres Bratislava III")
    calls = fake_geocode(monkeypatch, {})

    await routing.resolve(TENANT, "niekde tam")
    await routing.resolve(TENANT, "niekde tam")

    # Retrying spends another caller's patience for the same answer.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_coverage_edit_takes_effect_without_clearing_the_cache(monkeypatch):
    """Only the geocode is cached, never the decision — so the agency's edits
    apply to the next call, not the next restart."""
    bb = make_branch("Banská Bystrica", BANSKA_BYSTRICA, "okres Banská Bystrica")
    zvolen = make_branch("Zvolen", BANSKA_BYSTRICA, "okres Zvolen")
    fake_geocode(monkeypatch, {"Badín": (BADIN, "okres Banská Bystrica")})

    assert (await routing.resolve(TENANT, "Badín"))["id"] == bb["id"]

    branches_db.update(zvolen["id"], TENANT, {"areas": "Badín"})

    assert (await routing.resolve(TENANT, "Badín"))["id"] == zvolen["id"]


# ── radius ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_radius_is_configurable(monkeypatch):
    bb = make_branch("Banská Bystrica", BANSKA_BYSTRICA)
    make_branch("Bratislava", BRATISLAVA)
    fake_geocode(monkeypatch, {"Košice": (KOSICE, "okres Košice I")})

    monkeypatch.setattr(settings, "BRANCH_MATCH_RADIUS_KM", 200.0)

    # An agency that really does work a whole region from one address.
    assert (await routing.resolve(TENANT, "Košice"))["id"] == bb["id"]


# ── who a routed lead reaches ───────────────────────────────────────────────
def test_lead_agents_are_the_offices_own_people():
    bb = make_branch("Banská Bystrica", BANSKA_BYSTRICA)
    other = make_branch("Bratislava", BRATISLAVA)
    agents_db.create(TENANT, "Ján Novák", "jan@studio.sk", bb["id"])
    agents_db.create(TENANT, "Eva Kováčová", "eva@studio.sk", other["id"])
    agents_db.create(TENANT, "Nikto Nikde", "nikto@studio.sk")

    names = [a["name"] for a in routing.lead_agents(TENANT, bb["id"])]

    assert names == ["Ján Novák"]

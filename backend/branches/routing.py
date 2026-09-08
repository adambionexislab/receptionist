"""Which of an agency's offices a caller's property belongs to.

A seller rings the agency's one number and says where their property is. This
turns that spoken place into a branch, so the lead reaches the office that can
actually service it instead of a shared inbox — and so the dashboard can count
the call under that office.

THE ORDER MATTERS, and each step exists because the one before it can't cover
the case:

  1. The caller named an office outright. Nothing to infer; use it.
  2. The place is on an office's coverage list (branches.areas). The agency's
     own word about its catchment, so it wins over anything Google says, it
     costs no API call, and it is how two offices in one city get separated.
  3. The place's administrative district matches an office's. This is the step
     that handles villages: "Badín" shares no letters with "Banská Bystrica",
     so no amount of string matching finds it, but both sit in okres Banská
     Bystrica. One geocode answers it for every settlement in the country,
     with nothing to maintain by hand.
  4. The nearest office, within a sanity radius. Catches a district nobody
     listed, and is the right discriminator inside a city, where catchment
     genuinely is geographic. The radius is what stops a property 200 km away
     being filed under the least-distant office as though it were covered.
  5. Nothing. The lead goes to the agency inbox exactly as it did before
     branches existed, and the call shows in the unassigned bucket.

NOTHING HERE MAY RAISE OR HANG. It runs inside a tool call while Apollonia's
turn is blocked and the caller is listening to silence, so the geocode gets a
short timeout and every failure resolves to None. A lead in the agency inbox is
a good outcome; a dead pause on the phone is not.
"""

import logging
import math
from typing import Any, Optional

from agents import db as agents_db
from branches import db as branches_db
from config import settings
from geo import geocode
from listings.store import normalize_place, place_word_in

logger = logging.getLogger(__name__)

# Geocode results, keyed by (locale, normalised place). Callers name the same
# handful of towns over and over, so this turns the second seller from Badín
# into a free lookup. Failures are cached as None too — a place Google can't
# resolve won't resolve on the next call either, and retrying it would spend
# another few hundred milliseconds of the caller's patience.
_GEOCODE_CACHE: dict[tuple[str, str], Optional[dict[str, Any]]] = {}
_CACHE_MAX = 512


def _cache_get(key: tuple[str, str]) -> tuple[bool, Optional[dict[str, Any]]]:
    """(hit, value) — distinguishing a cached failure from a miss."""
    if key in _GEOCODE_CACHE:
        return True, _GEOCODE_CACHE[key]
    return False, None


def _cache_put(key: tuple[str, str], value: Optional[dict[str, Any]]) -> None:
    # Crude bound: a dict this size is a few hundred KB, and the working set is
    # one agency's towns. Clearing beats evicting cleverly at this size.
    if len(_GEOCODE_CACHE) >= _CACHE_MAX:
        _GEOCODE_CACHE.clear()
    _GEOCODE_CACHE[key] = value


def reset_cache() -> None:
    """Drop the geocode cache. For tests, and for the rare case of an operator
    wanting a re-lookup without a restart."""
    _GEOCODE_CACHE.clear()


def _distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance. Straight-line, not driving distance — good enough
    to rank offices that are tens of kilometres apart, and it needs no API."""
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def coverage(branch: dict[str, Any]) -> list[str]:
    """One office's coverage list, as entered by the agency — one place per
    line, blank lines ignored."""
    return [line.strip() for line in (branch.get("areas") or "").splitlines() if line.strip()]


def _by_name(branches: list[dict[str, Any]], name: Optional[str]) -> Optional[dict[str, Any]]:
    """The office the caller named. Matched on the normalised name, since the
    model is handing back a value it read from an enum but transcription and
    diacritics can still differ."""
    if not name:
        return None
    wanted = normalize_place(name.strip())
    if not wanted:
        return None
    for branch in branches:
        if normalize_place(branch["name"]) == wanted:
            return branch
    return None


def _by_coverage(
    branches: list[dict[str, Any]], area: str
) -> tuple[Optional[dict[str, Any]], bool]:
    """(branch, ambiguous) from the agency's own coverage lists.

    Ambiguous when two offices both claim the place — the agency has said
    something contradictory, so this defers rather than picking one at random.
    """
    hits = [
        branch for branch in branches
        if any(place_word_in(area, place) for place in coverage(branch))
    ]
    if len(hits) == 1:
        return hits[0], False
    return None, len(hits) > 1


def _by_district(
    branches: list[dict[str, Any]], district: Optional[str]
) -> tuple[Optional[dict[str, Any]], bool]:
    """(branch, ambiguous) from the okres/provincia. Two offices in one
    district is the ordinary two-branches-in-one-city case, and distance
    separates them better than a coin flip, so this defers to step 4."""
    if not district:
        return None, False
    hits = [
        branch for branch in branches
        if branch.get("district") and place_word_in(district, branch["district"])
    ]
    if len(hits) == 1:
        return hits[0], False
    return None, len(hits) > 1


def _nearest(
    branches: list[dict[str, Any]], lat: float, lng: float
) -> Optional[dict[str, Any]]:
    """The closest office with coordinates, if it is close enough to plausibly
    cover the property."""
    located = [b for b in branches if b.get("lat") is not None and b.get("lng") is not None]
    if not located:
        return None
    best = min(located, key=lambda b: _distance_km(lat, lng, b["lat"], b["lng"]))
    distance = _distance_km(lat, lng, best["lat"], best["lng"])
    if distance > settings.BRANCH_MATCH_RADIUS_KM:
        logger.info(
            "Nearest office %r is %.0f km from the property — beyond the %s km "
            "radius, leaving this lead with the agency",
            best["name"], distance, settings.BRANCH_MATCH_RADIUS_KM,
        )
        return None
    return best


async def _geocode(area: str, locale: str) -> Optional[dict[str, Any]]:
    """Geocode a spoken place, cached, short-timeout, never raising."""
    key = (locale, normalize_place(area))
    hit, cached = _cache_get(key)
    if hit:
        return cached

    result: Optional[dict[str, Any]] = None
    try:
        result = await geocode.lookup(
            area, locale, timeout=settings.BRANCH_GEOCODE_TIMEOUT_SECONDS
        )
    except geocode.GeocodeError as exc:
        logger.info("Could not geocode %r for branch routing: %s", area, exc)
    except Exception:
        # A bug here must not take the call down with it.
        logger.exception("Unexpected error geocoding %r for branch routing", area)
    _cache_put(key, result)
    return result


async def resolve(
    tenant_id: str,
    area: Optional[str],
    branch_name: Optional[str] = None,
    locale: str = "it",
) -> Optional[dict[str, Any]]:
    """The office a property in `area` belongs to, or None to leave it with the
    agency. See the module docstring for the order and why each step is there.

    Safe to call for any tenant: an agency with fewer than two offices short-
    circuits, because there is nothing to choose between.
    """
    branches = branches_db.list_for_tenant(tenant_id)
    if not branches:
        return None
    if len(branches) == 1:
        # One office is the whole agency. Routing to it is free and correct,
        # and it means a single-office agency still gets its calls attributed.
        return branches[0]

    named = _by_name(branches, branch_name)
    if named:
        return named

    area = (area or "").strip()
    if not area:
        return None

    covered, ambiguous = _by_coverage(branches, area)
    if covered:
        return covered
    if ambiguous:
        logger.info(
            "Tenant %s has two offices claiming %r in their coverage lists — "
            "leaving this lead with the agency", tenant_id, area,
        )
        return None

    located = await _geocode(area, locale)
    if not located:
        return None

    district_hit, _ = _by_district(branches, located.get("district"))
    if district_hit:
        return district_hit

    return _nearest(branches, located["lat"], located["lng"])


def lead_agents(tenant_id: str, branch_id: str) -> list[dict[str, Any]]:
    """The agents a branch-routed lead should be emailed to: everyone in that
    office. The agency inbox still gets a Cc — see call/router's
    _resolve_lead_recipients, which this feeds."""
    return agents_db.list_for_tenant(tenant_id, branch_id=branch_id)

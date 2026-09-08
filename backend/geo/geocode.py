"""Address to coordinates, via the Google Geocoding API.

Deliberately server-side. The video wizard could geocode in the browser with
the Maps JS SDK, but that would mean the browser key covering Geocoding too,
and a referrer-restricted key is still scrapeable off the page. Keeping the
lookup here means the geocoding key never leaves the server, and the browser
key is scoped to nothing but drawing a map.

Two callers, with opposite tolerances:

  videotour/router.py — an agent is typing in step 1 and waiting for a map to
      move. Slow is fine; wrong is not.
  branches/routing.py — a caller is on the phone and Apollonia's turn is
      blocked on the answer. This one passes a short `timeout` and treats any
      failure as "no match", because a lead routed to the agency inbox is a
      far better outcome than a silence on the line.

Beyond coordinates it returns the administrative district (okres in Slovakia,
provincia in Italy) and whether the match was street-precise, which is what
lets a village be resolved to the office that covers its district rather than
to whichever office happens to be nearest a city centroid.
"""

import logging
from typing import Any, Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_TIMEOUT = 15

# Google's component type for the second-level administrative area: the okres
# in Slovakia, the provincia in Italy — the unit an agency's catchment actually
# follows. Level 1 (kraj / regione) is the fallback for the handful of places
# that carry no level 2.
_DISTRICT_TYPES = ("administrative_area_level_2", "administrative_area_level_1")

# A geocode is street-precise when Google says it pinned a building or
# interpolated along a street. GEOMETRIC_CENTER/APPROXIMATE mean it fell back
# to the centre of a locality — good enough to pick a district, not good enough
# to pick between two offices in the same town.
_PRECISE_LOCATION_TYPES = ("ROOFTOP", "RANGE_INTERPOLATED")


class GeocodeError(Exception):
    """Raised when the lookup fails or the address can't be resolved."""


def _component(result: dict[str, Any], types: tuple[str, ...]) -> Optional[str]:
    """The long_name of the first address component matching `types`, in the
    order given — so a missing okres falls through to the kraj."""
    components = result.get("address_components") or []
    for wanted in types:
        for component in components:
            if wanted in (component.get("types") or []):
                name = (component.get("long_name") or "").strip()
                if name:
                    return name
    return None


async def lookup(
    address: str, locale: str = "it", timeout: Optional[float] = None
) -> dict[str, Any]:
    """Resolve a free-text address to coordinates, its district and a
    normalised label.

    `locale` goes through as the `language` param so the formatted address
    comes back in the tenant's language rather than English — it is shown back
    to the agent on the collapsed step-1 card, so it has to read naturally.

    `timeout` overrides the default for callers that cannot afford to wait
    (see the module docstring).
    """
    key = settings.GOOGLE_GEOCODING_API_KEY
    if not key:
        raise GeocodeError("GOOGLE_GEOCODING_API_KEY not configured")
    address = (address or "").strip()
    if not address:
        raise GeocodeError("Address is empty")

    try:
        async with httpx.AsyncClient(timeout=timeout or _TIMEOUT) as client:
            resp = await client.get(
                _GEOCODE_URL,
                params={
                    "address": address,
                    "key": key,
                    "language": locale,
                    # Bias toward the markets we actually serve, so "Via Roma"
                    # resolves to the Italian one rather than a namesake abroad.
                    "region": "sk" if locale == "sk" else "it",
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        logger.error("Geocoding request failed: %s", exc)
        raise GeocodeError(f"Geocoding request failed: {exc}") from exc

    status = data.get("status")
    if status == "ZERO_RESULTS":
        raise GeocodeError("No match for that address")
    if status != "OK" or not data.get("results"):
        # error_message carries the real cause (key restrictions, API not
        # enabled, quota) and is worth having in the logs verbatim.
        logger.error(
            "Geocoding returned %s: %s", status, data.get("error_message", ""),
        )
        raise GeocodeError(f"Geocoding failed ({status})")

    best = data["results"][0]
    location = best["geometry"]["location"]
    return {
        "address": best.get("formatted_address") or address,
        "lat": float(location["lat"]),
        "lng": float(location["lng"]),
        "place_id": best.get("place_id"),
        # Okres / provincia. None when Google returns no administrative
        # component at all, which happens for some ambiguous free text.
        "district": _component(best, _DISTRICT_TYPES),
        "locality": _component(best, ("locality", "postal_town")),
        "precise": (
            best["geometry"].get("location_type") in _PRECISE_LOCATION_TYPES
        ),
    }

"""Address to coordinates, via the Google Geocoding API.

Deliberately server-side. The wizard could geocode in the browser with the
Maps JS SDK, but that would mean the browser key covering Geocoding too, and a
referrer-restricted key is still scrapeable off the page. Keeping the lookup
here means the geocoding key never leaves the server, and the browser key is
scoped to nothing but drawing a map.

Called by videotour/router.py while the agent is still typing in step 1; the
coordinates it returns are only a starting point, since the agent then drags
the marker and the confirmed lat/lng is what gets stored.
"""

import logging
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)

_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_TIMEOUT = 15


class GeocodeError(Exception):
    """Raised when the lookup fails or the address can't be resolved."""


async def lookup(address: str, locale: str = "it") -> dict[str, Any]:
    """Resolve a free-text address to coordinates plus a normalised label.

    `locale` goes through as the `language` param so the formatted address
    comes back in the tenant's language rather than English — it is shown back
    to the agent on the collapsed step-1 card, so it has to read naturally.
    """
    key = settings.GOOGLE_GEOCODING_API_KEY
    if not key:
        raise GeocodeError("GOOGLE_GEOCODING_API_KEY not configured")
    address = (address or "").strip()
    if not address:
        raise GeocodeError("Address is empty")

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
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
    }

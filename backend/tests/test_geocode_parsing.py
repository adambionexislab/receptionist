"""Tests for reading a Google Geocoding response (geo/geocode.py).

These pin the three fields branch routing actually depends on — the district,
the coordinates, and whether the match was street-precise — against realistic
v3 payloads.

Worth having because those field names are exactly what changes if this ever
moves to Geocoding API v4: v3's snake_case `address_components[].long_name`
becomes `addressComponents[].longText`, and `geometry.location_type` becomes
`granularity`. Everything else in the codebase talks to this module rather than
to Google, so these tests are the ones that would go red on that migration —
which is the point of writing them.
"""

import pytest

from config import settings
from geo import geocode


def response(components, location_type="APPROXIMATE"):
    """A v3 geocode response, trimmed to the parts this module reads."""
    return {
        "status": "OK",
        "results": [{
            "formatted_address": "Badín, Slovensko",
            "place_id": "abc123",
            "address_components": components,
            "geometry": {
                "location": {"lat": 48.6742, "lng": 19.1300},
                "location_type": location_type,
            },
        }],
    }


def component(long_name, *types):
    return {"long_name": long_name, "short_name": long_name[:3], "types": list(types)}


BADIN = [
    component("Badín", "locality", "political"),
    component("okres Banská Bystrica", "administrative_area_level_2", "political"),
    component("Banskobystrický kraj", "administrative_area_level_1", "political"),
    component("Slovensko", "country", "political"),
]


@pytest.fixture(autouse=True)
def google(monkeypatch):
    """Stub the HTTP call, keeping the parsing under test real."""
    state = {"payload": response(BADIN)}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return state["payload"]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            state["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            state["params"] = params
            return FakeResponse()

    monkeypatch.setattr(geocode.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(settings, "GOOGLE_GEOCODING_API_KEY", "test-key")
    return state


async def test_the_district_is_the_okres(google):
    result = await geocode.lookup("Badín", "sk")

    # This one value is what routes a village to the office covering it.
    assert result["district"] == "okres Banská Bystrica"
    assert result["locality"] == "Badín"
    assert (result["lat"], result["lng"]) == (48.6742, 19.1300)


async def test_a_place_with_no_okres_falls_back_to_the_region(google):
    google["payload"] = response([
        component("Slovensko", "country", "political"),
        component("Banskobystrický kraj", "administrative_area_level_1", "political"),
    ])

    # Better a coarse district than none: an office listing the kraj still
    # matches, and the nearest-office step is still there underneath.
    assert (await geocode.lookup("niekde", "sk"))["district"] == "Banskobystrický kraj"


async def test_a_response_with_no_administrative_components_has_no_district(google):
    google["payload"] = response([component("Slovensko", "country", "political")])

    assert (await geocode.lookup("niekde", "sk"))["district"] is None


@pytest.mark.parametrize(
    "location_type,precise",
    [
        ("ROOFTOP", True),
        ("RANGE_INTERPOLATED", True),
        # A town name geocodes to the centre of the town, which is not good
        # enough to choose between two offices inside it.
        ("GEOMETRIC_CENTER", False),
        ("APPROXIMATE", False),
    ],
)
async def test_precision_distinguishes_an_address_from_a_town_centroid(
    google, location_type, precise
):
    google["payload"] = response(BADIN, location_type)

    assert (await geocode.lookup("Badín", "sk"))["precise"] is precise


async def test_the_short_timeout_is_honoured(google):
    await geocode.lookup("Badín", "sk", timeout=3)

    # A caller is on the line; the module docstring's contract in numbers.
    assert google["timeout"] == 3
    assert google["params"]["region"] == "sk"


async def test_no_results_is_an_error_not_an_empty_answer(google):
    google["payload"] = {"status": "ZERO_RESULTS", "results": []}

    with pytest.raises(geocode.GeocodeError):
        await geocode.lookup("mmmh", "sk")


async def test_a_missing_key_fails_before_any_request(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_GEOCODING_API_KEY", None)

    with pytest.raises(geocode.GeocodeError):
        await geocode.lookup("Badín", "sk")

import pytest

from listings.seed import get_seed_listings
from listings.store import store


@pytest.fixture(autouse=True)
def seed_store():
    store._listings = get_seed_listings()
    yield
    store._listings = []


def test_search_no_filters_returns_all():
    results = store.search()
    assert len(results) == len(get_seed_listings())


def test_search_type_affitto_returns_only_affitto():
    results = store.search(type="affitto")
    assert len(results) > 0
    assert all(l["type"] == "affitto" for l in results)


def test_search_max_price_excludes_above_threshold():
    threshold = 1000
    results = store.search(max_price=threshold)
    assert all(l["price"] <= threshold for l in results)
    assert len(results) < len(get_seed_listings())


def test_search_zone_partial_match_is_case_insensitive():
    results = store.search(zone="LODI")
    assert len(results) > 0
    assert all("lodi" in l["zone"].lower() for l in results)


def test_search_no_matching_results_returns_empty_list():
    results = store.search(zone="ZZZZNONEXISTENT99")
    assert results == []


def _sk_listing(address, zone, rooms, text):
    return {
        "address": address, "zone": zone, "type": "vendita", "rooms": rooms,
        "size_sqm": 60, "price": 150000, "currency": "EUR", "available": True,
        "text": text,
    }


@pytest.fixture
def project_listings():
    store._listings = [
        _sk_listing("Pezinská 19, Bratislava", "Bratislava - Ružinov", 2,
                    "Nový byt v projekte Rezidencia Anička, balkón."),
        _sk_listing("Obchodná 5, Bratislava", "Bratislava - Staré Mesto", 3,
                    "Trojizbový byt v rezidencii pri centre."),
        _sk_listing("Hlavná 45, Košice", "Košice - Staré Mesto", 3,
                    "Byt vedľa ulice Pezinská, pivnica."),
    ]


def test_get_by_address_finds_project_named_only_in_description(project_listings):
    results = store.get_by_address("Anička rezidencia")
    assert [l["address"] for l in results] == ["Pezinská 19, Bratislava"]


def test_get_by_address_matches_declined_project_name(project_listings):
    results = store.get_by_address("v Aničke")
    assert [l["address"] for l in results] == ["Pezinská 19, Bratislava"]


def test_get_by_address_exact_address_beats_description_mention(project_listings):
    # The Košice text mentions the street too; the listing AT it must win.
    results = store.get_by_address("Pezinská 19")
    assert [l["address"] for l in results] == ["Pezinská 19, Bratislava"]


def test_get_by_address_returns_every_listing_at_the_same_address(project_listings):
    store._listings.append(
        _sk_listing("Pezinská 19, Bratislava", "Bratislava - Ružinov", 3, "Väčší byt.")
    )
    results = store.get_by_address("Pezinská 19")
    assert sorted(l["rooms"] for l in results) == [2, 3]


def test_get_by_address_no_match_returns_empty_list(project_listings):
    assert store.get_by_address("Zzyzx Qwerty") == []

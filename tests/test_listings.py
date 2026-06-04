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

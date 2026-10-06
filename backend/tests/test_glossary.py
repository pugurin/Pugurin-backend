from uuid import UUID, uuid4

import pytest

from tests.conftest import API

FORBIDDEN_PHRASES = ("오를 것", "오릅니다", "사세요", "투자하세요", "유망", "추천")


def all_terms(client, **params):
    r = client.get(f"{API}/glossary", params=params)
    assert r.status_code == 200
    return r.json()["data"]


def test_list_is_sorted_by_display_order_then_term(client):
    terms = all_terms(client)
    keys = [(t["display_order"], t["term"]) for t in terms]
    assert keys == sorted(keys) and len(terms) >= 20


def test_term_shape(client):
    for t in all_terms(client):
        assert set(t) == {
            "id",
            "term",
            "category",
            "is_popular",
            "display_order",
            "short_definition",
            "long_definition",
            "example",
        }
        UUID(t["id"])
        assert t["category"] in {"trade", "land", "building", "tax"}
        assert t["short_definition"] and t["long_definition"] and t["example"]


def test_ids_and_orders_are_unique(client):
    terms = all_terms(client)
    assert len({t["id"] for t in terms}) == len(terms)
    assert len({t["term"] for t in terms}) == len(terms)


@pytest.mark.parametrize("category", ["trade", "land", "building", "tax"])
def test_category_filter(client, category):
    terms = all_terms(client, category=category)
    assert terms and all(t["category"] == category for t in terms)


def test_unknown_category_is_400(client):
    r = client.get(f"{API}/glossary", params={"category": "legal"})
    assert r.status_code == 400 and r.json()["error"]["field"] == "category"


def test_popular_filter(client):
    popular = all_terms(client, popular="true")
    assert popular and all(t["is_popular"] for t in popular)
    assert len(popular) < len(all_terms(client))


def test_search_matches_term_and_description(client):
    assert [t["term"] for t in all_terms(client, q="용적률")][0] == "용적률"
    by_description = all_terms(client, q="가축")
    assert any(t["term"] == "가축사육제한구역" for t in by_description)
    assert all_terms(client, q="존재하지않는말") == []


def test_search_combines_with_category(client):
    terms = all_terms(client, q="세", category="tax")
    assert terms and all(t["category"] == "tax" for t in terms)


def test_get_single_term(client):
    first = all_terms(client)[0]
    r = client.get(f"{API}/glossary/{first['id']}")
    assert r.status_code == 200 and r.json()["data"] == first


def test_unknown_term_is_404(client):
    r = client.get(f"{API}/glossary/{uuid4()}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_ids_are_stable_across_apps():
    from tests.conftest import make_client

    with make_client() as a, make_client() as b:
        assert all_terms(a) == all_terms(b)


def test_no_investment_advice_phrases(client):
    for t in all_terms(client):
        text = " ".join([t["short_definition"], t["long_definition"], t["example"]])
        assert not any(p in text for p in FORBIDDEN_PHRASES), t["term"]


def test_terms_used_by_parcel_responses_exist(client):
    names = {t["term"] for t in all_terms(client)}
    assert {"지목", "공시지가", "도로접면", "용도지역", "건폐율", "용적률", "가축사육제한구역"} <= names

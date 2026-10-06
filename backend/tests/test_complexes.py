from uuid import UUID, uuid4

import pytest

from app.core.dates import latest_etl_time
from app.repositories.types import PropertyType
from app.sample_data.market import build_sample_market
from tests.conftest import API, FIXED_NOW, HAEUNDAE

TRANSACTION_FIELDS = {
    "id",
    "property_type",
    "deal_type",
    "complex_id",
    "address",
    "region_code",
    "jibun",
    "pnu",
    "location_precision",
    "lat",
    "lng",
    "price",
    "deposit",
    "monthly_rent",
    "exclusive_area_m2",
    "exclusive_area_pyeong",
    "price_per_pyeong",
    "floor",
    "contract_date",
    "build_year",
    "trade_method",
    "is_cancelled",
    "cancelled_at",
}


def first_complex_id(client, property_type="apartment") -> str:
    r = client.get(f"{API}/map/markers", params={"bbox": HAEUNDAE, "zoom": 15, "property_type": property_type})
    return r.json()["data"]["markers"][0]["complex_id"]


def test_get_complex_shape(client):
    cid = first_complex_id(client)
    r = client.get(f"{API}/complexes/{cid}")
    assert r.status_code == 200
    data = r.json()["data"]
    assert set(data) == {
        "id",
        "property_type",
        "name",
        "address",
        "region_code",
        "pnu",
        "lat",
        "lng",
        "build_year",
        "household_count",
        "area_types",
    }
    assert data["id"] == cid and data["property_type"] == "apartment"
    assert data["address"].startswith("부산 ") and len(data["region_code"]) == 10 and len(data["pnu"]) == 19
    assert data["area_types"]
    for a in data["area_types"]:
        assert set(a) == {"exclusive_area_m2", "exclusive_area_pyeong", "supply_area_pyeong"}
        assert a["exclusive_area_pyeong"] == round(a["exclusive_area_m2"] / 3.3058, 1)


def test_villa_supply_area_is_null(client):
    data = client.get(f"{API}/complexes/{first_complex_id(client, 'villa')}").json()["data"]
    assert all(a["supply_area_pyeong"] is None for a in data["area_types"])


def test_unknown_complex_is_404(client):
    r = client.get(f"{API}/complexes/{uuid4()}")
    assert r.status_code == 404
    assert r.json() == {"error": {"code": "RESOURCE_NOT_FOUND", "message": "단지를 찾을 수 없습니다", "field": None}}


def test_malformed_complex_id_is_400(client):
    r = client.get(f"{API}/complexes/abc")
    assert r.status_code == 400
    assert r.json()["error"]["field"] == "complex_id"


def test_transactions_pagination_and_order(client):
    cid = first_complex_id(client)
    r = client.get(f"{API}/complexes/{cid}/transactions", params={"page_size": 5})
    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]) == 5
    meta = body["meta"]
    assert meta["page"] == 1 and meta["page_size"] == 5 and meta["total"] > 5
    assert meta["total_pages"] == -(-meta["total"] // 5)
    assert meta["reporting_lag_notice"] is True and meta["data_as_of"]
    dates = [t["contract_date"] for t in body["data"]]
    assert dates == sorted(dates, reverse=True)

    page2 = client.get(f"{API}/complexes/{cid}/transactions", params={"page_size": 5, "page": 2}).json()
    assert {t["id"] for t in page2["data"]}.isdisjoint({t["id"] for t in body["data"]})


def test_transaction_fields_and_price_per_pyeong(client):
    cid = first_complex_id(client)
    items = client.get(f"{API}/complexes/{cid}/transactions", params={"page_size": 100}).json()["data"]
    for t in items:
        assert set(t) == TRANSACTION_FIELDS
        assert t["location_precision"] == "parcel" and t["complex_id"] == cid
        if t["deal_type"] == "sale":
            assert t["price_per_pyeong"] == pytest.approx(t["price"] / (t["exclusive_area_m2"] / 3.3058), abs=1)
        else:
            assert t["price_per_pyeong"] is None


def test_transaction_filters(client):
    cid = first_complex_id(client)
    url = f"{API}/complexes/{cid}/transactions"
    jeonse = client.get(url, params={"deal_type": "jeonse", "page_size": 100}).json()["data"]
    assert jeonse and all(t["deal_type"] == "jeonse" for t in jeonse)
    broker_only = client.get(url, params={"exclude_direct": "true", "page_size": 100}).json()["data"]
    assert all(t["trade_method"] == "broker" for t in broker_only)
    cheap_first = client.get(url, params={"deal_type": "sale", "sort": "price_asc", "page_size": 100}).json()["data"]
    prices = [t["price"] for t in cheap_first]
    assert prices == sorted(prices)


def test_cancelled_transactions_hidden_unless_requested(client):
    market = build_sample_market(latest_etl_time(FIXED_NOW).date())
    cancelled_by_complex: dict = {}
    for tx in market.transactions:
        if tx.complex_id and tx.is_cancelled and tx.property_type == PropertyType.apartment:
            cancelled_by_complex.setdefault(tx.complex_id, []).append(tx)
    cid, cancelled = max(cancelled_by_complex.items(), key=lambda kv: len(kv[1]))
    url = f"{API}/complexes/{cid}/transactions"

    default = client.get(url, params={"page_size": 100}).json()
    assert all(not t["is_cancelled"] for t in default["data"])
    with_cancelled = client.get(url, params={"page_size": 100, "include_cancelled": "true"}).json()
    assert with_cancelled["meta"]["total"] == default["meta"]["total"] + len(cancelled)
    shown = [t for t in with_cancelled["data"] if t["is_cancelled"]]
    assert shown and all(t["cancelled_at"] for t in shown)


def test_transactions_of_unknown_complex_is_404(client):
    assert client.get(f"{API}/complexes/{uuid4()}/transactions").status_code == 404


@pytest.mark.parametrize("params", [{"page_size": 101}, {"page_size": 0}, {"page": 0}, {"sort": "random"}])
def test_transactions_invalid_query(client, params):
    r = client.get(f"{API}/complexes/{first_complex_id(client)}/transactions", params=params)
    assert r.status_code == 400
    assert r.json()["error"]["field"] == next(iter(params))


def test_complex_ids_are_stable_across_apps():
    from tests.conftest import make_client

    with make_client() as a, make_client() as b:
        assert first_complex_id(a) == first_complex_id(b)
        UUID(first_complex_id(a))

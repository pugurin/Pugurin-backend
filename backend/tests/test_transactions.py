import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.memory.dataset import MarketDataset
from app.repositories.memory.market import InMemoryMarketRepository
from tests.conftest import API, BUSAN, FIXED_NOW
from tests.test_stats import COMPLEX, DONG, EOK, OTHER_COMPLEX, OTHER_DONG, REGIONS, SIGUNGU, TRANSACTIONS

HEADERS = {"X-Device-Id": "2f8f1d52-7b0e-4c55-9c0e-3f6f9d1c2a11"}
FIELDS = {
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
AROUND_COMPLEX = "129.15,35.15,129.165,35.165"
AROUND_BOTH = "129.15,35.15,129.18,35.18"


@pytest.fixture(scope="module")
def tx_client():
    repo = InMemoryMarketRepository(FIXED_NOW, MarketDataset((COMPLEX, OTHER_COMPLEX), tuple(TRANSACTIONS)), REGIONS)
    with TestClient(create_app(clock=lambda: FIXED_NOW, market_repo=repo), headers=HEADERS) as c:
        yield c


def listing(client, **params):
    params.setdefault("property_type", "apartment")
    return client.get(f"{API}/transactions", params=params)


def rows(client, **params):
    r = listing(client, **params)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_place_is_required(tx_client):
    r = listing(tx_client)
    assert r.status_code == 400 and r.json()["error"]["field"] == "bbox"


@pytest.mark.parametrize(("code", "count"), [(DONG, 14), (OTHER_DONG, 3), (SIGUNGU, 17)])
def test_region_code_matches_a_dong_or_every_dong_in_a_district(tx_client, code, count):
    assert rows(tx_client, region_code=code, page_size=100)["meta"]["total"] == count


def test_bbox_selects_by_location(tx_client):
    assert rows(tx_client, bbox=AROUND_COMPLEX)["meta"]["total"] == 14
    assert rows(tx_client, bbox=AROUND_BOTH)["meta"]["total"] == 17
    assert rows(tx_client, bbox="127.0,37.4,127.1,37.6")["data"] == []


def test_region_and_bbox_together_narrow_the_result(tx_client):
    assert rows(tx_client, region_code=SIGUNGU, bbox=AROUND_COMPLEX)["meta"]["total"] == 14


@pytest.mark.parametrize("bad", ["123", "abcde", "263501050"])
def test_malformed_region_code_is_400(tx_client, bad):
    r = listing(tx_client, region_code=bad)
    assert r.status_code == 400 and r.json()["error"]["field"] == "region_code"


def test_row_fields_match_the_spec(tx_client):
    body = rows(tx_client, region_code=DONG)
    assert body["data"] and all(set(item) == FIELDS for item in body["data"])
    assert all(item["property_type"] == "apartment" and item["deal_type"] == "sale" for item in body["data"])


def test_cancelled_trades_are_hidden_unless_requested(tx_client):
    default = rows(tx_client, region_code=DONG, page_size=100)
    assert default["meta"]["total"] == 14 and not any(i["is_cancelled"] for i in default["data"])
    with_cancelled = rows(tx_client, region_code=DONG, include_cancelled="true", page_size=100)
    assert with_cancelled["meta"]["total"] == 15
    cancelled = [i for i in with_cancelled["data"] if i["is_cancelled"]]
    assert len(cancelled) == 1 and cancelled[0]["price"] == 99 * EOK


def test_default_order_is_newest_first(tx_client):
    dates = [i["contract_date"] for i in rows(tx_client, region_code=DONG, page_size=100)["data"]]
    assert dates == sorted(dates, reverse=True)


def test_price_sorting(tx_client):
    asc = [i["price"] for i in rows(tx_client, region_code=DONG, sort="price_asc", page_size=100)["data"]]
    desc = [i["price"] for i in rows(tx_client, region_code=DONG, sort="price_desc", page_size=100)["data"]]
    assert asc == sorted(asc) and desc == sorted(desc, reverse=True) and asc[0] == 1 * EOK and desc[0] == 9 * EOK


def test_price_sorting_uses_the_deposit_for_rentals(tx_client):
    deposits = [i["deposit"] for i in rows(tx_client, region_code=DONG, deal_type="jeonse", sort="price_asc")["data"]]
    assert deposits == [3 * EOK, 4 * EOK, 5 * EOK]


def test_pagination(tx_client):
    first = rows(tx_client, region_code=DONG, page_size=5)
    assert len(first["data"]) == 5 and first["meta"]["total"] == 14 and first["meta"]["total_pages"] == 3
    assert (first["meta"]["page"], first["meta"]["page_size"]) == (1, 5)
    third = rows(tx_client, region_code=DONG, page_size=5, page=3)
    assert len(third["data"]) == 4
    second = rows(tx_client, region_code=DONG, page_size=5, page=2)
    ids = [i["id"] for p in (first, second, third) for i in p["data"]]
    assert len(set(ids)) == 14


@pytest.mark.parametrize("params", [{"page_size": 101}, {"page_size": 0}, {"page": 0}, {"sort": "random"}])
def test_invalid_paging_and_sort(tx_client, params):
    r = listing(tx_client, region_code=DONG, **params)
    assert r.status_code == 400 and r.json()["error"]["field"] == next(iter(params))


def test_filters_work_like_the_map(tx_client):
    assert rows(tx_client, region_code=DONG, exclude_direct="true")["meta"]["total"] == 13
    assert rows(tx_client, region_code=DONG, period_months=1)["meta"]["total"] == 9
    assert rows(tx_client, region_code=DONG, price_min=6 * EOK)["meta"]["total"] == 5
    assert rows(tx_client, region_code=DONG, price_max=4 * EOK)["meta"]["total"] == 8
    assert rows(tx_client, region_code=DONG, exclusive_area_pyeong_max=20)["meta"]["total"] == 3
    assert rows(tx_client, region_code=DONG, deal_type="monthly")["meta"]["total"] == 3


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"property_type": None}, "property_type"),
        ({"property_type": "land", "deal_type": "jeonse"}, "deal_type"),
        ({"deal_type": "jeonse", "price_min": 1}, "price_min"),
        ({"price_min": 9, "price_max": 1}, "price_min"),
        ({"period_months": 61}, "period_months"),
    ],
)
def test_filter_validation_matches_the_map(tx_client, params, field):
    query = {k: v for k, v in {"region_code": DONG, "property_type": "apartment", **params}.items() if v is not None}
    r = tx_client.get(f"{API}/transactions", params=query)
    assert r.status_code == 400 and r.json()["error"]["field"] == field


def test_meta_carries_the_data_date(tx_client):
    meta = rows(tx_client, region_code=DONG)["meta"]
    assert meta["data_as_of"] == FIXED_NOW.isoformat() and meta["reporting_lag_notice"] is True


# ---------- 단건 ----------


def test_single_transaction(tx_client):
    item = rows(tx_client, region_code=DONG)["data"][0]
    r = tx_client.get(f"{API}/transactions/{item['id']}")
    assert r.status_code == 200
    assert r.json()["data"] == item
    assert r.json()["meta"]["data_as_of"] == FIXED_NOW.isoformat()


def test_single_transaction_can_be_a_cancelled_one(tx_client):
    cancelled = next(
        i
        for i in rows(tx_client, region_code=DONG, include_cancelled="true", page_size=100)["data"]
        if i["is_cancelled"]
    )
    body = tx_client.get(f"{API}/transactions/{cancelled['id']}").json()["data"]
    assert body["is_cancelled"] is True and body["price"] == 99 * EOK


def test_unknown_and_malformed_transaction_ids(tx_client):
    missing = tx_client.get(f"{API}/transactions/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    bad = tx_client.get(f"{API}/transactions/abc")
    assert bad.status_code == 400 and bad.json()["error"]["field"] == "transaction_id"


# ---------- 샘플 데이터와 다른 API의 일관성 ----------


def test_list_total_equals_the_map_aggregate_for_the_same_filters(client):
    for deal_type in ("sale", "jeonse", "monthly"):
        markers = client.get(
            f"{API}/map/markers",
            params={
                "bbox": BUSAN,
                "zoom": 11,
                "property_type": "apartment",
                "deal_type": deal_type,
                "period_months": 12,
            },
        ).json()["data"]["markers"]
        haeundae = next(m for m in markers if m["region_code"] == "26350")
        total = client.get(
            f"{API}/transactions",
            params={"region_code": "26350", "property_type": "apartment", "deal_type": deal_type, "page_size": 1},
        ).json()["meta"]["total"]
        assert total == haeundae["transaction_count"], deal_type


def test_list_includes_land_rows_at_dong_precision(client):
    body = client.get(
        f"{API}/transactions", params={"region_code": "2635010500", "property_type": "land", "period_months": 60}
    ).json()
    assert body["data"] and all(
        i["location_precision"] in {"parcel", "dong"} and i["complex_id"] is None for i in body["data"]
    )


def test_transaction_list_is_cacheable(client):
    params = {"region_code": "26350", "property_type": "apartment"}
    first = client.get(f"{API}/transactions", params=params)
    again = client.get(f"{API}/transactions", params=params, headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304

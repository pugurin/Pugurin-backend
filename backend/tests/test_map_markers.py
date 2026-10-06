import re
from uuid import UUID

import pytest

from app.core.config import Settings
from tests.conftest import API, BUSAN, DATA_AS_OF, HAEUNDAE, make_client


def get_markers(client, **params):
    params.setdefault("bbox", BUSAN)
    params.setdefault("zoom", 11)
    params.setdefault("property_type", "apartment")
    return client.get(f"{API}/map/markers", params=params)


def total_count(client, **params) -> int:
    r = get_markers(client, **params)
    assert r.status_code == 200, r.text
    return sum(m["transaction_count"] for m in r.json()["data"]["markers"])


def test_sigungu_level_returns_district_aggregates(client):
    body = get_markers(client).json()
    assert body["data"]["level"] == "sigungu"
    markers = body["data"]["markers"]
    assert 12 <= len(markers) <= 16
    for m in markers:
        assert m["kind"] == "region" and len(m["region_code"]) == 5
        assert m["transaction_count"] > 0
        assert set(m["summary"]) == {"median_price_per_pyeong"}
    assert body["meta"] == {"data_as_of": DATA_AS_OF, "reporting_lag_notice": True}


def test_expensive_district_has_higher_median_than_cheap_one(client):
    by_name = {m["name"]: m for m in get_markers(client).json()["data"]["markers"]}
    assert (
        by_name["해운대구"]["summary"]["median_price_per_pyeong"]
        > by_name["북구"]["summary"]["median_price_per_pyeong"]
    )


@pytest.mark.parametrize(
    ("zoom", "level"),
    [(0, "sigungu"), (11, "sigungu"), (12, "dong"), (13, "dong"), (14, "complex"), (22, "complex")],
)
def test_zoom_decides_level(client, zoom, level):
    assert get_markers(client, bbox=HAEUNDAE, zoom=zoom).json()["data"]["level"] == level


def test_dong_level_markers(client):
    markers = get_markers(client, bbox=HAEUNDAE, zoom=13).json()["data"]["markers"]
    assert markers
    for m in markers:
        assert m["kind"] == "region" and len(m["region_code"]) == 10
        assert m["name"].endswith(("동", "읍"))


def test_markers_are_inside_requested_bbox(client):
    for m in get_markers(client, bbox=HAEUNDAE, zoom=15).json()["data"]["markers"]:
        assert 129.10 <= m["lng"] <= 129.22 and 35.14 <= m["lat"] <= 35.22


def test_complex_marker_for_sale(client):
    markers = get_markers(client, bbox=HAEUNDAE, zoom=15).json()["data"]["markers"]
    assert markers
    for m in markers:
        assert m["kind"] == "complex"
        UUID(m["complex_id"])
        assert m["transaction_count"] >= 1
        latest = m["latest"]
        assert latest["deal_type"] == "sale"
        assert latest["price"] > 0 and latest["deposit"] is None and latest["monthly_rent"] is None
        assert latest["exclusive_area_pyeong"] > 0
        assert latest["supply_area_pyeong"] is not None
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", latest["contract_date"])


def test_complex_marker_for_jeonse_and_monthly(client):
    jeonse = get_markers(client, bbox=HAEUNDAE, zoom=15, deal_type="jeonse").json()["data"]["markers"]
    assert jeonse and all(
        m["latest"]["price"] is None and m["latest"]["deposit"] > 0 and m["latest"]["monthly_rent"] is None
        for m in jeonse
    )
    monthly = get_markers(client, bbox=HAEUNDAE, zoom=15, deal_type="monthly").json()["data"]["markers"]
    assert monthly and all(
        m["latest"]["price"] is None and m["latest"]["deposit"] > 0 and m["latest"]["monthly_rent"] > 0 for m in monthly
    )


def test_region_summary_shape_follows_deal_type(client):
    jeonse = get_markers(client, deal_type="jeonse").json()["data"]["markers"]
    assert all(set(m["summary"]) == {"median_deposit_per_pyeong"} for m in jeonse)
    monthly = get_markers(client, deal_type="monthly").json()["data"]["markers"]
    assert all(set(m["summary"]) == {"median_deposit", "median_monthly_rent"} for m in monthly)


def test_villa_has_no_supply_area(client):
    markers = get_markers(client, bbox=HAEUNDAE, zoom=15, property_type="villa").json()["data"]["markers"]
    assert markers and all(m["latest"]["supply_area_pyeong"] is None for m in markers)


def test_shorter_period_has_fewer_transactions(client):
    one, twelve, sixty = (total_count(client, period_months=n) for n in (1, 12, 60))
    assert 0 < one < twelve < sixty


def test_exclude_direct_reduces_count(client):
    assert total_count(client, exclude_direct="true") < total_count(client)


def test_price_filter_applies_to_latest(client):
    unfiltered = get_markers(client, bbox=HAEUNDAE, zoom=15).json()["data"]["markers"]
    filtered = get_markers(client, bbox=HAEUNDAE, zoom=15, price_max=500_000_000).json()["data"]["markers"]
    assert len(filtered) < len(unfiltered)
    assert all(m["latest"]["price"] <= 500_000_000 for m in filtered)


def test_exclusive_area_filter_applies_to_latest(client):
    markers = get_markers(client, bbox=HAEUNDAE, zoom=15, exclusive_area_pyeong_min=30).json()["data"]["markers"]
    assert markers and all(m["latest"]["exclusive_area_pyeong"] >= 29.95 for m in markers)


def test_land_at_complex_zoom_has_dong_regions_and_parcels(client):
    body = get_markers(client, bbox=HAEUNDAE, zoom=15, property_type="land").json()
    assert body["data"]["level"] == "complex"
    markers = body["data"]["markers"]
    regions = [m for m in markers if m["kind"] == "region"]
    parcels = [m for m in markers if m["kind"] == "parcel"]
    assert regions and parcels
    assert all(len(m["region_code"]) == 10 and set(m["summary"]) == {"median_price_per_pyeong"} for m in regions)
    for p in parcels:
        assert re.fullmatch(r"26\d{17}", p["pnu"])
        UUID(p["transaction_id"])
        assert set(p["latest"]) == {"deal_type", "price", "land_area_pyeong", "contract_date"}


def test_land_parcel_markers_exclude_masked_trades(client):
    regions_only = sum(
        m["transaction_count"]
        for m in get_markers(client, bbox=HAEUNDAE, zoom=15, property_type="land").json()["data"]["markers"]
        if m["kind"] == "region"
    )
    parcels = [
        m
        for m in get_markers(client, bbox=HAEUNDAE, zoom=15, property_type="land").json()["data"]["markers"]
        if m["kind"] == "parcel"
    ]
    assert len(parcels) < regions_only


def test_land_at_sigungu_zoom(client):
    body = get_markers(client, property_type="land", zoom=11).json()
    assert body["data"]["level"] == "sigungu"
    assert all(m["kind"] == "region" for m in body["data"]["markers"])


def test_outside_busan_returns_empty_list(client):
    r = get_markers(client, bbox="127.0,37.4,127.1,37.6", zoom=15)
    assert r.status_code == 200
    assert r.json()["data"]["markers"] == []


def test_marker_cap_falls_back_to_coarser_level():
    with make_client(settings=Settings(max_markers=3)) as c:
        body = get_markers(c, bbox=HAEUNDAE, zoom=15).json()
    assert body["data"]["level"] == "sigungu"
    assert 0 < len(body["data"]["markers"]) <= 3


def test_markers_response_is_cacheable(client):
    first = get_markers(client)
    second = client.get(
        f"{API}/map/markers",
        params={"bbox": BUSAN, "zoom": 11, "property_type": "apartment"},
        headers={"If-None-Match": first.headers["etag"]},
    )
    assert second.status_code == 304


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"property_type": "land", "deal_type": "jeonse"}, "deal_type"),
        ({"deal_type": "jeonse", "price_min": 1}, "price_min"),
        ({"deal_type": "sale", "deposit_max": 1}, "deposit_max"),
        ({"deal_type": "jeonse", "rent_min": 1}, "rent_min"),
        ({"property_type": "apartment", "land_area_pyeong_min": 1}, "land_area_pyeong_min"),
        ({"property_type": "land", "exclusive_area_pyeong_max": 10}, "exclusive_area_pyeong_max"),
        ({"price_min": 9, "price_max": 1}, "price_min"),
        ({"bbox": "1,2,3"}, "bbox"),
        ({"bbox": "129.2,35.1,129.1,35.2"}, "bbox"),
        ({"zoom": 23}, "zoom"),
        ({"zoom": -1}, "zoom"),
        ({"zoom": "abc"}, "zoom"),
        ({"zoom": "11.5"}, "zoom"),
        ({"property_type": "house"}, "property_type"),
        ({"deal_type": "lease"}, "deal_type"),
        ({"period_months": 61}, "period_months"),
        ({"period_months": 0}, "period_months"),
    ],
)
def test_invalid_query_is_400_with_field(client, params, field):
    r = get_markers(client, **params)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert r.json()["error"]["field"] == field


@pytest.mark.parametrize("missing", ["bbox", "zoom", "property_type"])
def test_required_query_params(client, missing):
    params = {"bbox": BUSAN, "zoom": 11, "property_type": "apartment"}
    del params[missing]
    r = client.get(f"{API}/map/markers", params=params)
    assert r.status_code == 400
    assert r.json()["error"]["field"] == missing

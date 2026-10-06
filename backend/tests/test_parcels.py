from datetime import datetime
from uuid import UUID

import pytest

from app.core.dates import KST
from app.repositories.ports import SourceUnavailable
from tests.conftest import API, make_client

U_DONG = (35.1640, 129.1610)
SEOUL = (37.5665, 126.9780)


def lookup(client, lat, lng):
    return client.get(f"{API}/parcels/lookup", params={"lat": lat, "lng": lng})


def find_parcel_point(client):
    lat, lng = U_DONG
    for k in range(30):
        r = lookup(client, lat, lng + k * 0.00025)
        if r.json()["data"] is not None:
            return lat, lng + k * 0.00025, r.json()
    raise AssertionError("필지를 찾지 못했습니다")


def find_road_point(client):
    lat, lng = U_DONG
    for k in range(60):
        r = lookup(client, lat, lng + k * 0.00025)
        if r.json()["meta"]["unavailable_reason"] == "no_parcel":
            return lat, lng + k * 0.00025
    raise AssertionError("도로 격자를 찾지 못했습니다")


def test_lookup_returns_full_parcel(client):
    _, _, body = find_parcel_point(client)
    data = body["data"]
    assert set(data) == {
        "pnu",
        "jibun_address",
        "land_category",
        "land_area_m2",
        "land_area_pyeong",
        "official_land_price_per_m2",
        "official_land_price_year",
        "road_side",
        "shape",
        "geometry",
        "zonings",
        "ratio_source",
        "restrictions",
        "glossary",
    }
    assert len(data["pnu"]) == 19 and data["pnu"].startswith("26")
    assert data["land_area_pyeong"] == round(data["land_area_m2"] / 3.3058, 1)
    assert data["official_land_price_per_m2"] > 0 and data["official_land_price_year"] == 2026
    assert body["meta"]["unavailable_reason"] is None and body["meta"]["cached_at"]


def test_geometry_is_closed_lng_lat_polygon(client):
    _, _, body = find_parcel_point(client)
    geometry = body["data"]["geometry"]
    assert geometry["type"] == "Polygon"
    ring = geometry["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) == 5
    assert all(128 < lng < 130 and 34 < lat < 36 for lng, lat in ring)


def test_zonings_are_sorted_and_sum_to_one(client):
    seen_multi = False
    lat, lng = U_DONG
    for k in range(120):
        data = lookup(client, lat, lng + k * 0.00025).json()["data"]
        if data is None:
            continue
        zonings = data["zonings"]
        ratios = [z["area_ratio"] for z in zonings]
        assert ratios == sorted(ratios, reverse=True)
        assert sum(ratios) == pytest.approx(1.0)
        for z in zonings:
            assert set(z) == {
                "zone_type",
                "area_ratio",
                "inclusion",
                "max_building_coverage_ratio",
                "max_floor_area_ratio",
            }
            assert z["inclusion"] in {"포함", "저촉", "접합"}
            assert isinstance(z["max_building_coverage_ratio"], int) and isinstance(z["max_floor_area_ratio"], int)
        seen_multi |= len(zonings) > 1
    assert seen_multi, "여러 용도지역에 걸친 필지 샘플이 있어야 합니다"


def test_ratio_source_discloses_sample_values(client):
    _, _, body = find_parcel_point(client)
    assert "샘플" in body["data"]["ratio_source"]


def test_glossary_map_points_to_existing_terms(client):
    _, _, body = find_parcel_point(client)
    mapping = body["data"]["glossary"]
    assert set(mapping) == {
        "land_category",
        "official_land_price_per_m2",
        "road_side",
        "zone_type",
        "max_building_coverage_ratio",
        "max_floor_area_ratio",
    }
    for term_id in mapping.values():
        r = client.get(f"{API}/glossary/{term_id}")
        assert r.status_code == 200 and r.json()["data"]["id"] == term_id


def test_restrictions_shape_and_glossary_links(client):
    lat, lng = U_DONG
    linked = False
    for k in range(60):
        data = lookup(client, lat, lng + k * 0.00025).json()["data"]
        for r in (data or {}).get("restrictions", []):
            assert set(r) == {"name", "plain_explanation", "glossary_term_id"} and r["plain_explanation"]
            if r["glossary_term_id"]:
                UUID(r["glossary_term_id"])
                linked = True
    assert linked


def test_get_by_pnu_matches_lookup(client):
    _, _, body = find_parcel_point(client)
    by_pnu = client.get(f"{API}/parcels/{body['data']['pnu']}")
    assert by_pnu.status_code == 200
    assert by_pnu.json()["data"] == body["data"]


def test_outside_busan_is_200_null_with_reason(client):
    r = lookup(client, *SEOUL)
    assert r.status_code == 200
    assert r.json() == {"data": None, "meta": {"cached_at": None, "unavailable_reason": "out_of_service_area"}}


def test_road_is_200_null_no_parcel(client):
    lat, lng = find_road_point(client)
    r = lookup(client, lat, lng)
    assert r.status_code == 200
    assert r.json()["data"] is None and r.json()["meta"]["unavailable_reason"] == "no_parcel"


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"lat": 95, "lng": 129.1}, "lat"),
        ({"lat": 35.1, "lng": 200}, "lng"),
        ({"lat": "x", "lng": 129.1}, "lat"),
        ({"lat": 35.1}, "lng"),
        ({"lng": 129.1}, "lat"),
    ],
)
def test_lookup_invalid_coordinates(client, params, field):
    r = client.get(f"{API}/parcels/lookup", params=params)
    assert r.status_code == 400
    assert r.json()["error"]["field"] == field


@pytest.mark.parametrize("pnu", ["123", "abcdefghijklmnopqrs", "26" + "0" * 18])
def test_pnu_must_be_exactly_19_digits(client, pnu):
    r = client.get(f"{API}/parcels/{pnu}")
    assert r.status_code == 400 and r.json()["error"]["field"] == "pnu"


def test_well_formed_unknown_pnu_is_not_an_error(client):
    nowhere = client.get(f"{API}/parcels/{'26' + '0' * 17}")
    assert nowhere.status_code == 200 and nowhere.json()["meta"]["unavailable_reason"] == "no_parcel"
    elsewhere = client.get(f"{API}/parcels/{'11' + '0' * 17}")
    assert elsewhere.json()["meta"]["unavailable_reason"] == "out_of_service_area"


class SpyParcelSource:
    def __init__(self, inner, *, fail=False):
        self.inner, self.fail = inner, fail
        self.resolve_calls = self.fetch_calls = 0

    async def resolve_pnu(self, lat, lng):
        self.resolve_calls += 1
        if self.fail:
            raise SourceUnavailable
        return await self.inner.resolve_pnu(lat, lng)

    async def fetch_parcel(self, pnu):
        self.fetch_calls += 1
        if self.fail:
            raise SourceUnavailable
        return await self.inner.fetch_parcel(pnu)


def spy_client(**kwargs):
    from app.adapters.mock.parcel_source import MockParcelSource

    spy = SpyParcelSource(MockParcelSource(2026), **kwargs)
    return make_client(parcel_source=spy), spy


def test_parcel_is_cached_by_pnu(client):
    c, spy = spy_client()
    with c:
        lat, lng, first = find_parcel_point(c)
        fetches_after_first = spy.fetch_calls
        again = lookup(c, lat, lng).json()
        by_pnu = c.get(f"{API}/parcels/{first['data']['pnu']}").json()
    assert spy.fetch_calls == fetches_after_first
    assert again["meta"]["cached_at"] == first["meta"]["cached_at"] == by_pnu["meta"]["cached_at"]


def test_no_parcel_is_negative_cached():
    c, spy = spy_client()
    with c:
        lat, lng = find_road_point(c)
        before = spy.resolve_calls
        lookup(c, lat, lng)
        lookup(c, lat, lng)
    assert spy.resolve_calls == before


def test_source_failure_degrades_to_200_and_is_not_cached():
    c, spy = spy_client(fail=True)
    with c:
        r = lookup(c, *U_DONG)
        assert r.status_code == 200
        assert r.json() == {"data": None, "meta": {"cached_at": None, "unavailable_reason": "source_unavailable"}}
        assert "no-store" in r.headers["cache-control"]
        lookup(c, *U_DONG)
    assert spy.resolve_calls == 2


def test_cache_entries_expire_after_ttl():
    from datetime import timedelta

    from app.core.config import Settings

    now = [datetime(2026, 10, 6, 12, 0, tzinfo=KST)]
    from fastapi.testclient import TestClient

    from app.adapters.mock.parcel_source import MockParcelSource
    from app.main import create_app

    spy = SpyParcelSource(MockParcelSource(2026))
    app = create_app(settings=Settings(), clock=lambda: now[0], parcel_source=spy)
    with TestClient(app, headers={"X-Device-Id": "2f8f1d52-7b0e-4c55-9c0e-3f6f9d1c2a11"}) as c:
        lat, lng, _ = find_parcel_point(c)
        fetches = spy.fetch_calls
        now[0] += timedelta(days=29)
        lookup(c, lat, lng)
        assert spy.fetch_calls == fetches
        now[0] += timedelta(days=2)
        lookup(c, lat, lng)
        assert spy.fetch_calls == fetches + 1

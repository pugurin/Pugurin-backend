from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.adapters.unconfigured import UnconfiguredParcelSource
from app.main import create_app
from app.repositories.memory.dataset import MarketDataset
from app.repositories.memory.market import InMemoryMarketRepository
from app.repositories.types import (
    DealType,
    ParcelData,
    PropertyType,
    RestrictionInfo,
    StatsMetrics,
    StatsResult,
    StatsSeries,
    ZoningPart,
)
from app.services.analysis_service import _compare
from tests.conftest import API, FIXED_NOW
from tests.test_stats import COMPLEX, DONG, HEADERS, OTHER_COMPLEX, REGIONS, TRANSACTIONS

PNU = "2635010500100010000"
COMPLEX_WITH_PNU = replace(COMPLEX, pnu=PNU)
FORBIDDEN_WORDS = ("오를", "내릴", "사세요", "추천", "투자")


class StubParcelSource:
    def __init__(self, zonings: tuple[ZoningPart, ...]):
        self._zonings = zonings

    async def resolve_pnu(self, lat: float, lng: float) -> str | None:
        return PNU

    async def fetch_parcel(self, pnu: str) -> ParcelData | None:
        return ParcelData(
            pnu=pnu,
            jibun_address="부산 해운대구 우동 1",
            land_category="대",
            land_area_m2=1000.0,
            official_land_price_per_m2=5_000_000,
            official_land_price_year=2026,
            road_side="중로한면",
            shape="가로장방형",
            geometry={"type": "Polygon", "coordinates": []},
            zonings=self._zonings,
            restrictions=(RestrictionInfo("가축사육제한구역", "가축 시설을 지을 수 없어요."),),
        )


def make(complexes=(COMPLEX, OTHER_COMPLEX), parcel_source=None) -> TestClient:
    dataset = MarketDataset(tuple(complexes), tuple(TRANSACTIONS))
    repo = InMemoryMarketRepository(FIXED_NOW, dataset, REGIONS)
    app = create_app(clock=lambda: FIXED_NOW, market_repo=repo, parcel_source=parcel_source)
    return TestClient(app, headers=HEADERS)


@pytest.fixture(scope="module")
def client():
    with make() as c:
        yield c


def analysis(client, complex_id=COMPLEX.id, **params):
    return client.get(f"{API}/analysis/complexes/{complex_id}", params=params)


def test_complex_and_its_dong_use_the_same_numbers_as_the_stats_api(client):
    body = analysis(client, period_months=6).json()["data"]
    complex_stats = client.get(
        f"{API}/complexes/{COMPLEX.id}/stats", params={"property_type": "apartment", "period_months": 6}
    ).json()["data"]
    region_stats = client.get(
        f"{API}/stats/regions/{DONG}", params={"property_type": "apartment", "period_months": 6}
    ).json()["data"]

    assert body["complex_stats"]["transaction_count"] == complex_stats["transaction_count"]
    assert body["complex_stats"]["median_price_per_pyeong"] == complex_stats["median_price_per_pyeong"]
    assert body["complex_stats"]["trend"] == complex_stats["trend"]
    assert body["region_stats"]["transaction_count"] == region_stats["transaction_count"]
    assert body["region_stats"]["median_price_per_pyeong"] == region_stats["median_price_per_pyeong"]
    assert body["region_stats"]["trend"] == region_stats["trend"]


def test_names_the_complex_and_the_dong_it_is_compared_with(client):
    body = analysis(client).json()["data"]
    assert body["complex"] == {
        "id": str(COMPLEX.id),
        "name": "통계단지",
        "property_type": "apartment",
        "region_code": DONG,
    }
    assert body["region"] == {"region_code": DONG, "name": "해운대구 우동"}


def test_comparison_is_the_ratio_of_the_two_medians(client):
    body = analysis(client, period_months=6).json()["data"]
    mine = body["complex_stats"]["median_price_per_pyeong"]
    dong = body["region_stats"]["median_price_per_pyeong"]
    c = body["comparison"]
    assert c["metric"] == "median_price_per_pyeong"
    assert c["complex_value"] == mine and c["region_value"] == dong
    assert c["difference_percent"] == round((mine / dong - 1) * 100, 1)


def test_summary_states_the_difference_without_advice(client):
    c = analysis(client, period_months=6).json()["data"]["comparison"]
    assert "우동 아파트" in c["summary"]
    assert f"{abs(round(c['difference_percent']))}%" in c["summary"] or "비슷" in c["summary"]
    assert not any(word in c["summary"] for word in FORBIDDEN_WORDS)


def test_jeonse_compares_deposit_per_pyeong(client):
    c = analysis(client, deal_type="jeonse").json()["data"]["comparison"]
    assert c["metric"] == "median_deposit_per_pyeong"
    assert "전세" in c["summary"]


def test_monthly_rent_has_trends_but_no_single_comparison(client):
    body = analysis(client, deal_type="monthly").json()["data"]
    assert body["comparison"] is None
    assert body["complex_stats"]["transaction_count"] == 3
    assert "median_monthly_rent" in body["complex_stats"]


def test_excluding_direct_deals_applies_to_both_sides(client):
    all_deals = analysis(client, period_months=6).json()["data"]
    broker_only = analysis(client, period_months=6, exclude_direct=True).json()["data"]
    assert broker_only["complex_stats"]["transaction_count"] == all_deals["complex_stats"]["transaction_count"] - 1
    assert broker_only["region_stats"]["transaction_count"] == all_deals["region_stats"]["transaction_count"] - 1


def test_comparison_is_null_when_a_side_has_no_trades(client):
    body = analysis(client, deal_type="jeonse", period_months=1).json()["data"]
    assert body["complex_stats"]["transaction_count"] == 0
    assert body["comparison"] is None


def test_disclaimer_and_method_are_always_present(client):
    res = analysis(client).json()
    assert "참고용" in res["data"]["disclaimer"]
    assert res["meta"]["method"].startswith("중위값")
    assert res["meta"]["data_as_of"]


def test_complex_without_a_parcel_number_has_no_zoning(client):
    res = analysis(client).json()
    assert res["data"]["zonings"] is None
    assert res["meta"]["zoning_unavailable_reason"] == "no_pnu"


def test_zoning_summary_comes_from_the_parcel_with_ordinance_limits():
    source = StubParcelSource((ZoningPart("일반상업지역", 0.2, "저촉"), ZoningPart("제2종일반주거지역", 0.8, "포함")))
    with make(complexes=(COMPLEX_WITH_PNU, OTHER_COMPLEX), parcel_source=source) as c:
        res = analysis(c).json()
    assert res["meta"]["zoning_unavailable_reason"] is None
    zonings = res["data"]["zonings"]
    assert [z["zone_type"] for z in zonings] == ["제2종일반주거지역", "일반상업지역"]
    assert zonings[0] == {
        "zone_type": "제2종일반주거지역",
        "area_ratio": 0.8,
        "inclusion": "포함",
        "max_building_coverage_ratio": 60,
        "max_floor_area_ratio": 250,
    }


def test_zoning_is_null_when_no_land_source_is_configured():
    with make(complexes=(COMPLEX_WITH_PNU, OTHER_COMPLEX), parcel_source=UnconfiguredParcelSource()) as c:
        res = analysis(c)
    assert res.status_code == 200
    assert res.json()["data"]["zonings"] is None
    assert res.json()["meta"]["zoning_unavailable_reason"] == "source_unavailable"
    assert res.json()["data"]["comparison"] is not None


def test_unknown_complex_is_404(client):
    res = analysis(client, complex_id=uuid4())
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_period_is_validated(client):
    assert analysis(client, period_months=0).status_code == 400
    assert analysis(client, period_months=61).status_code == 400


def _result(value: int | None) -> StatsResult:
    return StatsResult(StatsSeries(count=3, metrics=StatsMetrics(median_price_per_pyeong=value), trend=()))


@pytest.mark.parametrize(
    ("mine", "dong", "expected"),
    [
        (2_400_000, 2_000_000, "이 단지의 평당 매매가 중위값은 우동 아파트 전체보다 20% 높아요."),
        (1_700_000, 2_000_000, "이 단지의 평당 매매가 중위값은 우동 아파트 전체보다 15% 낮아요."),
        (2_010_000, 2_000_000, "이 단지의 평당 매매가 중위값은 우동 아파트 전체와 비슷해요."),
    ],
)
def test_summary_wording(mine, dong, expected):
    c = _compare(DealType.sale, PropertyType.apartment, "우동", _result(mine), _result(dong))
    assert c is not None and c.summary == expected
    assert c.difference_percent == round((mine / dong - 1) * 100, 1)


def test_no_comparison_without_a_median():
    assert _compare(DealType.sale, PropertyType.apartment, "우동", _result(None), _result(2_000_000)) is None

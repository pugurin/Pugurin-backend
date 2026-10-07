import statistics
from datetime import date
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.memory.dataset import MarketDataset
from app.repositories.memory.market import InMemoryMarketRepository
from app.repositories.types import (
    AreaType,
    Complex,
    DealType,
    LocationPrecision,
    PropertyType,
    RegionRef,
    TradeMethod,
    Transaction,
)
from tests.conftest import API, FIXED_NOW

HEADERS = {"X-Device-Id": "2f8f1d52-7b0e-4c55-9c0e-3f6f9d1c2a11"}
BIG, SMALL = 84.97, 59.97
PYEONG = 3.3058
DONG, OTHER_DONG, SIGUNGU = "2635010500", "2635010600", "26350"

COMPLEX = Complex(
    id=uuid5(NAMESPACE_URL, "stats-complex"),
    property_type=PropertyType.apartment,
    name="통계단지",
    address="부산 해운대구 우동 1",
    region_code=DONG,
    pnu=None,
    lat=35.16,
    lng=129.16,
    build_year=2010,
    household_count=None,
    area_types=(AreaType(SMALL, None), AreaType(BIG, None)),
)
OTHER_COMPLEX = Complex(
    id=uuid5(NAMESPACE_URL, "stats-other"),
    property_type=PropertyType.apartment,
    name="다른단지",
    address="부산 해운대구 중동 1",
    region_code=OTHER_DONG,
    pnu=None,
    lat=35.17,
    lng=129.17,
    build_year=2011,
    household_count=None,
    area_types=(AreaType(BIG, None),),
)
REGIONS = {
    SIGUNGU: RegionRef(SIGUNGU, "해운대구", 35.16, 129.16),
    DONG: RegionRef(DONG, "우동", 35.16, 129.16),
    OTHER_DONG: RegionRef(OTHER_DONG, "중동", 35.17, 129.17),
}
_counter = iter(range(10_000))


def tx(
    month: int,
    *,
    complex_=COMPLEX,
    price=None,
    deposit=None,
    rent=None,
    area=BIG,
    method=TradeMethod.broker,
    cancelled=False,
    deal=DealType.sale,
    year=2026,
    day=10,
) -> Transaction:
    return Transaction(
        id=uuid5(NAMESPACE_URL, f"tx-{next(_counter)}"),
        property_type=complex_.property_type,
        deal_type=deal,
        complex_id=complex_.id,
        address=complex_.address,
        region_code=complex_.region_code,
        jibun=None,
        pnu=None,
        location_precision=LocationPrecision.parcel,
        lat=complex_.lat,
        lng=complex_.lng,
        price=price,
        deposit=deposit,
        monthly_rent=rent,
        exclusive_area_m2=area,
        land_area_m2=None,
        supply_area_pyeong=None,
        floor=3,
        contract_date=date(year, month, day),
        build_year=2010,
        trade_method=method,
        is_cancelled=cancelled,
        cancelled_at=None,
    )


def per_pyeong(price: int, area: float = BIG) -> int:
    return int(round(price / (area / PYEONG)))


EOK = 100_000_000
TRANSACTIONS = [
    # 9월: 3건 (4,5,6억) → 중위값 5억
    tx(9, price=4 * EOK),
    tx(9, price=5 * EOK),
    tx(9, price=6 * EOK),
    # 8월: 2건 → 표본 부족(null)
    tx(8, price=7 * EOK),
    tx(8, price=9 * EOK),
    # 7월: 3건 + 해제 1건(제외)
    tx(7, price=3 * EOK),
    tx(7, price=3 * EOK),
    tx(7, price=3 * EOK),
    tx(7, price=99 * EOK, cancelled=True),
    # 10월: 직거래 1건 포함 3건
    tx(10, price=8 * EOK),
    tx(10, price=8 * EOK),
    tx(10, price=1 * EOK, method=TradeMethod.direct),
    # 작은 평형
    tx(9, price=3 * EOK, area=SMALL),
    tx(9, price=3 * EOK, area=SMALL),
    tx(9, price=3 * EOK, area=SMALL),
    # 다른 단지(다른 동)
    tx(9, complex_=OTHER_COMPLEX, price=10 * EOK),
    tx(9, complex_=OTHER_COMPLEX, price=10 * EOK),
    tx(9, complex_=OTHER_COMPLEX, price=10 * EOK),
    # 기간 밖(작년 1월)
    tx(1, year=2025, price=50 * EOK),
    # 전세·월세
    tx(9, deal=DealType.jeonse, deposit=3 * EOK),
    tx(9, deal=DealType.jeonse, deposit=4 * EOK),
    tx(9, deal=DealType.jeonse, deposit=5 * EOK),
    tx(9, deal=DealType.monthly, deposit=50_000_000, rent=800_000),
    tx(9, deal=DealType.monthly, deposit=100_000_000, rent=1_000_000),
    tx(9, deal=DealType.monthly, deposit=30_000_000, rent=500_000),
]


@pytest.fixture(scope="module")
def stats_client():
    dataset = MarketDataset((COMPLEX, OTHER_COMPLEX), tuple(TRANSACTIONS))
    repo = InMemoryMarketRepository(FIXED_NOW, dataset, REGIONS)
    with TestClient(create_app(clock=lambda: FIXED_NOW, market_repo=repo), headers=HEADERS) as c:
        yield c


def region_stats(client, code=DONG, **params):
    params.setdefault("property_type", "apartment")
    return client.get(f"{API}/stats/regions/{code}", params=params)


def complex_stats(client, complex_id=COMPLEX.id, **params):
    params.setdefault("property_type", "apartment")
    return client.get(f"{API}/complexes/{complex_id}/stats", params=params)


def month_of(body, ym):
    return next(p for p in body["data"]["trend"] if p["month"] == ym)


def sep_values():
    return [per_pyeong(x * EOK) for x in (4, 5, 6)] + [per_pyeong(3 * EOK, SMALL)] * 3


def test_monthly_median_per_pyeong_and_counts(stats_client):
    body = region_stats(stats_client, period_months=6).json()
    sep = month_of(body, "2026-09")
    assert sep["count"] == 6 and sep["median_price_per_pyeong"] == int(round(statistics.median(sep_values())))
    jul = month_of(body, "2026-07")
    assert jul["count"] == 3 and jul["median_price_per_pyeong"] == per_pyeong(3 * EOK)


def test_months_with_fewer_than_three_samples_have_null_medians_but_keep_counts(stats_client):
    body = region_stats(stats_client, period_months=6).json()
    aug = month_of(body, "2026-08")
    assert aug["count"] == 2 and aug["median_price_per_pyeong"] is None
    empty = month_of(body, "2026-05")
    assert empty["count"] == 0 and empty["median_price_per_pyeong"] is None


def test_trend_has_one_entry_per_month_ending_at_the_data_month(stats_client):
    body = region_stats(stats_client, period_months=6).json()
    months = [p["month"] for p in body["data"]["trend"]]
    assert months == ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    assert len(region_stats(stats_client, period_months=36).json()["data"]["trend"]) == 36


def test_overall_median_covers_the_whole_window_even_for_thin_months(stats_client):
    body = region_stats(stats_client, period_months=6).json()["data"]
    values = [per_pyeong(x * EOK) for x in (3, 3, 3, 7, 9, 4, 5, 6, 8, 8, 1)] + [per_pyeong(3 * EOK, SMALL)] * 3
    assert body["transaction_count"] == 14
    assert body["median_price_per_pyeong"] == int(round(statistics.median(values)))


def test_cancelled_trades_never_count(stats_client):
    body = region_stats(stats_client, period_months=6).json()
    assert month_of(body, "2026-07")["count"] == 3  # 해제 1건 제외


def test_exclude_direct_drops_direct_trades(stats_client):
    with_direct = region_stats(stats_client, period_months=6).json()["data"]
    without = region_stats(stats_client, period_months=6, exclude_direct="true").json()["data"]
    assert (with_direct["transaction_count"], without["transaction_count"]) == (14, 13)
    assert month_of({"data": without}, "2026-10")["count"] == 2


def test_period_window_excludes_older_trades(stats_client):
    short = region_stats(stats_client, period_months=3).json()["data"]
    assert short["transaction_count"] == 2 + 6 + 3
    assert [p["month"] for p in short["trend"]] == ["2026-08", "2026-09", "2026-10"]
    twelve = region_stats(stats_client, period_months=12).json()["data"]["transaction_count"]
    assert region_stats(stats_client, period_months=60).json()["data"]["transaction_count"] == twelve + 1


def test_sigungu_code_aggregates_every_dong_below_it(stats_client):
    dong = region_stats(stats_client, DONG, period_months=6).json()["data"]["transaction_count"]
    other = region_stats(stats_client, OTHER_DONG, period_months=6).json()["data"]["transaction_count"]
    assert (dong, other) == (14, 3)
    assert region_stats(stats_client, SIGUNGU, period_months=6).json()["data"]["transaction_count"] == 17


def test_jeonse_and_monthly_use_their_own_metrics(stats_client):
    jeonse = region_stats(stats_client, period_months=6, deal_type="jeonse").json()["data"]
    assert "median_price_per_pyeong" not in jeonse
    assert jeonse["median_deposit_per_pyeong"] == per_pyeong(4 * EOK)
    sep = month_of({"data": jeonse}, "2026-09")
    assert sep["count"] == 3 and sep["median_deposit_per_pyeong"] == per_pyeong(4 * EOK)
    monthly = region_stats(stats_client, period_months=6, deal_type="monthly").json()["data"]
    assert monthly["median_deposit"] == 50_000_000 and monthly["median_monthly_rent"] == 800_000
    assert set(month_of({"data": monthly}, "2026-09")) == {"month", "count", "median_deposit", "median_monthly_rent"}


def test_meta_describes_the_method(stats_client):
    meta = region_stats(stats_client).json()["meta"]
    assert meta["data_as_of"] == FIXED_NOW.isoformat() and meta["reporting_lag_notice"] is True
    assert "중위값" in meta["method"] and "해제" in meta["method"]


def test_region_response_has_no_area_breakdown(stats_client):
    assert "area_types" not in region_stats(stats_client).json()["data"]


# ---------- 단지 ----------


def test_complex_stats_match_the_spec_shape_and_split_by_area_type(stats_client):
    data = complex_stats(stats_client, period_months=6).json()["data"]
    assert {"median_price_per_pyeong", "transaction_count", "trend", "area_types"} <= set(data)
    assert data["transaction_count"] == 14  # 다른 단지·다른 거래유형 제외, 해제 제외
    small, big = data["area_types"]
    assert (small["exclusive_area_m2"], big["exclusive_area_m2"]) == (SMALL, BIG)
    assert small["exclusive_area_pyeong"] == 18.1 and big["exclusive_area_pyeong"] == 25.7
    assert small["supply_area_pyeong"] is None
    assert small["transaction_count"] == 3 and big["transaction_count"] == 11
    assert small["median_price_per_pyeong"] == per_pyeong(3 * EOK, SMALL)
    assert month_of({"data": small}, "2026-09")["median_price_per_pyeong"] == per_pyeong(3 * EOK, SMALL)
    assert month_of({"data": big}, "2026-08")["median_price_per_pyeong"] is None


def test_complex_overall_series_has_the_same_months_as_the_period(stats_client):
    data = complex_stats(stats_client, period_months=6).json()["data"]
    assert len(data["trend"]) == 6 and all(len(a["trend"]) == 6 for a in data["area_types"])


def test_unknown_complex_is_404(stats_client):
    assert complex_stats(stats_client, "00000000-0000-0000-0000-000000000000").status_code == 404


def test_complex_property_type_must_match(stats_client):
    r = complex_stats(stats_client, property_type="villa")
    assert r.status_code == 400 and r.json()["error"]["field"] == "property_type"


# ---------- 검증 ----------


def test_unknown_region_is_404_and_malformed_code_is_400(stats_client):
    assert region_stats(stats_client, "2699099999").status_code == 404
    bad = region_stats(stats_client, "123")
    assert bad.status_code == 400 and bad.json()["error"]["field"] == "region_code"


def raw_get(client, kind, **params):
    path = f"/stats/regions/{DONG}" if kind == "region" else f"/complexes/{COMPLEX.id}/stats"
    return client.get(API + path, params=params)


@pytest.mark.parametrize("kind", ["region", "complex"])
@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({}, "property_type"),
        ({"property_type": "house"}, "property_type"),
        ({"property_type": "land", "deal_type": "jeonse"}, "deal_type"),
        ({"property_type": "apartment", "deal_type": "lease"}, "deal_type"),
        ({"property_type": "apartment", "period_months": 0}, "period_months"),
        ({"property_type": "apartment", "period_months": 61}, "period_months"),
    ],
)
def test_invalid_query_is_400_with_field(stats_client, kind, params, field):
    r = raw_get(stats_client, kind, **params)
    assert r.status_code == 400 and r.json()["error"]["field"] == field


def test_land_is_measured_by_land_area():
    txs = [
        Transaction(
            id=uuid5(NAMESPACE_URL, f"land-{n}"),
            property_type=PropertyType.land,
            deal_type=DealType.sale,
            complex_id=None,
            address="부산 해운대구 우동",
            region_code=DONG,
            jibun=None,
            pnu=None,
            location_precision=LocationPrecision.dong,
            lat=35.16,
            lng=129.16,
            price=price,
            deposit=None,
            monthly_rent=None,
            exclusive_area_m2=None,
            land_area_m2=330.58,
            supply_area_pyeong=None,
            floor=None,
            contract_date=date(2026, 9, 1),
            build_year=None,
            trade_method=TradeMethod.broker,
            is_cancelled=False,
            cancelled_at=None,
        )
        for n, price in enumerate((1_000_000_000, 2_000_000_000, 3_000_000_000))
    ]
    repo = InMemoryMarketRepository(FIXED_NOW, MarketDataset((), tuple(txs)), REGIONS)
    with TestClient(create_app(clock=lambda: FIXED_NOW, market_repo=repo), headers=HEADERS) as c:
        data = c.get(f"{API}/stats/regions/{DONG}", params={"property_type": "land", "period_months": 3}).json()["data"]
    assert data["median_price_per_pyeong"] == 20_000_000


# ---------- 샘플 데이터 ----------


def test_stats_work_on_the_sample_dataset(client):
    body = client.get(f"{API}/stats/regions/{SIGUNGU}", params={"property_type": "apartment"}).json()
    assert body["data"]["transaction_count"] > 0 and len(body["data"]["trend"]) == 36
    assert body["data"]["median_price_per_pyeong"] > 10_000_000
    found = client.get(f"{API}/search", params={"q": "해운대아이파크"}).json()["data"][0]
    complex_body = client.get(
        f"{API}/complexes/{found['complex_id']}/stats", params={"property_type": "apartment"}
    ).json()
    assert complex_body["data"]["area_types"] and complex_body["data"]["transaction_count"] > 0


def test_stats_are_cacheable(client):
    first = client.get(f"{API}/stats/regions/{SIGUNGU}", params={"property_type": "apartment"})
    again = client.get(
        f"{API}/stats/regions/{SIGUNGU}",
        params={"property_type": "apartment"},
        headers={"If-None-Match": first.headers["etag"]},
    )
    assert again.status_code == 304

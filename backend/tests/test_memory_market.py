import pytest

from app.core.dates import latest_etl_time
from app.core.geo import BBox
from app.repositories.memory.market import InMemoryMarketRepository
from app.repositories.types import DealType, Level, PropertyType, Range, TradeMethod, TransactionFilter
from tests.conftest import FIXED_NOW
from tests.fakes.app import sample_repo
from tests.fakes.market import build_sample_market
from tests.fakes.regions import sample_regions

pytestmark = pytest.mark.anyio

AS_OF = latest_etl_time(FIXED_NOW)
BUSAN = BBox(128.75, 34.85, 129.35, 35.40)


def flt(**kw):
    return TransactionFilter(
        property_type=kw.pop("property_type", PropertyType.apartment),
        deal_type=kw.pop("deal_type", DealType.sale),
        **kw,
    )


async def test_cancelled_transactions_are_never_aggregated():
    market = build_sample_market(AS_OF.date())
    repo = InMemoryMarketRepository(AS_OF, market, sample_regions())
    from app.core.dates import months_ago

    cutoff = months_ago(AS_OF.date(), 60)
    expected = sum(
        1
        for t in market.transactions
        if t.property_type == PropertyType.apartment
        and t.deal_type == DealType.sale
        and not t.is_cancelled
        and t.contract_date >= cutoff
    )
    assert any(t.is_cancelled for t in market.transactions)
    aggregates = await repo.aggregate_regions(Level.dong, BUSAN, flt(period_months=60))
    assert sum(a.transaction_count for a in aggregates) == expected


async def test_sigungu_and_dong_levels_cover_the_same_transactions():
    repo = sample_repo(AS_OF)
    by_gu = await repo.aggregate_regions(Level.sigungu, BUSAN, flt())
    by_dong = await repo.aggregate_regions(Level.dong, BUSAN, flt())
    assert sum(a.transaction_count for a in by_gu) == sum(a.transaction_count for a in by_dong)


async def test_median_resists_outliers():
    repo = sample_repo(AS_OF)
    [haeundae] = [a for a in await repo.aggregate_regions(Level.sigungu, BUSAN, flt()) if a.region.name == "해운대구"]
    assert 20_000_000 < haeundae.median_price_per_pyeong < 40_000_000


async def test_direct_trades_are_dropped_on_request():
    market = build_sample_market(AS_OF.date())
    assert any(t.trade_method == TradeMethod.direct for t in market.transactions)
    repo = InMemoryMarketRepository(AS_OF, market, sample_regions())
    rows = await repo.complex_markers(BUSAN, flt(exclude_direct=True))
    assert rows and all(r.latest.trade_method == TradeMethod.broker for r in rows)


async def test_sample_data_is_deterministic():
    a, b = build_sample_market(AS_OF.date()), build_sample_market(AS_OF.date())
    assert a is b
    other_day = build_sample_market(AS_OF.date().replace(day=1))
    assert [c.id for c in other_day.complexes] == [c.id for c in a.complexes]


async def test_parcel_marker_pnus_resolve_to_real_parcels():
    from tests.fakes.parcel_source import MockParcelSource

    repo = sample_repo(AS_OF)
    source = MockParcelSource(2026)
    rows = await repo.parcel_markers(BUSAN, flt(property_type=PropertyType.land, period_months=60))
    assert rows
    for row in rows[:25]:
        assert await source.fetch_parcel(row.latest.pnu) is not None


async def test_complex_pnus_resolve_to_real_parcels():
    from tests.fakes.parcel_source import MockParcelSource

    market = build_sample_market(AS_OF.date())
    source = MockParcelSource(2026)
    for c in market.complexes[:25]:
        assert await source.fetch_parcel(c.pnu) is not None


# ── 단지 마커의 대표 평형(typical) ──────────────────────────
# 합성 단지·거래로 평형 고르기 규칙과 평균 반올림을 본다


def _complex(n: int, *area_types):
    from uuid import UUID

    from app.repositories.types import Complex

    return Complex(
        id=UUID(f"00000000-0000-4000-8000-{n:012d}"),
        property_type=PropertyType.apartment,
        name=f"가상단지{n}",
        address="부산 해운대구 우동",
        region_code="2635010500",
        pnu=None,
        lat=35.166,
        lng=129.166,
        build_year=2010,
        household_count=None,
        area_types=tuple(area_types),
    )


def _tx(complex_, n: int, day: str, area: float, *, deal_type=DealType.sale, price=None, deposit=None, rent=None,
        cancelled=False):  # fmt: skip
    from datetime import date
    from uuid import UUID

    from app.repositories.types import LocationPrecision, Transaction

    return Transaction(
        id=UUID(f"10000000-0000-4000-8000-{n:012d}"),
        property_type=PropertyType.apartment,
        deal_type=deal_type,
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
        floor=5,
        contract_date=date.fromisoformat(day),
        build_year=2010,
        trade_method=TradeMethod.broker,
        is_cancelled=cancelled,
        cancelled_at=None,
    )


def _repo(complexes, transactions):
    from app.repositories.memory.dataset import MarketDataset

    return InMemoryMarketRepository(AS_OF, MarketDataset(tuple(complexes), tuple(transactions)), sample_regions())


async def test_typical_area_is_the_most_traded_plan_with_its_average_price():
    from app.repositories.types import AreaType, TypicalArea

    # 1번: 85㎡ 3건 vs 60㎡ 2건(+해제 1건). 최근 거래는 60㎡지만 건수가 많은 85㎡가 대표 평형
    one = _complex(1, AreaType(59.9, None), AreaType(84.97, 34.2))
    # 2번: 두 평형 2건씩 동률이면 마지막 거래가 더 최근인 60㎡
    two = _complex(2, AreaType(59.9, 24.1), AreaType(84.97, 34.2))
    # 3번: 건수·마지막 거래일까지 같으면 작은 평형. 102㎡는 평형 표에 없어서 묶음 값을 대표 면적으로 쓴다
    three = _complex(3, AreaType(110.2, 42.0))
    txs = [
        _tx(one, 1, "2026-06-01", 84.97, price=500_000_000),
        _tx(one, 2, "2026-07-01", 84.90, price=520_000_000),
        _tx(one, 3, "2026-08-01", 85.10, price=510_000_001),  # 합 1,530,000,001 ÷ 3 → 510,000,000
        _tx(one, 4, "2026-09-01", 59.90, price=400_000_000),
        _tx(one, 5, "2026-09-10", 59.90, price=410_000_000),
        _tx(one, 6, "2026-09-20", 59.90, price=900_000_000, cancelled=True),  # 해제는 세지 않는다
        _tx(two, 7, "2026-07-01", 84.97, price=500_000_000),
        _tx(two, 8, "2026-07-02", 84.97, price=500_000_000),
        _tx(two, 9, "2026-05-01", 59.90, price=300_000_000),
        _tx(two, 10, "2026-09-01", 59.90, price=300_000_001),  # 평균 300,000,000.5 → 반올림 300,000,001
        _tx(three, 11, "2026-09-01", 110.20, price=800_000_000),
        _tx(three, 12, "2026-09-01", 101.80, price=700_000_000),
    ]
    rows = {r.complex.id: r for r in await _repo([one, two, three], txs).complex_markers(BUSAN, flt())}
    assert rows[one.id].typical == TypicalArea(84.97, 34.2, 3, avg_price=510_000_000)
    assert rows[one.id].latest.id == txs[4].id  # 최근 거래(latest)는 그대로
    assert rows[two.id].typical == TypicalArea(59.9, 24.1, 2, avg_price=300_000_001)
    assert rows[three.id].typical == TypicalArea(102.0, None, 1, avg_price=700_000_000)


async def test_typical_area_follows_the_marker_filter_and_deal_type():
    from app.repositories.types import AreaType, TypicalArea

    c = _complex(1, AreaType(59.9, None), AreaType(84.97, 34.2))
    txs = [
        _tx(c, 1, "2026-08-01", 84.97, deal_type=DealType.monthly, deposit=50_000_000, rent=1_000_000),
        _tx(c, 2, "2026-08-02", 84.97, deal_type=DealType.monthly, deposit=30_000_000, rent=1_000_001),
        _tx(c, 3, "2026-08-03", 59.90, deal_type=DealType.monthly, deposit=10_000_000, rent=500_000),
        _tx(c, 4, "2026-08-04", 59.90, deal_type=DealType.jeonse, deposit=200_000_000),
    ]
    repo = _repo([c], txs)
    [monthly] = await repo.complex_markers(BUSAN, flt(deal_type=DealType.monthly))
    assert monthly.typical == TypicalArea(84.97, 34.2, 2, avg_deposit=40_000_000, avg_monthly_rent=1_000_001)
    [jeonse] = await repo.complex_markers(BUSAN, flt(deal_type=DealType.jeonse))
    assert jeonse.typical == TypicalArea(59.9, None, 1, avg_deposit=200_000_000)
    # 면적 필터로 85㎡를 빼면 남은 60㎡가 대표 평형
    [small] = await repo.complex_markers(BUSAN, flt(deal_type=DealType.monthly, exclusive_area_pyeong=Range(None, 20)))
    assert small.typical == TypicalArea(59.9, None, 1, avg_deposit=10_000_000, avg_monthly_rent=500_000)

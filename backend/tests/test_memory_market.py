import pytest

from app.core.dates import latest_etl_time
from app.core.geo import BBox
from app.repositories.memory.market import InMemoryMarketRepository
from app.repositories.types import DealType, Level, PropertyType, TradeMethod, TransactionFilter
from app.sample_data.market import build_sample_market
from tests.conftest import FIXED_NOW

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
    repo = InMemoryMarketRepository(AS_OF, market)
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
    repo = InMemoryMarketRepository(AS_OF)
    by_gu = await repo.aggregate_regions(Level.sigungu, BUSAN, flt())
    by_dong = await repo.aggregate_regions(Level.dong, BUSAN, flt())
    assert sum(a.transaction_count for a in by_gu) == sum(a.transaction_count for a in by_dong)


async def test_median_resists_outliers():
    repo = InMemoryMarketRepository(AS_OF)
    [haeundae] = [a for a in await repo.aggregate_regions(Level.sigungu, BUSAN, flt()) if a.region.name == "해운대구"]
    assert 20_000_000 < haeundae.median_price_per_pyeong < 40_000_000


async def test_direct_trades_are_dropped_on_request():
    market = build_sample_market(AS_OF.date())
    assert any(t.trade_method == TradeMethod.direct for t in market.transactions)
    repo = InMemoryMarketRepository(AS_OF, market)
    rows = await repo.complex_markers(BUSAN, flt(exclude_direct=True))
    assert rows and all(r.latest.trade_method == TradeMethod.broker for r in rows)


async def test_sample_data_is_deterministic():
    a, b = build_sample_market(AS_OF.date()), build_sample_market(AS_OF.date())
    assert a is b
    other_day = build_sample_market(AS_OF.date().replace(day=1))
    assert [c.id for c in other_day.complexes] == [c.id for c in a.complexes]


async def test_parcel_marker_pnus_resolve_to_real_parcels():
    from app.adapters.mock.parcel_source import MockParcelSource

    repo = InMemoryMarketRepository(AS_OF)
    source = MockParcelSource(2026)
    rows = await repo.parcel_markers(BUSAN, flt(property_type=PropertyType.land, period_months=60))
    assert rows
    for row in rows[:25]:
        assert await source.fetch_parcel(row.latest.pnu) is not None


async def test_complex_pnus_resolve_to_real_parcels():
    from app.adapters.mock.parcel_source import MockParcelSource

    market = build_sample_market(AS_OF.date())
    source = MockParcelSource(2026)
    for c in market.complexes[:25]:
        assert await source.fetch_parcel(c.pnu) is not None

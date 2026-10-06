import statistics
from collections import defaultdict
from datetime import datetime
from uuid import UUID

from app.core.dates import months_ago
from app.core.geo import BBox, pyeong_exact
from app.repositories.types import (
    Complex,
    ComplexMarkerRow,
    DealType,
    Level,
    LocationPrecision,
    ParcelMarkerRow,
    PropertyType,
    RegionAggregate,
    RegionRef,
    TradeMethod,
    Transaction,
    TransactionFilter,
    TransactionPage,
)
from app.sample_data.market import SampleMarket, build_sample_market
from app.sample_data.regions import DONG_BY_CODE, SIGUNGU_BY_CODE


def _median_int(values: list[float]) -> int | None:
    return int(round(statistics.median(values))) if values else None


class InMemoryMarketRepository:
    """샘플 거래 데이터를 메모리에서 집계하는 구현. DB 저장소로 교체해도 같은 포트를 따른다."""

    def __init__(self, as_of: datetime, market: SampleMarket | None = None):
        self._as_of = as_of
        self._market = market or build_sample_market(as_of.date())
        self._complexes = {c.id: c for c in self._market.complexes}
        self._by_kind: dict[tuple[PropertyType, DealType], list[Transaction]] = defaultdict(list)
        self._by_complex: dict[UUID, list[Transaction]] = defaultdict(list)
        for tx in self._market.transactions:
            self._by_kind[(tx.property_type, tx.deal_type)].append(tx)
            if tx.complex_id:
                self._by_complex[tx.complex_id].append(tx)

    @property
    def data_as_of(self) -> datetime:
        return self._as_of

    def _matching(self, flt: TransactionFilter) -> list[Transaction]:
        cutoff = months_ago(self._as_of.date(), flt.period_months)
        out = []
        for tx in self._by_kind[(flt.property_type, flt.deal_type)]:
            if tx.is_cancelled or tx.contract_date < cutoff:
                continue
            if flt.exclude_direct and tx.trade_method == TradeMethod.direct:
                continue
            if not (
                flt.price.accepts(tx.price) and flt.deposit.accepts(tx.deposit) and flt.rent.accepts(tx.monthly_rent)
            ):
                continue
            area_pyeong = pyeong_exact(tx.basis_area_m2)
            area_range = flt.land_area_pyeong if flt.property_type == PropertyType.land else flt.exclusive_area_pyeong
            if not area_range.accepts(area_pyeong):
                continue
            out.append(tx)
        return out

    async def aggregate_regions(self, level: Level, bbox: BBox, flt: TransactionFilter) -> list[RegionAggregate]:
        groups: dict[str, list[Transaction]] = defaultdict(list)
        for tx in self._matching(flt):
            groups[tx.region_code if level == Level.dong else tx.region_code[:5]].append(tx)

        out = []
        for code, txs in groups.items():
            region = self._region_ref(level, code)
            if not bbox.contains(region.lat, region.lng):
                continue
            per_pyeong = [(tx.price or tx.deposit or 0) / pyeong_exact(tx.basis_area_m2) for tx in txs]
            out.append(
                RegionAggregate(
                    region=region,
                    transaction_count=len(txs),
                    median_price_per_pyeong=_median_int(per_pyeong) if flt.deal_type == DealType.sale else None,
                    median_deposit_per_pyeong=_median_int(per_pyeong) if flt.deal_type == DealType.jeonse else None,
                    median_deposit=_median_int([tx.deposit for tx in txs if tx.deposit])
                    if flt.deal_type == DealType.monthly
                    else None,
                    median_monthly_rent=_median_int([tx.monthly_rent for tx in txs if tx.monthly_rent])
                    if flt.deal_type == DealType.monthly
                    else None,
                )
            )
        return sorted(out, key=lambda a: a.region.code)

    @staticmethod
    def _region_ref(level: Level, code: str) -> RegionRef:
        if level == Level.dong:
            d = DONG_BY_CODE[code]
            return RegionRef(d.code, d.name, d.lat, d.lng)
        s = SIGUNGU_BY_CODE[code]
        return RegionRef(s.code, s.name, s.lat, s.lng)

    async def complex_markers(self, bbox: BBox, flt: TransactionFilter) -> list[ComplexMarkerRow]:
        groups: dict[UUID, list[Transaction]] = defaultdict(list)
        for tx in self._matching(flt):
            if tx.complex_id and bbox.contains(tx.lat, tx.lng):
                groups[tx.complex_id].append(tx)
        rows = [
            ComplexMarkerRow(self._complexes[cid], len(txs), max(txs, key=lambda t: (t.contract_date, str(t.id))))
            for cid, txs in groups.items()
        ]
        return sorted(rows, key=lambda r: (-r.transaction_count, r.complex.name))

    async def parcel_markers(self, bbox: BBox, flt: TransactionFilter) -> list[ParcelMarkerRow]:
        latest: dict[str, Transaction] = {}
        for tx in self._matching(flt):
            if tx.location_precision != LocationPrecision.parcel or tx.pnu is None or not bbox.contains(tx.lat, tx.lng):
                continue
            current = latest.get(tx.pnu)
            if current is None or (tx.contract_date, str(tx.id)) > (current.contract_date, str(current.id)):
                latest[tx.pnu] = tx
        return [ParcelMarkerRow(tx) for tx in sorted(latest.values(), key=lambda t: t.pnu or "")]

    async def get_complex(self, complex_id: UUID) -> Complex | None:
        return self._complexes.get(complex_id)

    async def complex_transactions(
        self,
        complex_id: UUID,
        *,
        deal_type: DealType | None,
        exclude_direct: bool,
        include_cancelled: bool,
        sort: str,
        offset: int,
        limit: int,
    ) -> TransactionPage:
        items = [
            tx
            for tx in self._by_complex.get(complex_id, [])
            if (deal_type is None or tx.deal_type == deal_type)
            and (include_cancelled or not tx.is_cancelled)
            and not (exclude_direct and tx.trade_method == TradeMethod.direct)
        ]
        effective_price = lambda t: t.price if t.price is not None else (t.deposit or 0)  # noqa: E731
        if sort == "price_asc":
            items.sort(key=lambda t: (effective_price(t), str(t.id)))
        elif sort == "price_desc":
            items.sort(key=lambda t: (-effective_price(t), str(t.id)))
        else:
            items.sort(key=lambda t: (t.contract_date, str(t.id)), reverse=True)
        return TransactionPage(items[offset : offset + limit], len(items))

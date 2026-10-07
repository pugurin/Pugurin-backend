import statistics
from collections import defaultdict
from datetime import date, datetime
from uuid import UUID

from app.core.dates import months_ago
from app.core.geo import BBox, pyeong_exact
from app.core.text import normalize
from app.repositories.memory.dataset import MarketData, RegionDirectory, sample_regions
from app.repositories.types import (
    AreaStats,
    Complex,
    ComplexMarkerRow,
    ComplexSearchHit,
    DealType,
    Level,
    LocationPrecision,
    MonthStats,
    ParcelMarkerRow,
    PropertyType,
    RegionAggregate,
    RegionSearchHit,
    StatsMetrics,
    StatsResult,
    StatsSeries,
    TradeMethod,
    Transaction,
    TransactionFilter,
    TransactionPage,
)
from app.sample_data.market import build_sample_market

MIN_MONTH_SAMPLE = 3  # 월 표본이 이보다 적으면 중위값을 내지 않는다
BBOX_PADDING = 0.003  # 약 300m
MIN_BOX_HALF = 0.004


def _match_score(query: str, name: str) -> int:
    if query == name:
        return 100
    if name.startswith(query):
        return 80
    if query in name:
        return 60
    return 0


def _padded(box: list[float], center: tuple[float, float]) -> tuple[float, float, float, float]:
    """단지 위치의 범위에 여백을 주고, 너무 좁으면 중심점 기준으로 최소 크기를 보장한다."""
    min_lng, min_lat, max_lng, max_lat = box
    min_lng, min_lat, max_lng, max_lat = (
        min_lng - BBOX_PADDING,
        min_lat - BBOX_PADDING,
        max_lng + BBOX_PADDING,
        max_lat + BBOX_PADDING,
    )
    if max_lng - min_lng < 2 * MIN_BOX_HALF:
        min_lng, max_lng = center[1] - MIN_BOX_HALF, center[1] + MIN_BOX_HALF
    if max_lat - min_lat < 2 * MIN_BOX_HALF:
        min_lat, max_lat = center[0] - MIN_BOX_HALF, center[0] + MIN_BOX_HALF
    return round(min_lng, 6), round(min_lat, 6), round(max_lng, 6), round(max_lat, 6)


def _median_int(values: list[float]) -> int | None:
    return int(round(statistics.median(values))) if values else None


def _metrics(deal_type: DealType, txs: list[Transaction]) -> StatsMetrics:
    if not txs:
        return StatsMetrics()
    if deal_type == DealType.sale:
        return StatsMetrics(
            median_price_per_pyeong=_median_int([t.price / pyeong_exact(t.basis_area_m2) for t in txs if t.price])
        )
    if deal_type == DealType.jeonse:
        return StatsMetrics(
            median_deposit_per_pyeong=_median_int(
                [t.deposit / pyeong_exact(t.basis_area_m2) for t in txs if t.deposit is not None]
            )
        )
    return StatsMetrics(
        median_deposit=_median_int([t.deposit for t in txs if t.deposit is not None]),
        median_monthly_rent=_median_int([t.monthly_rent for t in txs if t.monthly_rent is not None]),
    )


def _series(deal_type: DealType, txs: list[Transaction], months: list[date]) -> StatsSeries:
    by_month: dict[tuple[int, int], list[Transaction]] = defaultdict(list)
    for t in txs:
        by_month[(t.contract_date.year, t.contract_date.month)].append(t)
    trend = []
    for first in months:
        bucket = by_month.get((first.year, first.month), [])
        metrics = _metrics(deal_type, bucket) if len(bucket) >= MIN_MONTH_SAMPLE else StatsMetrics()
        trend.append(MonthStats(first.strftime("%Y-%m"), len(bucket), metrics))
    return StatsSeries(len(txs), _metrics(deal_type, txs), tuple(trend))


class InMemoryMarketRepository:
    """거래 데이터를 메모리에서 집계하는 구현(샘플 또는 수집한 실데이터). DB 저장소로 교체해도 같은 포트를 따른다."""

    def __init__(self, as_of: datetime, market: MarketData | None = None, regions: RegionDirectory | None = None):
        self._as_of = as_of
        self._market = market or build_sample_market(as_of.date())
        self._regions = regions if regions is not None else sample_regions()
        self._complexes = {c.id: c for c in self._market.complexes}
        self._by_kind: dict[tuple[PropertyType, DealType], list[Transaction]] = defaultdict(list)
        self._by_complex: dict[UUID, list[Transaction]] = defaultdict(list)
        for tx in self._market.transactions:
            self._by_kind[(tx.property_type, tx.deal_type)].append(tx)
            if tx.complex_id:
                self._by_complex[tx.complex_id].append(tx)
        self._popularity = {cid: sum(1 for t in txs if not t.is_cancelled) for cid, txs in self._by_complex.items()}
        self._search_index = [(normalize(c.name), normalize(c.address), c) for c in self._market.complexes]
        self._region_boxes = self._build_region_boxes()

    @property
    def data_as_of(self) -> datetime:
        return self._as_of

    def _build_region_boxes(self) -> dict[str, tuple[float, float, float, float]]:
        spans: dict[str, list[float]] = {}
        for c in self._market.complexes:
            box = spans.setdefault(c.region_code, [c.lng, c.lat, c.lng, c.lat])
            box[0], box[1], box[2], box[3] = (
                min(box[0], c.lng),
                min(box[1], c.lat),
                max(box[2], c.lng),
                max(box[3], c.lat),
            )
        boxes: dict[str, tuple[float, float, float, float]] = {}
        for code, ref in self._regions.items():
            if len(code) == 10:
                boxes[code] = _padded(spans.get(code, [ref.lng, ref.lat, ref.lng, ref.lat]), (ref.lat, ref.lng))
        for code, ref in self._regions.items():
            if len(code) == 5:
                children = [b for c, b in boxes.items() if c.startswith(code)]
                if children:
                    boxes[code] = (
                        min(b[0] for b in children),
                        min(b[1] for b in children),
                        max(b[2] for b in children),
                        max(b[3] for b in children),
                    )
                else:
                    boxes[code] = _padded([ref.lng, ref.lat, ref.lng, ref.lat], (ref.lat, ref.lng))
        return boxes

    def _stats_window(self, period_months: int) -> list[date]:
        """데이터 기준월로 끝나는 period_months개월(달 단위)의 첫째 날 목록, 오래된 달이 먼저다."""
        last = self._as_of.date().replace(day=1)
        return [months_ago(last, n) for n in range(period_months - 1, -1, -1)]

    @staticmethod
    def _stats_filter(txs, deal_type: DealType, exclude_direct: bool, start: date) -> list[Transaction]:
        return [
            t
            for t in txs
            if t.deal_type == deal_type
            and not t.is_cancelled
            and t.contract_date >= start
            and not (exclude_direct and t.trade_method == TradeMethod.direct)
        ]

    async def region_stats(
        self,
        region_code: str,
        property_type: PropertyType,
        deal_type: DealType,
        period_months: int,
        exclude_direct: bool,
    ) -> StatsResult | None:
        if region_code not in self._regions:
            return None
        months = self._stats_window(period_months)
        candidates = (t for t in self._by_kind[(property_type, deal_type)] if t.region_code.startswith(region_code))
        txs = self._stats_filter(candidates, deal_type, exclude_direct, months[0])
        return StatsResult(_series(deal_type, txs, months))

    async def complex_stats(
        self, complex_id: UUID, deal_type: DealType, period_months: int, exclude_direct: bool
    ) -> StatsResult | None:
        found = self._complexes.get(complex_id)
        if found is None:
            return None
        months = self._stats_window(period_months)
        txs = self._stats_filter(self._by_complex.get(complex_id, []), deal_type, exclude_direct, months[0])
        by_area = []
        for area in sorted(found.area_types, key=lambda a: a.exclusive_area_m2):
            bucket = round(area.exclusive_area_m2)
            inside = [t for t in txs if t.exclusive_area_m2 is not None and round(t.exclusive_area_m2) == bucket]
            if inside:
                by_area.append(AreaStats(area, _series(deal_type, inside, months)))
        return StatsResult(_series(deal_type, txs, months), tuple(by_area))

    async def search_complexes(self, query: str, limit: int) -> list[ComplexSearchHit]:
        q = normalize(query)
        hits = []
        for name, address, complex_ in self._search_index:
            score = _match_score(q, name) or (40 if q in address else 0)
            if score:
                hits.append(ComplexSearchHit(complex_, score, self._popularity.get(complex_.id, 0)))
        hits.sort(key=lambda h: (-h.score, -h.popularity, h.complex.name))
        return hits[:limit]

    async def search_regions(self, query: str, limit: int) -> list[RegionSearchHit]:
        q = normalize(query)
        hits = []
        for code, ref in self._regions.items():
            name = normalize(ref.name)
            score = _match_score(q, name)
            full_name = ref.name
            if len(code) == 10:
                sigungu = self._regions.get(code[:5])
                if sigungu:
                    full_name = f"{sigungu.name} {ref.name}"
                    prefix = normalize(sigungu.name)
                    if q.startswith(prefix) and len(q) > len(prefix):
                        score = max(score, _match_score(q[len(prefix) :], name))
            if score:
                hits.append(
                    RegionSearchHit(
                        code,
                        "dong" if len(code) == 10 else "sigungu",
                        full_name,
                        ref.lat,
                        ref.lng,
                        self._region_boxes[code],
                        score,
                    )
                )
        hits.sort(key=lambda h: (-h.score, h.level != "sigungu", h.name))
        return hits[:limit]

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
            region = self._regions[code]
            if not bbox.contains(region.lat, region.lng):
                continue
            m = _metrics(flt.deal_type, txs)
            out.append(
                RegionAggregate(
                    region=region,
                    transaction_count=len(txs),
                    median_price_per_pyeong=m.median_price_per_pyeong,
                    median_deposit_per_pyeong=m.median_deposit_per_pyeong,
                    median_deposit=m.median_deposit,
                    median_monthly_rent=m.median_monthly_rent,
                )
            )
        return sorted(out, key=lambda a: a.region.code)

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

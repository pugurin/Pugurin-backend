import statistics
from collections import defaultdict
from datetime import datetime
from uuid import UUID

from app.core.dates import months_ago
from app.core.geo import BBox, pyeong_exact
from app.core.text import normalize
from app.repositories.memory.dataset import MarketData, RegionDirectory, sample_regions
from app.repositories.types import (
    Complex,
    ComplexMarkerRow,
    ComplexSearchHit,
    DealType,
    Level,
    LocationPrecision,
    ParcelMarkerRow,
    PropertyType,
    RegionAggregate,
    RegionSearchHit,
    TradeMethod,
    Transaction,
    TransactionFilter,
    TransactionPage,
)
from app.sample_data.market import build_sample_market

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

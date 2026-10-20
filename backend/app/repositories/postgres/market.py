"""PostgreSQL/PostGIS 거래 저장소 (#38). InMemoryMarketRepository와 같은 포트·같은 결과를 낸다.

- 거래는 매 요청 DB에서 조회·집계한다(중위값은 percentile_cont).
- 지역·단지는 적재가 끝날 때만 바뀌므로 메모리에 스냅샷으로 들고, 새 적재가 보이면 다시 읽는다.
  검색 정규화·지도 범위 계산을 파일 캐시 구현과 같은 코드로 하기 위해서다.
- 지분거래·지목 '도로'(토지)는 저장은 되어 있고 여기서 뺀다(ERD 원칙 6). 정렬의 동점 처리는 id 문자열(C 정렬)이다.
"""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.busan import SIGUNGU, SIGUNGU_BY_CODE
from app.core.dates import KST, months_ago
from app.core.geo import SQM_PER_PYEONG, BBox, area_bucket
from app.core.text import normalize
from app.repositories.memory.dataset import RegionDirectory
from app.repositories.memory.market import (
    MIN_MONTH_SAMPLE,
    region_boxes,
    search_complex_hits,
    search_region_hits,
)
from app.repositories.types import (
    AreaStats,
    AreaType,
    Complex,
    ComplexMarkerRow,
    ComplexSearchHit,
    DealType,
    Level,
    LocationPrecision,
    MonthStats,
    ParcelMarkerRow,
    PropertyType,
    Range,
    RegionAggregate,
    RegionRef,
    RegionSearchHit,
    StatsMetrics,
    StatsResult,
    StatsSeries,
    TradeMethod,
    Transaction,
    TransactionFilter,
    TransactionPage,
)

SNAPSHOT_CHECK_SECONDS = 30.0

_TX_COLUMNS = """
    t.id, t.property_type, t.deal_type, t.complex_id, t.region_code, t.jibun, t.pnu, t.location_precision,
    ST_Y(t.location) AS lat, ST_X(t.location) AS lng, t.price, t.deposit, t.monthly_rent,
    t.exclusive_area_m2::float8 AS exclusive_area_m2, t.land_area_m2::float8 AS land_area_m2, t.floor,
    t.contract_date, t.build_year, t.trade_method, t.is_cancelled, t.cancelled_at,
    t.contract_type IS NOT DISTINCT FROM '갱신' AS is_renewal
"""

_PYEONG = f"(t.basis_area_m2::float8 / {SQM_PER_PYEONG})"
_METRICS = f"""
    count(*) AS n,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY t.price::float8 / {_PYEONG})
        FILTER (WHERE t.price IS NOT NULL AND t.price <> 0) AS price_per_pyeong,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY t.deposit::float8 / {_PYEONG})
        FILTER (WHERE t.deposit IS NOT NULL) AS deposit_per_pyeong,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY t.deposit::float8) FILTER (WHERE t.deposit IS NOT NULL) AS deposit,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY t.monthly_rent::float8)
        FILTER (WHERE t.monthly_rent IS NOT NULL) AS monthly_rent
"""

_ORDER = {
    "price_asc": 'coalesce(t.price, t.deposit, 0) ASC, t.id::text COLLATE "C" ASC',
    "price_desc": 'coalesce(t.price, t.deposit, 0) DESC, t.id::text COLLATE "C" ASC',
}
_NOT_RENEWAL = "t.contract_type IS DISTINCT FROM '갱신'"
_LATEST_FIRST = 't.contract_date DESC, t.id::text COLLATE "C" DESC'


def _int(value: float | None) -> int | None:
    return None if value is None else int(round(value))


def _metrics(deal_type: DealType, row) -> StatsMetrics:
    if row is None or not row.n:
        return StatsMetrics()
    if deal_type == DealType.sale:
        return StatsMetrics(median_price_per_pyeong=_int(row.price_per_pyeong))
    if deal_type == DealType.jeonse:
        return StatsMetrics(median_deposit_per_pyeong=_int(row.deposit_per_pyeong))
    return StatsMetrics(median_deposit=_int(row.deposit), median_monthly_rent=_int(row.monthly_rent))


@dataclass
class _Snapshot:
    version: int | None
    as_of: datetime | None
    regions: RegionDirectory
    region_names: dict[str, str]
    boxes: dict[str, tuple[float, float, float, float]]
    complexes: dict[UUID, Complex]
    popularity: dict[UUID, int]
    search_index: list[tuple[str, str, Complex]] = field(default_factory=list)


class _Where:
    """조건과 바인딩 값을 같이 모은다."""

    def __init__(self, base: str, params: dict):
        self.clauses = [base]
        self.params = dict(params)

    def add(self, clause: str, **params) -> None:
        self.clauses.append(clause)
        self.params.update(params)

    def range(self, column: str, name: str, rng: Range) -> None:
        if not rng.active:
            return
        self.add(f"{column} IS NOT NULL")
        if rng.min is not None:
            self.add(f"{column} >= CAST(:{name}_min AS float8)", **{f"{name}_min": float(rng.min)})
        if rng.max is not None:
            self.add(f"{column} <= CAST(:{name}_max AS float8)", **{f"{name}_max": float(rng.max)})

    @property
    def sql(self) -> str:
        return " AND ".join(f"({c})" for c in self.clauses)


class PostgresMarketRepository:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        fallback_as_of: datetime,
        exclude_share_deals: bool = True,
        exclude_road_land: bool = True,
        check_seconds: float = SNAPSHOT_CHECK_SECONDS,
    ):
        self._engine = engine
        self._fallback_as_of = fallback_as_of
        self._visible = "t.deleted_at IS NULL"
        if exclude_share_deals:
            self._visible += " AND NOT (t.property_type = 'land' AND t.is_share_deal)"
        if exclude_road_land:
            self._visible += " AND NOT (t.property_type = 'land' AND t.jimok IS NOT DISTINCT FROM '도로')"
        self._check_seconds = check_seconds
        self._snapshot: _Snapshot | None = None
        self._checked_at = 0.0
        self._lock = asyncio.Lock()

    # ── 스냅샷 ────────────────────────────────────────────────

    @property
    def data_as_of(self) -> datetime:
        snap = self._snapshot
        return snap.as_of if snap and snap.as_of else self._fallback_as_of

    @property
    def has_data(self) -> bool | None:
        """적재된 데이터가 있는지. 아직 읽지 않았으면 None."""
        return None if self._snapshot is None else self._snapshot.version is not None

    async def refresh(self, *, force: bool = False) -> None:
        """새 적재가 끝났으면 지역·단지 스냅샷을 다시 읽는다. 확인은 check_seconds마다 한 번만 한다."""
        if not force and self._snapshot is not None and time.monotonic() - self._checked_at < self._check_seconds:
            return
        async with self._lock:
            if not force and self._snapshot is not None and time.monotonic() - self._checked_at < self._check_seconds:
                return
            async with self._engine.connect() as conn:
                version = (
                    await conn.execute(text("SELECT max(id) FROM etl_runs WHERE finished_at IS NOT NULL"))
                ).scalar()
                if force or self._snapshot is None or version != self._snapshot.version:
                    self._snapshot = await self._load_snapshot(conn, version)
            self._checked_at = time.monotonic()

    async def _snap(self) -> _Snapshot:
        await self.refresh()
        assert self._snapshot is not None
        return self._snapshot

    async def _load_snapshot(self, conn: AsyncConnection, version: int | None) -> _Snapshot:
        as_of = (await conn.execute(text("SELECT max(fetched_at) FROM source_fetch_log"))).scalar()
        rows = (
            await conn.execute(
                text("SELECT code, level, parent_code, name, ST_Y(centroid), ST_X(centroid) FROM regions")
            )
        ).all()
        with_trades = {
            r[0]
            for r in (
                await conn.execute(text(f"SELECT DISTINCT t.region_code FROM transactions t WHERE {self._visible}"))
            ).all()
        }
        names = {code: name for code, _, _, name, _, _ in rows}
        centroid = {code: (lat, lng) for code, _, _, _, lat, lng in rows if lat is not None}
        regions: RegionDirectory = {}
        for s in SIGUNGU:
            lat, lng = centroid.get(s.code, (s.lat, s.lng))
            regions[s.code] = RegionRef(s.code, s.name, lat, lng)
        for code, level, parent, name, _, _ in sorted(rows):
            if level == "dong" and code in with_trades and parent in regions:
                lat, lng = centroid.get(code, (regions[parent].lat, regions[parent].lng))
                regions[code] = RegionRef(code, name, lat, lng)

        areas: dict[UUID, list[AreaType]] = defaultdict(list)
        for cid, area, supply in (
            await conn.execute(
                text(
                    "SELECT complex_id, exclusive_area_m2::float8, supply_area_pyeong::float8 "
                    "FROM complex_area_types ORDER BY complex_id, area_bucket"
                )
            )
        ).all():
            areas[cid].append(AreaType(area, supply))
        complexes: dict[UUID, Complex] = {}
        for r in (
            await conn.execute(
                text(
                    "SELECT id, property_type, name, region_code, jibun, pnu, ST_Y(location) AS lat, "
                    "ST_X(location) AS lng, build_year, household_count FROM complexes"
                )
            )
        ).all():
            complexes[r.id] = Complex(
                id=r.id,
                property_type=PropertyType(r.property_type),
                name=r.name,
                address=self._address(names, r.region_code, r.jibun),
                region_code=r.region_code,
                pnu=r.pnu,
                lat=r.lat,
                lng=r.lng,
                build_year=r.build_year,
                household_count=r.household_count,
                area_types=tuple(areas.get(r.id, ())),
            )
        popularity = dict(
            (
                await conn.execute(
                    text(
                        f"""SELECT t.complex_id, count(*) FROM transactions t
                            WHERE {self._visible} AND t.complex_id IS NOT NULL AND NOT t.is_cancelled GROUP BY 1"""
                    )
                )
            ).all()
        )
        ordered = sorted(complexes.values(), key=lambda c: (c.name, str(c.id)))
        return _Snapshot(
            version=version,
            as_of=as_of.astimezone(KST) if as_of else None,
            regions=regions,
            region_names=names,
            boxes=region_boxes(regions, ordered),
            complexes=complexes,
            popularity=popularity,
            search_index=[(normalize(c.name), normalize(c.address), c) for c in ordered],
        )

    @staticmethod
    def _address(names: dict[str, str], region_code: str, jibun: str | None) -> str:
        sgg = SIGUNGU_BY_CODE[region_code[:5]].name
        return f"부산 {sgg} {names.get(region_code, '')} {jibun or ''}".strip()

    def _transaction(self, snap: _Snapshot, r) -> Transaction:
        land = r.property_type == PropertyType.land
        return Transaction(
            id=r.id,
            property_type=PropertyType(r.property_type),
            deal_type=DealType(r.deal_type),
            complex_id=r.complex_id,
            address=self._address(snap.region_names, r.region_code, r.jibun),
            region_code=r.region_code,
            jibun=r.jibun,
            pnu=r.pnu,
            location_precision=LocationPrecision(r.location_precision),
            lat=r.lat,
            lng=r.lng,
            price=r.price,
            deposit=r.deposit,
            monthly_rent=r.monthly_rent,
            exclusive_area_m2=None if land else r.exclusive_area_m2,
            land_area_m2=r.land_area_m2 if land else None,
            supply_area_pyeong=None,
            floor=r.floor,
            contract_date=r.contract_date,
            build_year=r.build_year,
            trade_method=TradeMethod(r.trade_method) if r.trade_method else None,
            is_cancelled=r.is_cancelled,
            cancelled_at=datetime.combine(r.cancelled_at, datetime.min.time(), tzinfo=KST) if r.cancelled_at else None,
            is_renewal=r.is_renewal,
        )

    # ── 필터 ──────────────────────────────────────────────────

    def _matching(self, flt: TransactionFilter, *, include_cancelled: bool = False) -> _Where:
        w = _Where(self._visible, {})
        w.add("t.property_type = :pt AND t.deal_type = :dt", pt=flt.property_type.value, dt=flt.deal_type.value)
        w.add("t.contract_date >= :cutoff", cutoff=months_ago(self.data_as_of.date(), flt.period_months))
        if not include_cancelled:
            w.add("NOT t.is_cancelled")
        if flt.exclude_direct:
            w.add("t.trade_method IS DISTINCT FROM 'direct'")
        if not flt.include_renewals:
            w.add(_NOT_RENEWAL)
        w.range("t.price", "price", flt.price)
        w.range("t.deposit", "deposit", flt.deposit)
        w.range("t.monthly_rent", "rent", flt.rent)
        area = flt.land_area_pyeong if flt.property_type == PropertyType.land else flt.exclusive_area_pyeong
        w.range(_PYEONG, "area", area)
        return w

    @staticmethod
    def _bbox(w: _Where, bbox: BBox) -> None:
        w.add(
            "t.location && ST_MakeEnvelope(:min_lng, :min_lat, :max_lng, :max_lat, 4326)",
            min_lng=bbox.min_lng,
            min_lat=bbox.min_lat,
            max_lng=bbox.max_lng,
            max_lat=bbox.max_lat,
        )

    async def _fetch(self, sql: str, params: dict):
        async with self._engine.connect() as conn:
            return (await conn.execute(text(sql), params)).all()

    # ── 지도 ──────────────────────────────────────────────────

    async def aggregate_regions(self, level: Level, bbox: BBox, flt: TransactionFilter) -> list[RegionAggregate]:
        snap = await self._snap()
        w = self._matching(flt)
        width = 10 if level == Level.dong else 5
        rows = await self._fetch(
            f"SELECT left(t.region_code, {width}) AS code, {_METRICS} FROM transactions t WHERE {w.sql} GROUP BY 1",
            w.params,
        )
        out = []
        for r in rows:
            region = snap.regions.get(r.code)
            if region is None or not bbox.contains(region.lat, region.lng):
                continue
            m = _metrics(flt.deal_type, r)
            out.append(
                RegionAggregate(
                    region=region,
                    transaction_count=r.n,
                    median_price_per_pyeong=m.median_price_per_pyeong,
                    median_deposit_per_pyeong=m.median_deposit_per_pyeong,
                    median_deposit=m.median_deposit,
                    median_monthly_rent=m.median_monthly_rent,
                )
            )
        return sorted(out, key=lambda a: a.region.code)

    async def complex_markers(self, bbox: BBox, flt: TransactionFilter) -> list[ComplexMarkerRow]:
        snap = await self._snap()
        w = self._matching(flt)
        w.add("t.complex_id IS NOT NULL")
        self._bbox(w, bbox)
        rows = await self._fetch(
            f"""SELECT DISTINCT ON (t.complex_id) count(*) OVER (PARTITION BY t.complex_id) AS n, {_TX_COLUMNS}
                FROM transactions t WHERE {w.sql}
                ORDER BY t.complex_id, {_LATEST_FIRST}""",
            w.params,
        )
        markers = [
            ComplexMarkerRow(snap.complexes[r.complex_id], r.n, self._transaction(snap, r))
            for r in rows
            if r.complex_id in snap.complexes
        ]
        return sorted(markers, key=lambda m: (-m.transaction_count, m.complex.name, str(m.complex.id)))

    async def parcel_markers(self, bbox: BBox, flt: TransactionFilter) -> list[ParcelMarkerRow]:
        snap = await self._snap()
        w = self._matching(flt)
        w.add("t.location_precision = 'parcel' AND t.pnu IS NOT NULL")
        self._bbox(w, bbox)
        rows = await self._fetch(
            f"""SELECT DISTINCT ON (t.pnu) {_TX_COLUMNS} FROM transactions t WHERE {w.sql}
                ORDER BY t.pnu, {_LATEST_FIRST}""",
            w.params,
        )
        return [ParcelMarkerRow(self._transaction(snap, r)) for r in sorted(rows, key=lambda r: r.pnu)]

    # ── 단지·지역 ─────────────────────────────────────────────

    async def get_complex(self, complex_id: UUID) -> Complex | None:
        return (await self._snap()).complexes.get(complex_id)

    async def get_region(self, region_code: str) -> RegionRef | None:
        return (await self._snap()).regions.get(region_code)

    async def search_complexes(self, query: str, limit: int) -> list[ComplexSearchHit]:
        snap = await self._snap()
        return search_complex_hits(snap.search_index, snap.popularity, query, limit)

    async def search_regions(self, query: str, limit: int) -> list[RegionSearchHit]:
        snap = await self._snap()
        return search_region_hits(snap.regions, snap.boxes, query, limit)

    # ── 거래 목록 ─────────────────────────────────────────────

    async def _page(self, w: _Where, sort: str, offset: int, limit: int) -> TransactionPage:
        snap = await self._snap()
        order = _ORDER.get(sort, _LATEST_FIRST)
        async with self._engine.connect() as conn:
            total = (await conn.execute(text(f"SELECT count(*) FROM transactions t WHERE {w.sql}"), w.params)).scalar()
            rows = (
                await conn.execute(
                    text(
                        f"SELECT {_TX_COLUMNS} FROM transactions t WHERE {w.sql} "
                        f"ORDER BY {order} OFFSET :offset LIMIT :limit"
                    ),
                    {**w.params, "offset": offset, "limit": limit},
                )
            ).all()
        return TransactionPage([self._transaction(snap, r) for r in rows], total)

    async def list_transactions(
        self,
        flt: TransactionFilter,
        *,
        bbox: BBox | None,
        region_code: str | None,
        include_cancelled: bool,
        sort: str,
        offset: int,
        limit: int,
    ) -> TransactionPage:
        await self._snap()  # 기간 기준(data_as_of)을 먼저 맞춘다
        w = self._matching(flt, include_cancelled=include_cancelled)
        if region_code is not None:
            w.add("left(t.region_code, :rc_len) = :rc", rc_len=len(region_code), rc=region_code)
        if bbox is not None:
            self._bbox(w, bbox)
        return await self._page(w, sort, offset, limit)

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
        include_renewals: bool = False,
    ) -> TransactionPage:
        w = _Where(self._visible, {})
        w.add("t.complex_id = :cid", cid=complex_id)
        if not include_renewals:
            w.add(_NOT_RENEWAL)
        if deal_type is not None:
            w.add("t.deal_type = :dt", dt=deal_type.value)
        if not include_cancelled:
            w.add("NOT t.is_cancelled")
        if exclude_direct:
            w.add("t.trade_method IS DISTINCT FROM 'direct'")
        return await self._page(w, sort, offset, limit)

    async def get_transaction(self, transaction_id: UUID) -> Transaction | None:
        snap = await self._snap()
        rows = await self._fetch(
            f"SELECT {_TX_COLUMNS} FROM transactions t WHERE {self._visible} AND t.id = :id", {"id": transaction_id}
        )
        return self._transaction(snap, rows[0]) if rows else None

    # ── 통계 ──────────────────────────────────────────────────

    def _stats_window(self, period_months: int) -> list[date]:
        last = self.data_as_of.date().replace(day=1)
        return [months_ago(last, n) for n in range(period_months - 1, -1, -1)]

    def _stats_where(self, deal_type: DealType, exclude_direct: bool, start: date, include_renewals: bool) -> _Where:
        w = _Where(self._visible, {})
        w.add("t.deal_type = :dt AND NOT t.is_cancelled AND t.contract_date >= :start", dt=deal_type.value, start=start)
        if exclude_direct:
            w.add("t.trade_method IS DISTINCT FROM 'direct'")
        if not include_renewals:
            w.add(_NOT_RENEWAL)
        return w

    @staticmethod
    def _series(deal_type: DealType, months: list[date], overall, by_month: dict[date, object]) -> StatsSeries:
        trend = []
        for first in months:
            row = by_month.get(first)
            count = row.n if row else 0
            metrics = _metrics(deal_type, row) if count >= MIN_MONTH_SAMPLE else StatsMetrics()
            trend.append(MonthStats(first.strftime("%Y-%m"), count, metrics))
        return StatsSeries(overall.n if overall else 0, _metrics(deal_type, overall), tuple(trend))

    async def _grouped(self, w: _Where, bucket: str | None = None):
        """월별·기간 전체(+평형별) 지표를 한 번에 구한다. g_month/g_bucket이 1이면 그 축으로 합친 행이다."""
        month = "date_trunc('month', t.contract_date)::date"
        if bucket is None:
            select, sets = f"{month} AS month, GROUPING({month}) AS g_month", f"({month}), ()"
        else:
            select = f"{month} AS month, {bucket} AS bucket, GROUPING({month}) AS g_month"
            select += f", GROUPING({bucket}) AS g_bucket"
            sets = f"({bucket}, {month}), ({bucket}), ({month}), ()"
        return await self._fetch(
            f"SELECT {select}, {_METRICS} FROM transactions t WHERE {w.sql} GROUP BY GROUPING SETS ({sets})", w.params
        )

    async def region_stats(
        self,
        region_code: str,
        property_type: PropertyType,
        deal_type: DealType,
        period_months: int,
        exclude_direct: bool,
        *,
        include_renewals: bool = False,
    ) -> StatsResult | None:
        snap = await self._snap()
        if region_code not in snap.regions:
            return None
        months = self._stats_window(period_months)
        w = self._stats_where(deal_type, exclude_direct, months[0], include_renewals)
        w.add(
            "t.property_type = :pt AND left(t.region_code, :rc_len) = :rc",
            pt=property_type.value,
            rc_len=len(region_code),
            rc=region_code,
        )
        rows = await self._grouped(w)
        overall = next((r for r in rows if r.g_month == 1), None)
        by_month = {r.month: r for r in rows if r.g_month == 0}
        return StatsResult(self._series(deal_type, months, overall, by_month))

    async def complex_stats(
        self,
        complex_id: UUID,
        deal_type: DealType,
        period_months: int,
        exclude_direct: bool,
        *,
        include_renewals: bool = False,
    ) -> StatsResult | None:
        snap = await self._snap()
        found = snap.complexes.get(complex_id)
        if found is None:
            return None
        months = self._stats_window(period_months)
        w = self._stats_where(deal_type, exclude_direct, months[0], include_renewals)
        w.add("t.complex_id = :cid", cid=complex_id)
        rows = await self._grouped(w, "floor(t.exclusive_area_m2 + 0.5)::int")
        overall = next((r for r in rows if r.g_month == 1 and r.g_bucket == 1), None)
        by_month = {r.month: r for r in rows if r.g_month == 0 and r.g_bucket == 1}
        bucket_total = {r.bucket: r for r in rows if r.g_month == 1 and r.g_bucket == 0 and r.bucket is not None}
        bucket_month: dict[int, dict[date, object]] = defaultdict(dict)
        for r in rows:
            if r.g_month == 0 and r.g_bucket == 0 and r.bucket is not None:
                bucket_month[r.bucket][r.month] = r
        by_area = []
        for area in sorted(found.area_types, key=lambda a: a.exclusive_area_m2):
            bucket = area_bucket(area.exclusive_area_m2)
            if bucket in bucket_total:
                by_area.append(
                    AreaStats(area, self._series(deal_type, months, bucket_total[bucket], bucket_month[bucket]))
                )
        return StatsResult(self._series(deal_type, months, overall, by_month), tuple(by_area))

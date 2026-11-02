"""경매·공매 조회. 데이터는 `python -m app.ingestion auctions`(worker가 매일)로 채운 `auctions` 테이블에만 있다."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.dates import months_ago
from app.repositories.types import (
    Auction,
    AuctionFilter,
    AuctionGroup,
    AuctionPage,
    AuctionSort,
    AuctionSource,
    RecentTrade,
)

# 경매마당 용도(getcaUseCodeList 이름) → 우리 매물 유형. '%'로 끝나면 앞부분 일치(다세대주택, 아파트(생활주택) 등)
_GROUPS: dict[AuctionGroup, tuple[str, ...]] = {
    AuctionGroup.apartment: ("아파트%", "주상복합%"),
    AuctionGroup.officetel: ("오피스텔", "오피스텔(주거)%"),
    AuctionGroup.villa: ("다세대%", "연립%", "도시생활주택%", "도시형생활주택%", "빌라"),
    AuctionGroup.land: (
        "대지", "전", "답", "과수원", "목장용지", "임야", "잡종지", "도로", "주차장", "공장용지", "창고용지",
        "학교용지", "종교용지", "체육용지", "공원", "하천", "구거", "제방", "유지", "염전", "묘지", "기타용지",
        "기타토지", "토지",
    ),
}  # fmt: skip
_ALL_PATTERNS = [p for patterns in _GROUPS.values() for p in patterns]


def _matches(usage: str, pattern: str) -> bool:
    return usage.startswith(pattern[:-1]) if pattern.endswith("%") else usage == pattern


def auction_group(usage: str | None) -> AuctionGroup:
    for group, patterns in _GROUPS.items():
        if usage and any(_matches(usage, p) for p in patterns):
            return group
    return AuctionGroup.other


# 실거래와 비교하는 유형: 경매 용도 묶음 → 단지 property_type
_COMPARABLE = (AuctionGroup.apartment, AuctionGroup.officetel, AuctionGroup.villa)


def _group_case() -> str:
    """물건용도 원문 → 비교할 단지 유형(apartment·officetel·villa), 나머지는 NULL.

    패턴은 코드 상수라 SQL에 바로 넣는다(사용자 입력이 아니다).
    """
    whens = []
    for group in _COMPARABLE:
        cond = " OR ".join(f"a.usage LIKE '{p}'" if p.endswith("%") else f"a.usage = '{p}'" for p in _GROUPS[group])
        whens.append(f"WHEN {cond} THEN '{group.value}'")
    return "CASE " + " ".join(whens) + " END"


# 단지와는 PNU(같은 필지)로 잇는다.
# 한 필지에 단지가 여럿이면 같은 유형·전용면적이 가장 가까운 평형이 있는 곳, 그다음 id 순.
# 경매 건물면적은 전용면적이다(운영 데이터에서 아파트·오피스텔의 95% 이상이 단지 평형과 0.5㎡ 안에서 일치).
# 최근 실거래: 그 평형(area_bucket)의 최근 12개월 매매 중 가장 최근 1건. 해제·직거래·삭제된 거래는 뺀다.
_FROM = f"""
    FROM auctions a
    LEFT JOIN LATERAL (
        SELECT c.id, at.area_bucket
        FROM complexes c
        LEFT JOIN LATERAL (
            SELECT t.area_bucket, abs(t.exclusive_area_m2 - a.building_area_m2) AS diff
            FROM complex_area_types t
            WHERE t.complex_id = c.id AND a.building_area_m2 IS NOT NULL
              AND c.property_type = ({_group_case()})
              AND abs(t.exclusive_area_m2 - a.building_area_m2) <= :area_tolerance
            ORDER BY diff, t.area_bucket LIMIT 1
        ) at ON true
        WHERE a.pnu IS NOT NULL AND c.pnu = a.pnu
        ORDER BY at.diff IS NULL, at.diff, c.id LIMIT 1
    ) cx ON true
    LEFT JOIN LATERAL (
        SELECT t.price, t.contract_date, t.exclusive_area_m2::float8 AS exclusive_area_m2, count(*) OVER () AS n
        FROM transactions t
        WHERE cx.area_bucket IS NOT NULL AND t.complex_id = cx.id
          AND t.deleted_at IS NULL AND NOT t.is_cancelled
          AND t.deal_type = 'sale' AND t.trade_method IS DISTINCT FROM 'direct'
          AND t.contract_date >= :since AND floor(t.exclusive_area_m2 + 0.5) = cx.area_bucket
        ORDER BY t.contract_date DESC, t.id LIMIT 1
    ) rt ON true
"""

_SELECT = f"""
    SELECT a.id, a.source, a.case_no, a.court_name, a.title, a.usage, a.address,
           ST_Y(a.location) AS lat, ST_X(a.location) AS lng, a.location_precise,
           cx.id AS complex_id,
           a.appraisal_price, a.min_price, a.deposit, a.fail_count, a.sale_date, a.bid_start_at, a.bid_end_at,
           a.land_area_m2::float8 AS land_area_m2, a.building_area_m2::float8 AS building_area_m2,
           a.is_share, a.status, a.closed_at, a.schedule, a.results,
           rt.price AS recent_trade_price, rt.contract_date AS recent_trade_date,
           rt.exclusive_area_m2 AS recent_trade_area_m2, rt.n AS recent_trade_count
    {_FROM}
"""

_ORDER = {
    AuctionSort.sale_date: "a.sale_date NULLS LAST, a.id",
    AuctionSort.min_price_ratio: "a.min_price::float8 / NULLIF(a.appraisal_price, 0) NULLS LAST, a.id",
    AuctionSort.min_price: "a.min_price NULLS LAST, a.id",
    # 최근 실거래 대비 많이 싼 순 = 최저가 ÷ 최근 실거래가가 작은 순. 비교할 거래가 없으면 뒤로
    AuctionSort.recent_trade_discount: "a.min_price::float8 / NULLIF(rt.price, 0) NULLS LAST, a.id",
}
AREA_TOLERANCE_M2 = 3.0
RECENT_TRADE_MONTHS = 12

_DATA_AS_OF = text(
    """
    SELECT max(finished_at) FROM etl_runs
    WHERE job = 'auction' AND status IN ('success', 'partial', 'quota_exceeded')
    """
)


class PostgresAuctionRepository:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def data_as_of(self) -> datetime | None:
        """마지막으로 끝난 경매 동기화 시각. 한 번도 안 끝났으면 None(데이터 없음)."""
        async with self._engine.connect() as conn:
            return (await conn.execute(_DATA_AS_OF)).scalar()

    async def search(
        self, flt: AuctionFilter, *, today: date, sort: AuctionSort, offset: int, limit: int
    ) -> AuctionPage:
        where, params = self._where(flt)
        order = f" ORDER BY {_ORDER[sort]}"
        async with self._engine.connect() as conn:
            total = (await conn.execute(text(f"SELECT count(*) FROM auctions a{where}"), params)).scalar_one()
            rows = (
                await conn.execute(
                    text(f"{_SELECT}{where}{order} OFFSET :offset LIMIT :limit"),
                    {**params, **_trade_params(today), "offset": offset, "limit": limit},
                )
            ).mappings()
            return AuctionPage([_auction(r) for r in rows], total)

    async def get(self, auction_id: str, *, today: date) -> Auction | None:
        async with self._engine.connect() as conn:
            row = (
                (await conn.execute(text(f"{_SELECT} WHERE a.id = :id"), {"id": auction_id, **_trade_params(today)}))
                .mappings()
                .first()
            )
        return _auction(row) if row else None

    @staticmethod
    def _where(flt: AuctionFilter) -> tuple[str, dict]:
        clauses: list[str] = []
        params: dict = {}
        if not flt.include_closed:
            clauses.append("a.closed_at IS NULL")
        if flt.exclude_share:
            clauses.append("NOT a.is_share")
        if flt.source is not None:
            clauses.append("a.source = :source")
            params["source"] = flt.source.value
        if flt.bbox is not None:
            clauses.append("a.location && ST_MakeEnvelope(:min_lng, :min_lat, :max_lng, :max_lat, 4326)")
            b = flt.bbox
            params.update(min_lng=b.min_lng, min_lat=b.min_lat, max_lng=b.max_lng, max_lat=b.max_lat)
        if flt.complex_id is not None:
            clauses.append("a.pnu IS NOT NULL AND a.pnu = (SELECT pnu FROM complexes WHERE id = :complex_id)")
            params["complex_id"] = flt.complex_id
        if flt.groups:
            patterns = [p for g in flt.groups for p in _GROUPS.get(g, ())]
            parts = []
            if patterns:
                parts.append("a.usage LIKE ANY(CAST(:patterns AS text[]))")
                params["patterns"] = patterns
            if AuctionGroup.other in flt.groups:
                parts.append("a.usage IS NULL OR NOT (a.usage LIKE ANY(CAST(:known AS text[])))")
                params["known"] = _ALL_PATTERNS
            clauses.append("(" + " OR ".join(f"({p})" for p in parts) + ")")
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def _trade_params(today: date) -> dict:
    return {"since": months_ago(today, RECENT_TRADE_MONTHS), "area_tolerance": AREA_TOLERANCE_M2}


def _auction(r) -> Auction:
    complex_id = r["complex_id"]
    return Auction(
        id=r["id"],
        source=AuctionSource(r["source"]),
        case_no=r["case_no"],
        court_name=r["court_name"],
        title=r["title"],
        usage=r["usage"],
        group=auction_group(r["usage"]),
        address=r["address"],
        lat=r["lat"],
        lng=r["lng"],
        location_precise=r["location_precise"],
        complex_id=complex_id if complex_id is None or isinstance(complex_id, UUID) else UUID(str(complex_id)),
        appraisal_price=r["appraisal_price"],
        min_price=r["min_price"],
        deposit=r["deposit"],
        fail_count=r["fail_count"],
        sale_date=r["sale_date"],
        bid_start_at=r["bid_start_at"],
        bid_end_at=r["bid_end_at"],
        land_area_m2=r["land_area_m2"],
        building_area_m2=r["building_area_m2"],
        is_share=r["is_share"],
        status=r["status"],
        closed_at=r["closed_at"],
        schedule=tuple(r["schedule"] or ()),
        results=tuple(r["results"] or ()),
        recent_trade=(
            RecentTrade(
                price=r["recent_trade_price"],
                contract_date=r["recent_trade_date"],
                exclusive_area_m2=r["recent_trade_area_m2"],
                count=r["recent_trade_count"],
            )
            if r["recent_trade_price"] is not None
            else None
        ),
    )

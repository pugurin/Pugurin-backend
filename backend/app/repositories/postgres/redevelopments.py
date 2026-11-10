"""정비사업 구역 조회. 데이터는 `python -m app.ingestion redevelopments`(worker가 매일)로 채운 테이블에만 있다."""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.repositories.types import (
    LocationKind,
    Redevelopment,
    RedevelopmentFilter,
    RedevelopmentKind,
    RedevelopmentPage,
    RedevelopmentStage,
)

_COLS = """
    id, area_name, kind, step_raw, stage, stage_order, location, location_kind, lat, lng, location_precise,
    area_m2, households, buildings, union_members, floor_area_ratio, building_coverage_ratio, scale,
    business_entity, contractor, engineer, architect, specialists, tel, images,
    first_seen_at, last_seen_at, closed_at
"""
# 사업이 많이 나간 구역부터. 모르는 단계(NULL)는 뒤로
_ORDER = "stage_order DESC NULLS LAST, area_name, id"

_DATA_AS_OF = text(
    """
    SELECT max(finished_at) FROM etl_runs
    WHERE job = 'redevelopment' AND status IN ('success', 'partial', 'quota_exceeded')
    """
)


class PostgresRedevelopmentRepository:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def data_as_of(self) -> datetime | None:
        """마지막으로 끝난 정비사업 동기화 시각. 한 번도 안 끝났으면 None(데이터 없음)."""
        async with self._engine.connect() as conn:
            return (await conn.execute(_DATA_AS_OF)).scalar()

    async def search(self, flt: RedevelopmentFilter, *, offset: int, limit: int) -> RedevelopmentPage:
        where, params = self._where(flt)
        async with self._engine.connect() as conn:
            total = (await conn.execute(text(f"SELECT count(*) FROM redevelopments{where}"), params)).scalar_one()
            rows = await conn.execute(
                text(f"SELECT {_COLS} FROM redevelopments{where} ORDER BY {_ORDER} OFFSET :offset LIMIT :limit"),
                {**params, "offset": offset, "limit": limit},
            )
            return RedevelopmentPage([_zone(r) for r in rows.mappings()], total)

    async def get(self, zone_id: str) -> Redevelopment | None:
        async with self._engine.connect() as conn:
            row = (
                (await conn.execute(text(f"SELECT {_COLS} FROM redevelopments WHERE id = :id"), {"id": zone_id}))
                .mappings()
                .first()
            )
        return _zone(row) if row else None

    @staticmethod
    def _where(flt: RedevelopmentFilter) -> tuple[str, dict]:
        clauses: list[str] = []
        params: dict = {}
        if not flt.include_closed:
            clauses.append("closed_at IS NULL")
        if flt.bbox is not None:
            clauses.append("geom && ST_MakeEnvelope(:min_lng, :min_lat, :max_lng, :max_lat, 4326)")
            b = flt.bbox
            params.update(min_lng=b.min_lng, min_lat=b.min_lat, max_lng=b.max_lng, max_lat=b.max_lat)
        if flt.kinds:
            clauses.append("kind = ANY(CAST(:kinds AS text[]))")
            params["kinds"] = [k.value for k in flt.kinds]
        if flt.stages:
            clauses.append("stage = ANY(CAST(:stages AS text[]))")
            params["stages"] = [s.value for s in flt.stages]
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def _zone(r) -> Redevelopment:
    return Redevelopment(
        id=r["id"],
        area_name=r["area_name"],
        kind=RedevelopmentKind(r["kind"]),
        step_raw=r["step_raw"],
        stage=RedevelopmentStage(r["stage"]),
        stage_order=r["stage_order"],
        location=r["location"],
        location_kind=LocationKind(r["location_kind"]),
        lat=r["lat"],
        lng=r["lng"],
        location_precise=r["location_precise"],
        area_m2=r["area_m2"],
        households=r["households"],
        buildings=r["buildings"],
        union_members=r["union_members"],
        floor_area_ratio=r["floor_area_ratio"],
        building_coverage_ratio=r["building_coverage_ratio"],
        scale=r["scale"],
        business_entity=r["business_entity"],
        contractor=r["contractor"],
        engineer=r["engineer"],
        architect=r["architect"],
        specialists=r["specialists"],
        tel=r["tel"],
        images=dict(r["images"] or {}),
        first_seen_at=r["first_seen_at"],
        last_seen_at=r["last_seen_at"],
        closed_at=r["closed_at"],
    )

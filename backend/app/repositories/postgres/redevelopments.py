"""정비사업 구역 조회. 데이터는 `python -m app.ingestion redevelopments`(worker가 매일)로 채운 테이블에만 있다."""

import json
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.geo import BBox
from app.repositories.types import (
    LocationKind,
    Redevelopment,
    RedevelopmentFilter,
    RedevelopmentKind,
    RedevelopmentPage,
    RedevelopmentStage,
    RedevelopmentZone,
)

# 좌표가 없으면 이어진 정비구역 경계의 대표점을 위치로 쓴다(docs/REDEVELOPMENT_ZONES.md "위치 보강")
_COLS = """
    r.id, r.area_name, r.kind, r.step_raw, r.stage, r.stage_order, r.location, r.location_kind,
    COALESCE(r.lat, ST_Y(z.label_point)) AS lat, COALESCE(r.lng, ST_X(z.label_point)) AS lng, r.location_precise,
    CASE WHEN r.geom IS NOT NULL THEN 'lot' WHEN z.zone_id IS NOT NULL THEN 'zone' ELSE 'none' END AS position_source,
    z.zone_id,
    r.area_m2, r.households, r.buildings, r.union_members, r.floor_area_ratio, r.building_coverage_ratio, r.scale,
    r.business_entity, r.contractor, r.engineer, r.architect, r.specialists, r.tel, r.images,
    r.first_seen_at, r.last_seen_at, r.closed_at
"""
# 조각이 여럿이면 가장 큰 조각을 대표로 쓴다
_FROM = """ FROM redevelopments r LEFT JOIN LATERAL (
    SELECT zone_id, label_point FROM redevelopment_zones
    WHERE redevelopment_id = r.id ORDER BY area_m2 DESC, zone_id LIMIT 1
) z ON true"""
# 사업이 많이 나간 구역부터. 모르는 단계(NULL)는 뒤로
_ORDER = "r.stage_order DESC NULLS LAST, r.area_name, r.id"

_ZONES_AS_OF = text("SELECT max(loaded_at) FROM redevelopment_zones")
# 줌에 맞춰 단순화한다(화면에서 1px보다 작은 굴곡은 버림). 이어진 우리 구역의 이름·종류·단계를 함께 준다
_ZONES_IN = text(
    """
    SELECT z.zone_id, z.layer, z.name, z.notice, z.designated_on,
           ST_AsGeoJSON(ST_Multi(ST_SimplifyPreserveTopology(z.geom, :tolerance)), 6) AS geometry,
           r.id AS redevelopment_id, r.area_name, r.kind, r.stage
    FROM redevelopment_zones z
    LEFT JOIN redevelopments r ON r.id = z.redevelopment_id AND r.closed_at IS NULL
    WHERE z.geom && ST_MakeEnvelope(:min_lng, :min_lat, :max_lng, :max_lat, 4326)
    ORDER BY z.area_m2 DESC, z.zone_id
    LIMIT :limit
    """
)

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

    async def zones_as_of(self) -> datetime | None:
        """정비구역 경계 파일을 실은 시각. 한 번도 안 실었으면 None."""
        async with self._engine.connect() as conn:
            return (await conn.execute(_ZONES_AS_OF)).scalar()

    async def zones_in(self, bbox: BBox, *, tolerance: float, limit: int) -> list[RedevelopmentZone]:
        params = {
            "min_lng": bbox.min_lng,
            "min_lat": bbox.min_lat,
            "max_lng": bbox.max_lng,
            "max_lat": bbox.max_lat,
            "tolerance": tolerance,
            "limit": limit,
        }
        async with self._engine.connect() as conn:
            rows = (await conn.execute(_ZONES_IN, params)).mappings().all()
        return [
            RedevelopmentZone(
                zone_id=r["zone_id"],
                layer=r["layer"],
                name=r["name"],
                notice=r["notice"],
                designated_on=r["designated_on"],
                geometry=json.loads(r["geometry"]),
                redevelopment_id=r["redevelopment_id"],
                area_name=r["area_name"],
                kind=RedevelopmentKind(r["kind"]) if r["kind"] else None,
                stage=RedevelopmentStage(r["stage"]) if r["stage"] else None,
            )
            for r in rows
        ]

    async def search(self, flt: RedevelopmentFilter, *, offset: int, limit: int) -> RedevelopmentPage:
        where, params = self._where(flt)
        async with self._engine.connect() as conn:
            total = (await conn.execute(text(f"SELECT count(*){_FROM}{where}"), params)).scalar_one()
            rows = await conn.execute(
                text(f"SELECT {_COLS}{_FROM}{where} ORDER BY {_ORDER} OFFSET :offset LIMIT :limit"),
                {**params, "offset": offset, "limit": limit},
            )
            return RedevelopmentPage([_zone(r) for r in rows.mappings()], total)

    async def get(self, zone_id: str) -> Redevelopment | None:
        async with self._engine.connect() as conn:
            row = (
                (await conn.execute(text(f"SELECT {_COLS}{_FROM} WHERE r.id = :id"), {"id": zone_id}))
                .mappings()
                .first()
            )
        return _zone(row) if row else None

    @staticmethod
    def _where(flt: RedevelopmentFilter) -> tuple[str, dict]:
        clauses: list[str] = []
        params: dict = {}
        if not flt.include_closed:
            clauses.append("r.closed_at IS NULL")
        if flt.bbox is not None:
            clauses.append(
                "COALESCE(r.geom, z.label_point) && ST_MakeEnvelope(:min_lng, :min_lat, :max_lng, :max_lat, 4326)"
            )
            b = flt.bbox
            params.update(min_lng=b.min_lng, min_lat=b.min_lat, max_lng=b.max_lng, max_lat=b.max_lat)
        if flt.kinds:
            clauses.append("r.kind = ANY(CAST(:kinds AS text[]))")
            params["kinds"] = [k.value for k in flt.kinds]
        if flt.stages:
            clauses.append("r.stage = ANY(CAST(:stages AS text[]))")
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
        position_source=r["position_source"],
        zone_id=r["zone_id"],
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

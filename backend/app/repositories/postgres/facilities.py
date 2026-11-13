"""학교·병원·학원 조회. 데이터는 `python -m app.ingestion facilities`(worker가 매일)로 채운 테이블에만 있다.

거리는 직선거리(m)다. 학교 목록은 **배정 학교(학구도)가 아니라 가까운 학교**다.
배정 초등학교는 `python -m app.ingestion school-zones`로 실은 학구도(통학구역)에서 따로 찾는다.
"""

import json
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.geo import BBox
from app.repositories.types import (
    SHARED_ZONE,
    ZONE,
    Academy,
    AcademyCategory,
    Hospital,
    HospitalTier,
    Nearby,
    School,
    SchoolLevel,
    SchoolZone,
    SchoolZoneInfo,
)

_LEVEL_BY_KIND = {
    "초등학교": SchoolLevel.elementary,
    "중학교": SchoolLevel.middle,
    "고등학교": SchoolLevel.high,
    "특수학교": SchoolLevel.special,
}
_KINDS_BY_LEVEL = {level: [k for k, v in _LEVEL_BY_KIND.items() if v == level] for level in SchoolLevel}

_CLASSES_BY_TIER: dict[HospitalTier, tuple[str, ...]] = {
    HospitalTier.general: ("상급종합", "종합병원"),
    HospitalTier.hospital: ("병원", "요양병원", "정신병원", "한방병원", "치과병원"),
    HospitalTier.clinic: ("의원",),
    HospitalTier.dental: ("치과의원",),
    HospitalTier.oriental: ("한의원",),
    HospitalTier.public: ("보건소", "보건지소", "보건진료소", "보건의료원"),
}
_TIER_BY_CLASS = {c: tier for tier, classes in _CLASSES_BY_TIER.items() for c in classes}
_KNOWN_CLASSES = list(_TIER_BY_CLASS)


_CATEGORY_BY_FIELD = {
    "입시.검정 및 보습": AcademyCategory.tutoring,
    "예능(대)": AcademyCategory.arts,
    "국제화": AcademyCategory.language,
    "독서실": AcademyCategory.reading_room,
}
_FIELDS_BY_CATEGORY = {c: [f for f, v in _CATEGORY_BY_FIELD.items() if v == c] for c in AcademyCategory}


def academy_category(field: str | None) -> AcademyCategory:
    return _CATEGORY_BY_FIELD.get(field or "", AcademyCategory.other)


def school_level(kind: str | None) -> SchoolLevel:
    return _LEVEL_BY_KIND.get(kind or "", SchoolLevel.other)


def hospital_tier(class_name: str | None) -> HospitalTier:
    return _TIER_BY_CLASS.get(class_name or "", HospitalTier.other)


_POINT = "ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)"
_DIST = f"round(ST_Distance(location::geography, {_POINT}::geography))::int"
# 개교 전 학교((가칭), 개교일이 미래)는 빼고, 폐교는 closed_at으로 빠진다
_SCHOOL_OPEN = "closed_at IS NULL AND location IS NOT NULL AND (opened_on IS NULL OR opened_on <= :today)"
_SCHOOL_COLS = (
    "id, name, kind, foundation, coed, address, ST_Y(location) AS lat, ST_X(location) AS lng, tel, homepage, opened_on"
)
_ACADEMY_COLS = "id, name, kind, field, course, address, ST_Y(location) AS lat, ST_X(location) AS lng, tel"
_HOSPITAL_COLS = "id, name, class_name, address, ST_Y(location) AS lat, ST_X(location) AS lng, tel, homepage, doctors"

_ZONE_INFO = text("SELECT max(base_date) AS base_date, max(loaded_at) AS loaded_at FROM school_zones")
# 좌표가 든 통학구역(경계 포함)의 배정 학교 중 가장 가까운 곳. 공동통학구역은 학교가 여럿이다
_ASSIGNED_ELEMENTARY = text(
    f"""
    SELECT {_SCHOOL_COLS}, {_DIST} AS distance_m FROM schools
    WHERE {_SCHOOL_OPEN} AND id IN (
        SELECT zs.school_id FROM school_zones z JOIN school_zone_schools zs USING (zone_id)
        WHERE ST_Intersects(z.geom, {_POINT})
    )
    ORDER BY distance_m, id LIMIT 1
    """
)
# 한 학교가 여러 구역에 걸치면 합친다. 하나라도 공동통학구역이면 공동통학구역
_SCHOOL_ZONE = text(
    """
    SELECT ST_AsGeoJSON(ST_Union(z.geom), 6) AS geometry, bool_or(z.kind = :shared) AS shared
    FROM school_zone_schools zs JOIN school_zones z USING (zone_id)
    WHERE zs.school_id = :school_id
    """
)

_DATA_AS_OF = text(
    """
    SELECT max(finished_at) FROM etl_runs
    WHERE job = 'facility' AND status IN ('success', 'partial', 'quota_exceeded')
    """
)


class PostgresFacilityRepository:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def data_as_of(self) -> datetime | None:
        async with self._engine.connect() as conn:
            return (await conn.execute(_DATA_AS_OF)).scalar()

    async def school_zone_info(self) -> SchoolZoneInfo | None:
        """학구도를 실었는지. 안 실었으면 None."""
        async with self._engine.connect() as conn:
            return await self._zone_info(conn)

    @staticmethod
    async def _zone_info(conn) -> SchoolZoneInfo | None:
        row = (await conn.execute(_ZONE_INFO)).mappings().one()
        return SchoolZoneInfo(row["base_date"], row["loaded_at"]) if row["loaded_at"] else None

    async def school_zone(self, school_id: str) -> SchoolZone | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(_SCHOOL_ZONE, {"school_id": school_id, "shared": SHARED_ZONE})).mappings().one()
        if row["geometry"] is None:
            return None
        return SchoolZone(school_id, SHARED_ZONE if row["shared"] else ZONE, json.loads(row["geometry"]))

    async def nearby(
        self,
        lat: float,
        lng: float,
        *,
        today: date,
        school_radius_m: int,
        schools_per_level: int,
        hospital_radius_m: int,
        nearest_hospitals: int,
        academy_radius_m: int,
    ) -> Nearby:
        point = {"lat": lat, "lng": lng}
        async with self._engine.connect() as conn:
            schools: dict[SchoolLevel, list[School]] = {}
            for level in (SchoolLevel.elementary, SchoolLevel.middle, SchoolLevel.high):
                rows = await conn.execute(
                    text(
                        f"""
                        SELECT {_SCHOOL_COLS}, {_DIST} AS distance_m FROM schools
                        WHERE {_SCHOOL_OPEN} AND kind = ANY(CAST(:kinds AS text[]))
                          AND ST_DWithin(location::geography, {_POINT}::geography, :radius)
                        ORDER BY distance_m, id LIMIT :limit
                        """
                    ),
                    {
                        **point,
                        "today": today,
                        "kinds": _KINDS_BY_LEVEL[level],
                        "radius": school_radius_m,
                        "limit": schools_per_level,
                    },
                )
                schools[level] = [_school(r) for r in rows.mappings()]

            within = f"closed_at IS NULL AND ST_DWithin(location::geography, {_POINT}::geography, :radius)"
            counts_rows = await conn.execute(
                text(f"SELECT class_name, count(*) FROM hospitals WHERE {within} GROUP BY class_name"),
                {**point, "radius": hospital_radius_m},
            )
            counts = {tier: 0 for tier in HospitalTier}
            for class_name, n in counts_rows.all():
                counts[hospital_tier(class_name)] += n

            general = (
                (
                    await conn.execute(
                        text(
                            f"""
                        SELECT {_HOSPITAL_COLS}, {_DIST} AS distance_m FROM hospitals
                        WHERE closed_at IS NULL AND location IS NOT NULL AND class_name = ANY(CAST(:classes AS text[]))
                        ORDER BY distance_m, id LIMIT 1
                        """
                        ),
                        {**point, "classes": list(_CLASSES_BY_TIER[HospitalTier.general])},
                    )
                )
                .mappings()
                .first()
            )
            nearest = await conn.execute(
                text(
                    f"""
                    SELECT * FROM (
                        SELECT {_HOSPITAL_COLS}, {_DIST} AS distance_m FROM hospitals
                        WHERE closed_at IS NULL AND location IS NOT NULL
                        ORDER BY location <-> {_POINT} LIMIT :candidates
                    ) near ORDER BY distance_m, id LIMIT :limit
                    """
                ),
                # 각도(<->) 순서와 미터 순서가 조금 달라서, 넉넉히 뽑은 뒤 실제 거리로 다시 정렬한다
                {**point, "limit": nearest_hospitals, "candidates": nearest_hospitals * 4},
            )
            academy_rows = await conn.execute(
                text(f"SELECT field, count(*) FROM academies WHERE {within} GROUP BY field"),
                {**point, "radius": academy_radius_m},
            )
            academy_counts = {c: 0 for c in AcademyCategory}
            for field, n in academy_rows.all():
                academy_counts[academy_category(field)] += n
            zone_info = await self._zone_info(conn)
            assigned = None
            if zone_info is not None:
                found = (await conn.execute(_ASSIGNED_ELEMENTARY, {**point, "today": today})).mappings().first()
                assigned = _school(found) if found else None
            return Nearby(
                schools=schools,
                school_radius_m=school_radius_m,
                hospital_radius_m=hospital_radius_m,
                hospital_counts=counts,
                nearest_general=_hospital(general) if general else None,
                nearest_hospitals=[_hospital(r) for r in nearest.mappings()],
                academy_radius_m=academy_radius_m,
                academy_counts=academy_counts,
                zone_info=zone_info,
                assigned_elementary=assigned,
            )

    async def schools_in(self, bbox: BBox, levels: tuple[SchoolLevel, ...], *, today: date, limit: int) -> list[School]:
        where, params = self._box(bbox)
        if levels:
            kinds = [k for level in levels for k in _KINDS_BY_LEVEL[level]]
            other = SchoolLevel.other in levels
            where += " AND (kind = ANY(CAST(:kinds AS text[]))" + (
                " OR kind IS NULL OR NOT (kind = ANY(CAST(:known AS text[]))))" if other else ")"
            )
            params.update(kinds=kinds, known=list(_LEVEL_BY_KIND))
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                text(f"SELECT {_SCHOOL_COLS} FROM schools WHERE {_SCHOOL_OPEN} AND {where} ORDER BY id LIMIT :limit"),
                {**params, "today": today, "limit": limit},
            )
            return [_school(r) for r in rows.mappings()]

    async def hospitals_in(self, bbox: BBox, tiers: tuple[HospitalTier, ...], *, limit: int) -> list[Hospital]:
        where, params = self._box(bbox)
        if tiers:
            classes = [c for tier in tiers for c in _CLASSES_BY_TIER.get(tier, ())]
            other = HospitalTier.other in tiers
            where += " AND (class_name = ANY(CAST(:classes AS text[]))" + (
                " OR class_name IS NULL OR NOT (class_name = ANY(CAST(:known AS text[]))))" if other else ")"
            )
            params.update(classes=classes, known=_KNOWN_CLASSES)
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                text(
                    f"SELECT {_HOSPITAL_COLS} FROM hospitals WHERE closed_at IS NULL AND {where} "
                    "ORDER BY doctors DESC NULLS LAST, id LIMIT :limit"
                ),
                {**params, "limit": limit},
            )
            return [_hospital(r) for r in rows.mappings()]

    async def academies_in(self, bbox: BBox, categories: tuple[AcademyCategory, ...], *, limit: int) -> list[Academy]:
        where, params = self._box(bbox)
        if categories:
            fields = [f for c in categories for f in _FIELDS_BY_CATEGORY[c]]
            other = AcademyCategory.other in categories
            where += " AND (field = ANY(CAST(:fields AS text[]))" + (
                " OR field IS NULL OR NOT (field = ANY(CAST(:known AS text[]))))" if other else ")"
            )
            params.update(fields=fields, known=list(_CATEGORY_BY_FIELD))
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                text(
                    f"SELECT {_ACADEMY_COLS} FROM academies WHERE closed_at IS NULL AND {where} "
                    "ORDER BY id LIMIT :limit"
                ),
                {**params, "limit": limit},
            )
            return [_academy(r) for r in rows.mappings()]

    @staticmethod
    def _box(b: BBox) -> tuple[str, dict]:
        return (
            "location && ST_MakeEnvelope(:min_lng, :min_lat, :max_lng, :max_lat, 4326)",
            {"min_lng": b.min_lng, "min_lat": b.min_lat, "max_lng": b.max_lng, "max_lat": b.max_lat},
        )


def _school(r) -> School:
    return School(
        id=r["id"],
        name=r["name"],
        kind=r["kind"],
        level=school_level(r["kind"]),
        foundation=r["foundation"],
        coed=r["coed"],
        address=r["address"],
        lat=r["lat"],
        lng=r["lng"],
        tel=r["tel"],
        homepage=r["homepage"],
        opened_on=r["opened_on"],
        distance_m=r.get("distance_m"),
    )


def _hospital(r) -> Hospital:
    return Hospital(
        id=r["id"],
        name=r["name"],
        class_name=r["class_name"],
        tier=hospital_tier(r["class_name"]),
        address=r["address"],
        lat=r["lat"],
        lng=r["lng"],
        tel=r["tel"],
        homepage=r["homepage"],
        doctors=r["doctors"],
        distance_m=r.get("distance_m"),
    )


def _academy(r) -> Academy:
    return Academy(
        id=r["id"],
        name=r["name"],
        kind=r["kind"],
        field=r["field"],
        category=academy_category(r["field"]),
        course=r["course"],
        address=r["address"],
        lat=r["lat"],
        lng=r["lng"],
        tel=r["tel"],
        distance_m=r.get("distance_m"),
    )

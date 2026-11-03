"""부산 학교·병원·학원 목록을 원천에서 받아 `schools`·`hospitals`·`academies` 테이블에 맞춘다(하루 한 번, 호출 약 17회).

- 학교·학원은 좌표가 없어 도로명 주소를 카카오로 지오코딩한다.
  결과는 `geocode.json`에 쌓여 같은 주소를 다시 부르지 않는다(학원 첫 동기화만 약 7천 번).
- 병원은 원천 좌표를 쓰고, 좌표가 빠진 곳만 지오코딩한다.
- 목록을 끝까지 받은 쪽만 upsert하고, 그 목록에 없는 시설은 `closed_at`을 채운다(폐교·폐업).
  한쪽이 실패해도 다른 쪽은 진행한다.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.adapters.facilities import AcademyItem, HospitalItem, SchoolItem
from app.adapters.kakao import GeoResult
from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.ingestion.geocode import GeocodeCache
from app.ingestion.load import LoadBusy
from app.repositories.ports import SourceUnavailable

FACILITY_LOCK_KEY = 3800_0003
BUSAN_PREFIX = "26"


class SchoolSource(Protocol):
    calls_made: int

    async def schools(self) -> list[SchoolItem]: ...


class HospitalSource(Protocol):
    calls_made: int

    async def hospitals(self) -> list[HospitalItem]: ...


class AcademySource(Protocol):
    calls_made: int

    async def academies(self) -> list[AcademyItem]: ...


class AddressLookup(Protocol):
    async def lookup(self, query: str) -> GeoResult | None: ...


@dataclass
class FacilityReport:
    run_id: int
    status: str = "running"
    schools: int = 0
    hospitals: int = 0
    academies: int = 0
    inserted: int = 0
    updated: int = 0
    closed: int = 0
    without_location: int = 0
    errors: list[str] = field(default_factory=list)


def geocode_key(address: str) -> str:
    return f"facility|{address}"


_UPSERT_SCHOOL = text(
    """
    INSERT INTO schools (id, name, kind, foundation, coed, address, region_code, location, tel, homepage, opened_on,
                         last_seen_at, closed_at)
    VALUES (:id, :name, :kind, :foundation, :coed, :address, :region_code,
            ST_SetSRID(ST_MakePoint(CAST(:lng AS float8), CAST(:lat AS float8)), 4326),
            :tel, :homepage, :opened_on, :now, NULL)
    ON CONFLICT (id) DO UPDATE SET
        name = excluded.name, kind = excluded.kind, foundation = excluded.foundation, coed = excluded.coed,
        address = excluded.address,
        region_code = COALESCE(excluded.region_code, schools.region_code),
        location = COALESCE(excluded.location, schools.location),
        tel = excluded.tel, homepage = excluded.homepage, opened_on = excluded.opened_on,
        last_seen_at = excluded.last_seen_at, closed_at = NULL, updated_at = now()
    """
)

_UPSERT_HOSPITAL = text(
    """
    INSERT INTO hospitals (id, name, class_name, address, location, tel, homepage, doctors, opened_on,
                           last_seen_at, closed_at)
    VALUES (:id, :name, :class_name, :address,
            ST_SetSRID(ST_MakePoint(CAST(:lng AS float8), CAST(:lat AS float8)), 4326),
            :tel, :homepage, :doctors, :opened_on, :now, NULL)
    ON CONFLICT (id) DO UPDATE SET
        name = excluded.name, class_name = excluded.class_name, address = excluded.address,
        location = COALESCE(excluded.location, hospitals.location),
        tel = excluded.tel, homepage = excluded.homepage, doctors = excluded.doctors,
        opened_on = excluded.opened_on, last_seen_at = excluded.last_seen_at, closed_at = NULL, updated_at = now()
    """
)

_UPSERT_ACADEMY = text(
    """
    INSERT INTO academies (id, name, kind, field, course, address, region_code, location, tel, last_seen_at, closed_at)
    VALUES (:id, :name, :kind, :field, :course, :address, :region_code,
            ST_SetSRID(ST_MakePoint(CAST(:lng AS float8), CAST(:lat AS float8)), 4326),
            :tel, :now, NULL)
    ON CONFLICT (id) DO UPDATE SET
        name = excluded.name, kind = excluded.kind, field = excluded.field, course = excluded.course,
        address = excluded.address,
        region_code = COALESCE(excluded.region_code, academies.region_code),
        location = COALESCE(excluded.location, academies.location),
        tel = excluded.tel, last_seen_at = excluded.last_seen_at, closed_at = NULL, updated_at = now()
    """
)

_START_RUN = text(
    "INSERT INTO etl_runs (job, status, params) VALUES ('facility', 'running', CAST(:params AS jsonb)) RETURNING id"
)
_FINISH_RUN = text(
    """
    UPDATE etl_runs SET status = :status, finished_at = now(), calls_made = :calls, rows_fetched = :fetched,
        rows_inserted = :inserted, rows_updated = :updated, rows_deleted = :closed,
        report = CAST(:report AS jsonb), error = :error
    WHERE id = :id
    """
)


async def sync_facilities(
    engine: AsyncEngine,
    schools: SchoolSource | None,
    hospitals: HospitalSource | None,
    geocoder: AddressLookup | None,
    cache: GeocodeCache,
    *,
    academies: AcademySource | None = None,
    now: datetime,
    out: Callable[[str], None] = print,
) -> FacilityReport:
    """다른 시설 동기화가 돌고 있으면 기록을 남기지 않고 `LoadBusy`를 던진다."""
    async with engine.connect() as lock:
        if not (await lock.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": FACILITY_LOCK_KEY})).scalar():
            raise LoadBusy("다른 학교·병원 동기화가 이미 진행 중입니다. 끝난 뒤에 다시 실행하세요")
        await lock.commit()
        try:
            return await _sync(engine, schools, hospitals, academies, geocoder, cache, now=now, out=out)
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": FACILITY_LOCK_KEY})
            await lock.commit()


async def _sync(engine, schools, hospitals, academies, geocoder, cache, *, now, out) -> FacilityReport:
    params = {"schools": schools is not None, "hospitals": hospitals is not None, "academies": academies is not None}
    async with engine.begin() as conn:
        run_id = (await conn.execute(_START_RUN, {"params": json.dumps(params)})).scalar_one()
    report = FacilityReport(run_id)
    geo = _Geo(geocoder, cache, report)

    school_items = await _fetch("학교", schools.schools, report) if schools else None
    hospital_items = await _fetch("병원", hospitals.hospitals, report) if hospitals else None
    academy_items = await _fetch("학원", academies.academies, report) if academies else None
    report.schools = len(school_items or [])
    report.hospitals = len(hospital_items or [])
    report.academies = len(academy_items or [])
    out(f"  학교 {report.schools:,}곳 · 병원 {report.hospitals:,}곳 · 학원 {report.academies:,}곳")

    # 지오코딩(카카오 호출)은 트랜잭션 밖에서 먼저 끝낸다
    school_rows = [_school_row(s, await geo.find(s.address), now) for s in school_items or []]
    hospital_rows = []
    for h in hospital_items or []:
        point = (h.lat, h.lng) if h.lat and h.lng else None
        if point is None:
            found = await geo.find(h.address)
            point = (found.lat, found.lng) if found else None
        hospital_rows.append(_hospital_row(h, point, now))
    academy_rows = [_academy_row(a, await geo.find(a.road_address), now) for a in academy_items or []]
    cache.save()

    async with engine.begin() as conn:
        if school_items is not None:
            await _upsert(conn, "schools", _UPSERT_SCHOOL, school_rows, report, now)
        if hospital_items is not None:
            await _upsert(conn, "hospitals", _UPSERT_HOSPITAL, hospital_rows, report, now)
        if academy_items is not None:
            await _upsert(conn, "academies", _UPSERT_ACADEMY, academy_rows, report, now)

    calls = sum(s.calls_made for s in (schools, hospitals, academies) if s is not None)
    fetched_any = any(items is not None for items in (school_items, hospital_items, academy_items))
    if not fetched_any:
        report.status = "failed"
    elif report.errors:
        report.status = "quota_exceeded" if any("한도" in e for e in report.errors) else "partial"
    else:
        report.status = "success"
    async with engine.begin() as conn:
        await conn.execute(
            _FINISH_RUN,
            {
                "id": run_id,
                "status": report.status,
                "calls": calls,
                "fetched": report.schools + report.hospitals + report.academies,
                "inserted": report.inserted,
                "updated": report.updated,
                "closed": report.closed,
                "report": json.dumps(
                    {
                        "schools": report.schools,
                        "hospitals": report.hospitals,
                        "academies": report.academies,
                        "without_location": report.without_location,
                        "errors": report.errors[:20],
                    },
                    ensure_ascii=False,
                ),
                "error": "; ".join(report.errors[:3]) or None,
            },
        )
    return report


async def _fetch(name: str, fetch, report: FacilityReport):
    try:
        return await fetch()
    except (QuotaExceeded, SourceAuthError, SourceUnavailable) as e:
        report.errors.append(f"{name} 목록: {e}")
        return None


class _Geo:
    def __init__(self, geocoder: AddressLookup | None, cache: GeocodeCache, report: FacilityReport):
        self._geocoder, self._cache, self._report = geocoder, cache, report
        self._stopped = False

    async def find(self, address: str) -> GeoResult | None:
        query = address.split(",")[0].split("(")[0].strip()
        key = geocode_key(query)
        if query and key not in self._cache and self._geocoder is not None and not self._stopped:
            try:
                result = await self._geocoder.lookup(query)
            except (QuotaExceeded, SourceAuthError) as e:
                self._report.errors.append(f"지오코딩: {e}")
                self._stopped = True
            except SourceUnavailable:
                pass
            else:
                busan = result is not None and (result.b_code or "").startswith(BUSAN_PREFIX)
                self._cache.put(key, result if busan else None)
        found = self._cache.get(key)
        if found is None:
            self._report.without_location += 1
        return found


async def _upsert(conn: AsyncConnection, table: str, statement, rows: list[dict], report, now) -> None:
    existing = {r[0] for r in (await conn.execute(text(f"SELECT id FROM {table}"))).all()}  # noqa: S608
    if rows:
        await conn.execute(statement, rows)
    seen = [r["id"] for r in rows]
    report.inserted += sum(1 for i in seen if i not in existing)
    report.updated += sum(1 for i in seen if i in existing)
    closed = await conn.execute(
        text(
            f"UPDATE {table} SET closed_at = :now, updated_at = now() "  # noqa: S608
            "WHERE closed_at IS NULL AND NOT (id = ANY(CAST(:seen AS text[])))"
        ),
        {"now": now, "seen": seen},
    )
    report.closed += closed.rowcount


def _school_row(s: SchoolItem, geo: GeoResult | None, now: datetime) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "kind": s.kind,
        "foundation": s.foundation,
        "coed": s.coed,
        "address": s.address,
        "region_code": geo.b_code if geo else None,
        "lat": geo.lat if geo else None,
        "lng": geo.lng if geo else None,
        "tel": s.tel,
        "homepage": s.homepage,
        "opened_on": s.opened_on,
        "now": now,
    }


def _hospital_row(h: HospitalItem, point: tuple[float, float] | None, now: datetime) -> dict:
    return {
        "id": h.id,
        "name": h.name,
        "class_name": h.class_name,
        "address": h.address,
        "lat": point[0] if point else None,
        "lng": point[1] if point else None,
        "tel": h.tel,
        "homepage": h.homepage,
        "doctors": h.doctors,
        "opened_on": h.opened_on,
        "now": now,
    }


def _academy_row(a: AcademyItem, geo: GeoResult | None, now: datetime) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "kind": a.kind,
        "field": a.field,
        "course": a.course,
        "address": a.address,
        "region_code": geo.b_code if geo else None,
        "lat": geo.lat if geo else None,
        "lng": geo.lng if geo else None,
        "tel": a.tel,
        "now": now,
    }

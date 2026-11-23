"""부산 정비사업 구역을 원천에서 받아 `redevelopments` 테이블에 맞춘다(하루 한 번, 호출 약 4회).

1. 전체 목록을 받는다(350건 안팎). 구역명에 "테스트"가 들어간 운영 데이터의 시험 행은 버리고 건수만 센다.
2. 같은 ID(aCode)가 두 번 나오면 앞의 것만 쓰고 리포트에 남긴다.
3. 위치가 지번(`lot`)인 구역만 카카오로 지오코딩한다. 결과는 `geocode.json`에 쌓여 같은 주소를 다시 부르지 않는다.
   조합 사무실 주소(`office`)나 빈 위치(`none`)는 좌표를 비운다.
4. upsert하고, 이번 목록에 없는 구역은 `closed_at`을 채운다.
   **수집이 실패하거나 0건이면 아무것도 닫지 않는다**(일시 장애로 전체가 사라지지 않게). 그 실행은 `failed`다.
"""

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.kakao import GeoResult
from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.adapters.redevelopment import RedevelopmentItem
from app.ingestion.geocode import GeocodeCache
from app.ingestion.load import LoadBusy
from app.ingestion.redevelopment_zones import relink
from app.repositories.ports import SourceUnavailable
from app.repositories.types import LocationKind, RedevelopmentStage

REDEVELOPMENT_LOCK_KEY = 3800_0004
BUSAN_PREFIX = "26"
TEST_MARKER = "테스트"


class RedevelopmentSource(Protocol):
    calls_made: int

    async def zones(self) -> list[RedevelopmentItem]: ...


class AddressLookup(Protocol):
    async def lookup(self, query: str) -> GeoResult | None: ...


@dataclass
class RedevelopmentReport:
    run_id: int
    status: str = "running"
    fetched: int = 0  # 시험 행·중복을 뺀 구역 수
    skipped_test: int = 0
    duplicates: list[str] = field(default_factory=list)
    unknown_steps: dict[str, int] = field(default_factory=dict)  # 단계 표에 없는 원문 → 구역 수
    lot: int = 0
    office: int = 0
    none: int = 0
    without_location: int = 0
    inserted: int = 0
    updated: int = 0
    closed: int = 0
    errors: list[str] = field(default_factory=list)


def geocode_key(query: str) -> str:
    return f"redevelopment|{query}"


def is_test_zone(item: RedevelopmentItem) -> bool:
    return TEST_MARKER in item.area_name


_UPSERT = text(
    """
    INSERT INTO redevelopments (
        id, area_name, kind, step_raw, stage, stage_order, location, location_kind,
        lat, lng, location_precise, region_code, geom, office_geom, area_m2, households, buildings, union_members,
        floor_area_ratio, building_coverage_ratio, scale, business_entity, contractor, engineer, architect,
        specialists, tel, images, first_seen_at, last_seen_at, closed_at
    ) VALUES (
        :id, :area_name, :kind, :step_raw, :stage, :stage_order, :location, :location_kind,
        CAST(:lat AS float8), CAST(:lng AS float8), :precise, :region_code,
        ST_SetSRID(ST_MakePoint(CAST(:lng AS float8), CAST(:lat AS float8)), 4326),
        ST_SetSRID(ST_MakePoint(CAST(:office_lng AS float8), CAST(:office_lat AS float8)), 4326),
        :area_m2, :households, :buildings, :union_members,
        :floor_area_ratio, :building_coverage_ratio, :scale, :business_entity, :contractor, :engineer, :architect,
        :specialists, :tel, CAST(:images AS jsonb), :now, :now, NULL
    )
    ON CONFLICT (id) DO UPDATE SET
        area_name = excluded.area_name,
        kind = excluded.kind,
        step_raw = excluded.step_raw,
        -- 단계가 바뀐 경우에만 이전 단계와 바뀐 시각을 남긴다(같은 단계가 이어지면 기록을 그대로 둔다)
        previous_stage = CASE WHEN redevelopments.stage IS DISTINCT FROM excluded.stage
                              THEN redevelopments.stage ELSE redevelopments.previous_stage END,
        stage_changed_at = CASE WHEN redevelopments.stage IS DISTINCT FROM excluded.stage
                                THEN CAST(:now AS timestamptz) ELSE redevelopments.stage_changed_at END,
        stage = excluded.stage,
        stage_order = excluded.stage_order,
        location = excluded.location,
        location_kind = excluded.location_kind,
        -- 지오코딩을 이번에 못 해 봤으면(한도·장애) 위치 문구가 그대로일 때만 예전 좌표를 둔다.
        -- 문구가 바뀌었으면 옛 좌표는 틀린 자리라 비운다. 해 봤으면 결과(없으면 NULL)로 바꾼다
        lat = CASE WHEN CAST(:keep_location AS boolean)
                        AND redevelopments.location IS NOT DISTINCT FROM excluded.location
                   THEN redevelopments.lat ELSE excluded.lat END,
        lng = CASE WHEN CAST(:keep_location AS boolean)
                        AND redevelopments.location IS NOT DISTINCT FROM excluded.location
                   THEN redevelopments.lng ELSE excluded.lng END,
        location_precise = CASE WHEN CAST(:keep_location AS boolean)
                        AND redevelopments.location IS NOT DISTINCT FROM excluded.location
                                THEN redevelopments.location_precise ELSE excluded.location_precise END,
        -- 법정동 코드는 좌표와 같은 규칙을 따른다
        region_code = CASE WHEN CAST(:keep_location AS boolean)
                        AND redevelopments.location IS NOT DISTINCT FROM excluded.location
                           THEN redevelopments.region_code ELSE excluded.region_code END,
        geom = CASE WHEN CAST(:keep_location AS boolean)
                        AND redevelopments.location IS NOT DISTINCT FROM excluded.location
                    THEN redevelopments.geom ELSE excluded.geom END,
        -- 조합 사무실 좌표: 이번에 찾았으면 그것, 못 해 봤고 위치 문구가 그대로면 예전 값, 문구가 바뀌었으면 비운다
        office_geom = CASE WHEN excluded.office_geom IS NOT NULL THEN excluded.office_geom
                           WHEN redevelopments.location IS NOT DISTINCT FROM excluded.location
                           THEN redevelopments.office_geom END,
        area_m2 = excluded.area_m2,
        households = excluded.households,
        buildings = excluded.buildings,
        union_members = excluded.union_members,
        floor_area_ratio = excluded.floor_area_ratio,
        building_coverage_ratio = excluded.building_coverage_ratio,
        scale = excluded.scale,
        business_entity = excluded.business_entity,
        contractor = excluded.contractor,
        engineer = excluded.engineer,
        architect = excluded.architect,
        specialists = excluded.specialists,
        tel = excluded.tel,
        images = excluded.images,
        last_seen_at = excluded.last_seen_at,
        closed_at = NULL,
        updated_at = now()
    RETURNING (xmax = 0)
    """
)

_CLOSE = text(
    """
    UPDATE redevelopments SET closed_at = :now, updated_at = now()
    WHERE closed_at IS NULL AND NOT (id = ANY(CAST(:seen AS text[])))
    """
)

_START_RUN = text(
    "INSERT INTO etl_runs (job, status, params) VALUES ('redevelopment', 'running', CAST(:params AS jsonb)) "
    "RETURNING id"
)
_FINISH_RUN = text(
    """
    UPDATE etl_runs SET status = :status, finished_at = now(), calls_made = :calls, rows_fetched = :fetched,
        rows_inserted = :inserted, rows_updated = :updated, rows_deleted = :closed,
        report = CAST(:report AS jsonb), error = :error
    WHERE id = :id
    """
)


async def sync_redevelopments(
    engine: AsyncEngine,
    source: RedevelopmentSource,
    geocoder: AddressLookup | None,
    cache: GeocodeCache,
    *,
    now: datetime,
    out: Callable[[str], None] = print,
) -> RedevelopmentReport:
    """다른 정비사업 동기화가 돌고 있으면 기록을 남기지 않고 `LoadBusy`를 던진다."""
    async with engine.connect() as lock:
        if not (await lock.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": REDEVELOPMENT_LOCK_KEY})).scalar():
            raise LoadBusy("다른 정비사업 동기화가 이미 진행 중입니다. 끝난 뒤에 다시 실행하세요")
        await lock.commit()
        try:
            return await _sync(engine, source, geocoder, cache, now=now, out=out)
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": REDEVELOPMENT_LOCK_KEY})
            await lock.commit()


async def _sync(engine, source, geocoder, cache, *, now, out) -> RedevelopmentReport:
    async with engine.begin() as conn:
        run_id = (await conn.execute(_START_RUN, {"params": json.dumps({"region": BUSAN_PREFIX})})).scalar_one()
    report = RedevelopmentReport(run_id)

    fetched: list[RedevelopmentItem] | None
    try:
        fetched = await source.zones()
    except (QuotaExceeded, SourceAuthError, SourceUnavailable) as e:
        report.errors.append(f"구역 목록: {e}")
        fetched = None

    items = _clean(fetched or [], report)
    report.fetched = len(items)
    out(f"  정비구역 {report.fetched:,}곳 (시험 행 {report.skipped_test}건 제외, 중복 {len(report.duplicates)}건)")
    if fetched is not None and not items:
        report.errors.append("원천이 0건을 돌려줘 아무것도 닫지 않았습니다")

    if items:
        geo = _Geo(geocoder, cache, report)
        async with engine.begin() as conn:
            for item in items:
                found, tried = await geo.find(item)
                office = await geo.office(item)
                row = _row(item, found, keep_location=not tried, now=now, office=office)
                inserted = (await conn.execute(_UPSERT, row)).scalar_one()
                if inserted:
                    report.inserted += 1
                else:
                    report.updated += 1
            closed = await conn.execute(_CLOSE, {"now": now, "seen": [i.id for i in items]})
            report.closed = closed.rowcount
            # 새 구역·좌표 변화를 정비구역 경계 잇기에 반영한다(경계를 아직 안 실었으면 잇는 것이 없다)
            await relink(conn)
        cache.save()

    if not items:
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
                "calls": source.calls_made,
                "fetched": report.fetched,
                "inserted": report.inserted,
                "updated": report.updated,
                "closed": report.closed,
                "report": json.dumps(
                    {
                        "fetched": report.fetched,
                        "skipped_test": report.skipped_test,
                        "duplicates": report.duplicates[:50],
                        "unknown_steps": report.unknown_steps,
                        "location_kinds": {"lot": report.lot, "office": report.office, "none": report.none},
                        "without_location": report.without_location,
                        "errors": report.errors[:20],
                    },
                    ensure_ascii=False,
                ),
                "error": "; ".join(report.errors[:3]) or None,
            },
        )
    return report


def _clean(fetched: list[RedevelopmentItem], report: RedevelopmentReport) -> list[RedevelopmentItem]:
    """시험 행을 버리고, 같은 ID는 앞의 것만 남기고, 모르는 단계와 위치 종류를 센다."""
    items: dict[str, RedevelopmentItem] = {}
    unknown: Counter[str] = Counter()
    for item in fetched:
        if is_test_zone(item):
            report.skipped_test += 1
            continue
        if item.id in items:
            report.duplicates.append(item.id)
            continue
        items[item.id] = item
        if item.stage == RedevelopmentStage.unknown and item.step_raw:
            unknown[item.step_raw] += 1
        if item.location_kind == LocationKind.lot:
            report.lot += 1
        elif item.location_kind == LocationKind.office:
            report.office += 1
        else:
            report.none += 1
    report.unknown_steps = dict(unknown.most_common())
    return list(items.values())


class _Geo:
    def __init__(self, geocoder: AddressLookup | None, cache: GeocodeCache, report: RedevelopmentReport):
        self._geocoder, self._cache, self._report = geocoder, cache, report
        self._stopped = False

    async def office(self, item: RedevelopmentItem) -> GeoResult | None:
        """조합 사무실 좌표(건물까지 맞은 것만). 구역 위치가 아니라 정비구역 경계와 잇는 데에만 쓴다."""
        if not item.office_query:
            return None
        found = await self._lookup(item.office_query)
        return found if found is not None and found.precise else None

    async def _lookup(self, query: str) -> GeoResult | None:
        """캐시에 없으면 지오코딩해 넣는다(부산 밖·못 찾음은 None으로 기억). 한도·인증 오류면 이후 호출을 멈춘다."""
        key = geocode_key(query)
        if key not in self._cache and self._geocoder is not None and not self._stopped:
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
        return self._cache.get(key)

    async def find(self, item: RedevelopmentItem) -> tuple[GeoResult | None, bool]:
        """(결과, 이번에 위치를 정할 수 있었는지). 지번이 아니면 좌표 없음으로 정해진다."""
        query = item.geocode_query
        if item.location_kind != LocationKind.lot or not query:
            self._report.without_location += 1
            return None, True
        key = geocode_key(query)
        await self._lookup(query)
        tried = key in self._cache
        found = self._cache.get(key)
        if found is None:
            self._report.without_location += 1
        return found, tried


def _row(
    item: RedevelopmentItem,
    geo: GeoResult | None,
    *,
    keep_location: bool,
    now: datetime,
    office: GeoResult | None = None,
) -> dict:
    return {
        "office_lat": office.lat if office else None,
        "office_lng": office.lng if office else None,
        "id": item.id,
        "area_name": item.area_name,
        "kind": item.kind.value,
        "step_raw": item.step_raw,
        "stage": item.stage.value,
        "stage_order": item.stage_order,
        "location": item.location,
        "location_kind": item.location_kind.value,
        "lat": geo.lat if geo else None,
        "lng": geo.lng if geo else None,
        "precise": bool(geo and geo.precise),
        # 지번을 건물까지 맞게 찾은 구역만 법정동 코드를 갖는다(사무실 주소 구역은 NULL)
        "region_code": geo.b_code if geo and geo.precise else None,
        "keep_location": keep_location,
        "area_m2": item.area_m2,
        "households": item.households,
        "buildings": item.buildings,
        "union_members": item.union_members,
        "floor_area_ratio": item.floor_area_ratio,
        "building_coverage_ratio": item.building_coverage_ratio,
        "scale": item.scale,
        "business_entity": item.business_entity,
        "contractor": item.contractor,
        "engineer": item.engineer,
        "architect": item.architect,
        "specialists": item.specialists,
        "tel": item.tel,
        "images": json.dumps(item.images, ensure_ascii=False),
        "now": now,
    }

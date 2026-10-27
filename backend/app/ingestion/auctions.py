"""부산 경매·공매 물건을 경매마당 API에서 받아 `auctions` 테이블에 맞춘다(하루 한 번).

1. 진행 중 목록을 전부 받는다(경매 약 3,300건 → 34회, 공매 약 500건 → 6회).
2. 법원 경매는 새로 나왔거나 목록 값이 바뀐 물건만 상세를 받는다(상세 한도가 월 10만 회라 매일 전부는 못 받는다).
3. 주소를 카카오로 지오코딩한다. 결과는 실거래가와 같은 `geocode.json`에 쌓여 같은 주소를 다시 부르지 않는다.
4. upsert하고, 이번 목록에 없는 물건은 `closed_at`을 채운다(그 출처의 목록을 끝까지 받았을 때만).

개인정보는 어댑터(`app/adapters/madangs.py`)에서 이미 버려진 상태로 들어온다.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.kakao import GeoResult
from app.adapters.madangs import AuctionItem
from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.ingestion.geocode import GeocodeCache
from app.ingestion.load import LoadBusy
from app.repositories.ports import SourceUnavailable

AUCTION_LOCK_KEY = 3800_0002
BUSAN_PREFIX = "26"
DETAIL_CONCURRENCY = 4  # 경매마당 권장: 동시 조회 4개 이하


class AuctionSource(Protocol):
    calls_made: int

    async def court_list(self, region_prefix: str) -> list[AuctionItem]: ...

    async def court_detail(self, item: AuctionItem) -> AuctionItem: ...

    async def public_list(self, region_prefix: str) -> list[AuctionItem]: ...


class AddressLookup(Protocol):
    async def lookup(self, query: str) -> GeoResult | None: ...


@dataclass
class AuctionReport:
    run_id: int
    status: str = "running"
    court: int = 0
    public: int = 0
    details_fetched: int = 0
    details_failed: int = 0
    details_pending: int = 0  # 한도 때문에 다음 실행으로 미룬 상세
    without_location: int = 0
    inserted: int = 0
    updated: int = 0
    closed: int = 0
    errors: list[str] = field(default_factory=list)


def geocode_query(item: AuctionItem) -> str:
    """지번 주소가 있으면 그걸, 없으면 도로명 주소에서 동·호수(쉼표 뒤)를 떼고 쓴다."""
    if item.jibun_address:
        return item.jibun_address
    return item.address.split(",")[0].split("(")[0].strip()


def geocode_key(query: str) -> str:
    return f"auction|{query}"


_EXISTING = text("SELECT id, list_hash, detail_fetched_at IS NOT NULL, jibun_address FROM auctions")

_UPSERT = text(
    """
    INSERT INTO auctions (
        id, source, case_no, court_name, title, usage, address, jibun_address, region_code, pnu,
        location, location_precise, appraisal_price, min_price, deposit, fail_count, sale_date,
        bid_start_at, bid_end_at, land_area_m2, building_area_m2, is_share, status, schedule, results,
        list_hash, first_seen_at, last_seen_at, detail_fetched_at, closed_at
    ) VALUES (
        :id, :source, :case_no, :court_name, :title, :usage, :address, :jibun_address, :region_code, :pnu,
        ST_SetSRID(ST_MakePoint(CAST(:lng AS float8), CAST(:lat AS float8)), 4326), :precise,
        :appraisal_price, :min_price, :deposit, :fail_count, :sale_date,
        :bid_start_at, :bid_end_at, :land_area_m2, :building_area_m2, :is_share, :status,
        CAST(:schedule AS jsonb), CAST(:results AS jsonb),
        :list_hash, :now, :now, :detail_fetched_at, NULL
    )
    ON CONFLICT (id) DO UPDATE SET
        case_no = excluded.case_no,
        court_name = excluded.court_name,
        title = excluded.title,
        usage = excluded.usage,
        address = excluded.address,
        jibun_address = COALESCE(excluded.jibun_address, auctions.jibun_address),
        region_code = COALESCE(excluded.region_code, auctions.region_code),
        pnu = CASE WHEN excluded.location IS NULL THEN auctions.pnu ELSE excluded.pnu END,
        location = COALESCE(excluded.location, auctions.location),
        location_precise = CASE WHEN excluded.location IS NULL THEN auctions.location_precise
                                ELSE excluded.location_precise END,
        appraisal_price = excluded.appraisal_price,
        min_price = excluded.min_price,
        deposit = COALESCE(excluded.deposit, auctions.deposit),
        fail_count = excluded.fail_count,
        sale_date = excluded.sale_date,
        bid_start_at = excluded.bid_start_at,
        bid_end_at = excluded.bid_end_at,
        land_area_m2 = COALESCE(excluded.land_area_m2, auctions.land_area_m2),
        building_area_m2 = COALESCE(excluded.building_area_m2, auctions.building_area_m2),
        is_share = excluded.is_share,
        status = excluded.status,
        schedule = CASE WHEN CAST(:has_detail AS boolean) THEN excluded.schedule ELSE auctions.schedule END,
        results = CASE WHEN CAST(:has_detail AS boolean) THEN excluded.results ELSE auctions.results END,
        list_hash = excluded.list_hash,
        last_seen_at = excluded.last_seen_at,
        detail_fetched_at = COALESCE(excluded.detail_fetched_at, auctions.detail_fetched_at),
        closed_at = NULL,
        updated_at = now()
    RETURNING (xmax = 0)
    """
)

_CLOSE = text(
    """
    UPDATE auctions SET closed_at = :now, updated_at = now()
    WHERE source = :source AND closed_at IS NULL AND NOT (id = ANY(CAST(:seen AS text[])))
    """
)

_START_RUN = text(
    "INSERT INTO etl_runs (job, status, params) VALUES ('auction', 'running', CAST(:params AS jsonb)) RETURNING id"
)
_FINISH_RUN = text(
    """
    UPDATE etl_runs SET status = :status, finished_at = now(), calls_made = :calls, rows_fetched = :fetched,
        rows_inserted = :inserted, rows_updated = :updated, rows_deleted = :closed,
        report = CAST(:report AS jsonb), error = :error
    WHERE id = :id
    """
)


async def sync_auctions(
    engine: AsyncEngine,
    source: AuctionSource,
    geocoder: AddressLookup | None,
    cache: GeocodeCache,
    *,
    now: datetime,
    max_details: int | None = None,
    out: Callable[[str], None] = print,
) -> AuctionReport:
    """다른 경매 동기화가 돌고 있으면 기록을 남기지 않고 `LoadBusy`를 던진다."""
    async with engine.connect() as lock:
        if not (await lock.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": AUCTION_LOCK_KEY})).scalar():
            raise LoadBusy("다른 경매 동기화가 이미 진행 중입니다. 끝난 뒤에 다시 실행하세요")
        await lock.commit()
        try:
            return await _sync(engine, source, geocoder, cache, now=now, max_details=max_details, out=out)
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": AUCTION_LOCK_KEY})
            await lock.commit()


async def _sync(engine, source, geocoder, cache, *, now, max_details, out) -> AuctionReport:
    async with engine.begin() as conn:
        run_id = (await conn.execute(_START_RUN, {"params": json.dumps({"region": BUSAN_PREFIX})})).scalar_one()
    report = AuctionReport(run_id)
    fatal: Exception | None = None

    lists: dict[str, list[AuctionItem]] = {}
    for name, fetch in (("court", source.court_list), ("public", source.public_list)):
        try:
            lists[name] = await fetch(BUSAN_PREFIX)
        except (QuotaExceeded, SourceUnavailable) as e:
            report.errors.append(f"{name} 목록: {e}")
        except SourceAuthError as e:
            fatal = e
            report.errors.append(f"{name} 목록: {e}")
            break
    report.court, report.public = len(lists.get("court", [])), len(lists.get("public", []))
    out(f"  경매 {report.court:,}건 · 공매 {report.public:,}건")

    async with engine.connect() as conn:
        found = (await conn.execute(_EXISTING)).all()
    existing = {r[0]: (r[1], r[2]) for r in found}
    stored_jibun = {r[0]: r[3] for r in found if r[3]}

    fetched: dict[str, AuctionItem] = {}
    court = lists.get("court", [])
    stale = [i for i in court if i.id not in existing or existing[i.id] != (i.list_hash, True)]
    if max_details is not None:
        report.details_pending += max(0, len(stale) - max_details)
        stale = stale[:max_details]
    if stale and fatal is None:
        out(f"  상세 {len(stale):,}건 조회")
        await _fetch_details(source, stale, fetched, report, out)

    # 상세를 다시 받지 않은 물건은 저장해 둔 지번 주소로 지오코딩한다(캐시가 그대로 맞는다)
    court = [fetched.get(i.id) or replace(i, jibun_address=stored_jibun.get(i.id)) for i in court]
    items = court + lists.get("public", [])
    geo = await _geocode(geocoder, cache, items, report)

    wanted = {i.id for i in court if i.id not in existing or existing[i.id] != (i.list_hash, True)}
    async with engine.begin() as conn:
        for item in items:
            has_detail = item.source == "public" or item.id in fetched
            row = _row(item, geo.get(item.id), has_detail, now)
            if item.id in wanted and not has_detail:
                # 상세를 못 받았으면 예전 지문을 남겨 다음 실행에서 다시 받는다
                row["list_hash"] = existing[item.id][0] if item.id in existing else ""
            inserted = (await conn.execute(_UPSERT, row)).scalar_one()
            if inserted:
                report.inserted += 1
            else:
                report.updated += 1
        for name, seen in lists.items():
            closed = await conn.execute(_CLOSE, {"now": now, "source": name, "seen": [i.id for i in seen]})
            report.closed += closed.rowcount

    if fatal or len(lists) == 0:
        report.status = "failed"
    elif report.errors or report.details_failed or report.details_pending:
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
                "fetched": len(items),
                "inserted": report.inserted,
                "updated": report.updated,
                "closed": report.closed,
                "report": json.dumps(
                    {
                        "court": report.court,
                        "public": report.public,
                        "details_fetched": report.details_fetched,
                        "details_failed": report.details_failed,
                        "details_pending": report.details_pending,
                        "without_location": report.without_location,
                        "errors": report.errors[:20],
                    },
                    ensure_ascii=False,
                ),
                "error": "; ".join(report.errors[:3]) or None,
            },
        )
    return report


async def _fetch_details(source, stale, fetched, report, out) -> None:
    gate = asyncio.Semaphore(DETAIL_CONCURRENCY)
    stop: list[str] = []

    async def one(item: AuctionItem) -> None:
        async with gate:
            if stop:
                report.details_pending += 1
                return
            try:
                fetched[item.id] = await source.court_detail(item)
                report.details_fetched += 1
                if report.details_fetched % 500 == 0:
                    out(f"  상세 {report.details_fetched:,}건")
            except (QuotaExceeded, SourceAuthError) as e:
                if not stop:
                    stop.append(str(e))
                    report.errors.append(f"상세: {e}")
                report.details_pending += 1
            except SourceUnavailable:
                report.details_failed += 1

    await asyncio.gather(*(one(i) for i in stale))


async def _geocode(geocoder, cache: GeocodeCache, items, report) -> dict[str, GeoResult]:
    found: dict[str, GeoResult] = {}
    stopped = False
    for item in items:
        key = geocode_key(geocode_query(item))
        if key not in cache and geocoder is not None and not stopped:
            try:
                result = await geocoder.lookup(geocode_query(item))
            except (QuotaExceeded, SourceAuthError) as e:
                report.errors.append(f"지오코딩: {e}")
                stopped = True
            except SourceUnavailable:
                pass
            else:
                cache.put(key, result if result and (result.b_code or "").startswith(BUSAN_PREFIX) else None)
        result = cache.get(key)
        if result:
            found[item.id] = result
        else:
            report.without_location += 1
    cache.save()
    return found


def _row(item: AuctionItem, geo: GeoResult | None, has_detail: bool, now: datetime) -> dict:
    return {
        "id": item.id,
        "source": item.source,
        "case_no": item.case_no,
        "court_name": item.court_name,
        "title": item.title,
        "usage": item.usage,
        "address": item.address,
        "jibun_address": item.jibun_address,
        "region_code": geo.b_code if geo else None,
        "pnu": geo.pnu if geo else None,
        "lat": geo.lat if geo else None,
        "lng": geo.lng if geo else None,
        "precise": bool(geo and geo.precise),
        "appraisal_price": item.appraisal_price,
        "min_price": item.min_price,
        "deposit": item.deposit,
        "fail_count": item.fail_count,
        "sale_date": item.sale_date,
        "bid_start_at": item.bid_start_at,
        "bid_end_at": item.bid_end_at,
        "land_area_m2": item.land_area_m2,
        "building_area_m2": item.building_area_m2,
        "is_share": item.is_share,
        "status": item.status,
        "schedule": json.dumps(list(item.schedule), ensure_ascii=False),
        "results": json.dumps(list(item.results), ensure_ascii=False),
        "has_detail": has_detail,
        "list_hash": item.list_hash,
        "detail_fetched_at": now if item.source == "court" and has_detail else None,
        "now": now,
    }

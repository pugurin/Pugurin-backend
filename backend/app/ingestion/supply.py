"""아파트 평형별 공급면적을 건축물대장에서 받아 `complex_area_types.supply_area_pyeong`을 채운다.

- 단지(PNU)마다 전유공용면적을 앞 페이지부터 받다가, 그 단지의 평형이 모두 표본을 얻으면 멈춘다(최대 `max_pages`).
- 하루 호출 한도(공공데이터포털 개발계정 1만 회) 안에서 `max_calls`만큼만 쓰고, 나머지 단지는 다음 날 이어서 받는다.
  처음 보는 단지부터, 그다음 공급면적이 빈 평형이 있는 단지를 `RECHECK_DAYS`가 지나면 다시 본다.
- 산출하지 못한 평형은 null로 둔다. 가짜 값을 채우지 않는다.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.building import supply_pyeong, supply_samples
from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.ingestion.load import LoadBusy
from app.repositories.ports import SourceUnavailable

SUPPLY_LOCK_KEY = 3800_0004
SUPPLY_SOURCE = "building_register"
RECHECK_DAYS = 30
MAX_PAGES = 8
CONCURRENCY = 4


class UnitAreaSource(Protocol):
    calls_made: int

    async def unit_areas(self, pnu: str, page: int) -> tuple[list[dict], int]: ...


@dataclass
class SupplyReport:
    run_id: int
    status: str = "running"
    complexes: int = 0
    area_types_filled: int = 0
    area_types_missing: int = 0
    failed: int = 0
    calls: int = 0
    errors: list[str] = field(default_factory=list)


_TODO = text(
    """
    SELECT c.id, c.pnu, array_agg(t.area_bucket ORDER BY t.area_bucket) AS buckets,
           array_agg(t.area_bucket ORDER BY t.area_bucket) FILTER (WHERE t.supply_area_pyeong IS NULL) AS missing
    FROM complexes c
    JOIN complex_area_types t ON t.complex_id = c.id
    WHERE c.property_type = 'apartment' AND c.pnu IS NOT NULL
    GROUP BY c.id
    HAVING c.supply_checked_at IS NULL
        OR (bool_or(t.supply_area_pyeong IS NULL) AND c.supply_checked_at < :recheck_before)
    ORDER BY c.supply_checked_at NULLS FIRST,
             (SELECT count(*) FROM transactions x WHERE x.complex_id = c.id AND x.deleted_at IS NULL) DESC,
             c.id
    """
)

_SET_SUPPLY = text(
    """
    UPDATE complex_area_types SET supply_area_pyeong = :pyeong, supply_source = :source
    WHERE complex_id = :complex_id AND area_bucket = :bucket
    """
)
_CHECKED = text("UPDATE complexes SET supply_checked_at = :now WHERE id = :complex_id")
_START_RUN = text(
    "INSERT INTO etl_runs (job, status, params) VALUES ('building', 'running', CAST(:params AS jsonb)) RETURNING id"
)
_FINISH_RUN = text(
    """
    UPDATE etl_runs SET status = :status, finished_at = now(), calls_made = :calls, rows_fetched = :complexes,
        rows_updated = :filled, report = CAST(:report AS jsonb), error = :error
    WHERE id = :id
    """
)


async def sync_supply_areas(
    engine: AsyncEngine,
    source: UnitAreaSource,
    *,
    now: datetime,
    max_calls: int,
    max_pages: int = MAX_PAGES,
    out: Callable[[str], None] = print,
) -> SupplyReport:
    """다른 공급면적 동기화가 돌고 있으면 기록을 남기지 않고 `LoadBusy`를 던진다."""
    async with engine.connect() as lock:
        if not (await lock.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": SUPPLY_LOCK_KEY})).scalar():
            raise LoadBusy("다른 공급면적 동기화가 이미 진행 중입니다. 끝난 뒤에 다시 실행하세요")
        await lock.commit()
        try:
            return await _sync(engine, source, now=now, max_calls=max_calls, max_pages=max_pages, out=out)
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SUPPLY_LOCK_KEY})
            await lock.commit()


async def _sync(engine, source, *, now, max_calls, max_pages, out) -> SupplyReport:
    params = {"max_calls": max_calls, "max_pages": max_pages}
    async with engine.begin() as conn:
        run_id = (await conn.execute(_START_RUN, {"params": json.dumps(params)})).scalar_one()
        todo = (await conn.execute(_TODO, {"recheck_before": now - timedelta(days=RECHECK_DAYS)})).all()
    report = SupplyReport(run_id)
    out(f"  공급면적을 볼 아파트 단지 {len(todo):,}곳 (오늘 호출 한도 {max_calls:,}회)")
    start_calls = source.calls_made
    stopped: list[str] = []

    async def one(complex_id, pnu: str, buckets: list[int], missing: list[int] | None) -> None:
        wanted = set(missing or buckets)
        pages: dict[int, list[dict]] = {}
        last = 1
        try:
            page = 1
            while page <= min(last, max_pages):
                if stopped or source.calls_made - start_calls >= max_calls:
                    return  # 오늘 몫을 다 썼다. 확인 시각을 남기지 않아 내일 이어서 본다
                rows, last = await source.unit_areas(pnu, page)
                pages[page] = rows
                if wanted <= set(supply_samples(pages, last)):
                    break
                page += 1
        except (QuotaExceeded, SourceAuthError) as e:
            stopped.append(str(e))
            return
        except SourceUnavailable as e:
            report.failed += 1
            if len(report.errors) < 20:
                report.errors.append(f"{pnu}: {e}")
            return
        samples = supply_samples(pages, last)
        found = {b: p for b in wanted if (p := supply_pyeong(samples.get(b, []))) is not None}
        async with engine.begin() as conn:
            for bucket, pyeong in found.items():
                await conn.execute(
                    _SET_SUPPLY, {"complex_id": complex_id, "bucket": bucket, "pyeong": pyeong, "source": SUPPLY_SOURCE}
                )
            await conn.execute(_CHECKED, {"complex_id": complex_id, "now": now})
        report.complexes += 1
        report.area_types_filled += len(found)
        report.area_types_missing += len(wanted) - len(found)

    for i in range(0, len(todo), CONCURRENCY):
        if stopped or source.calls_made - start_calls >= max_calls:
            break
        await asyncio.gather(*(one(*row) for row in todo[i : i + CONCURRENCY]))

    report.calls = source.calls_made - start_calls
    report.errors = stopped[:1] + report.errors
    if stopped:
        report.status = "quota_exceeded" if any("한도" in e for e in stopped) else "failed"
    elif report.failed:
        report.status = "partial"
    else:
        report.status = "success"
    async with engine.begin() as conn:
        await conn.execute(
            _FINISH_RUN,
            {
                "id": run_id,
                "status": report.status,
                "calls": report.calls,
                "complexes": report.complexes,
                "filled": report.area_types_filled,
                "report": json.dumps(
                    {
                        "todo": len(todo),
                        "complexes": report.complexes,
                        "filled": report.area_types_filled,
                        "missing": report.area_types_missing,
                        "failed": report.failed,
                        "errors": report.errors[:20],
                    },
                    ensure_ascii=False,
                ),
                "error": "; ".join(report.errors[:3]) or None,
            },
        )
    return report

"""수집 캐시(.cache)를 PostgreSQL/PostGIS에 적재한다 (#38, docs/ERD.md 3장).

- 거래는 `source_key`로 멱등 upsert한다. id는 파일 캐시 빌드(build.py)와 같은 uuid5라 앱이 들고 있는 id가 바뀌지 않는다.
- (종류, 시군구, 계약월) 단위로 커밋한다. 다시 받은 단위에서 사라진 행은 지우지 않고 `deleted_at`을 채운다.
- 원천 값은 그대로 저장한다. 지분거래·도로·갱신 계약을 통계에서 빼는 일은 조회·집계 단계가 한다.
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.adapters.molit.types import RawTrade
from app.core.busan import SIGUNGU, SIGUNGU_BY_CODE
from app.core.text import normalize
from app.ingestion.build import GeoIndex
from app.ingestion.collect import Window
from app.ingestion.rawcache import RawCache
from app.repositories.types import DealType, PropertyType

# 적재는 한 번에 하나만 돈다. 둘이 같은 단지·거래를 upsert하면 deadlock으로 단위가 실패한다.
LOAD_LOCK_KEY = 3800_0001


class LoadBusy(RuntimeError):
    """다른 적재(worker 또는 수동 `load`)가 이미 돌고 있다."""


@dataclass
class LoadReport:
    run_id: int
    status: str = "running"
    windows_loaded: int = 0
    windows_skipped: int = 0
    windows_failed: list[Window] = field(default_factory=list)
    rows_fetched: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_cancelled: int = 0
    rows_deleted: int = 0
    unmapped_regions: set[tuple[str, str]] = field(default_factory=set)
    unmapped_rows: int = 0


@dataclass(frozen=True)
class _Regions:
    by_name: dict[tuple[str, str], str]  # (시군구 코드, 법정동 이름) → 법정동 코드
    codes: set[str]
    centroid: dict[str, tuple[float, float]]  # 코드 → (lat, lng). 동 중심점이 없으면 시군구 중심점
    names: dict[str, str]  # 코드 → 마스터 이름

    def code_for(self, t: RawTrade) -> str | None:
        # 아파트 매매는 응답의 법정동 코드를 먼저 쓴다(파일 캐시 빌드와 같은 규칙)
        if t.umd_cd and (t.sgg_cd + t.umd_cd) in self.codes:
            return t.sgg_cd + t.umd_cd
        return self.by_name.get((t.sgg_cd, t.umd_nm))


def _month_range(ym: str) -> tuple[date, date]:
    start = date(int(ym[:4]), int(ym[4:]), 1)
    end = date(start.year + start.month // 12, start.month % 12 + 1, 1)
    return start, end


def complex_key(t: RawTrade) -> tuple:
    """파일 캐시 빌드(build.py)와 같은 단지 키. 아파트는 aptSeq, 나머지는 지번 + 정규화한 이름."""
    if t.complex_seq:
        return ("seq", t.complex_seq)
    return (t.property_type.value, t.sgg_cd, t.umd_nm, t.jibun, normalize(t.name or ""))


def complex_id(key: tuple) -> UUID:
    return uuid5(NAMESPACE_URL, f"pugurin:complex:{key}")


def transaction_id(source_key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"pugurin:tx:{source_key}")


def _renewal_right(value: str | None) -> bool | None:
    return True if value == "사용" else None


# ── SQL ──────────────────────────────────────────────────────

# 이름·위치는 처음 본 거래 기준이다(파일 캐시 빌드와 같음). 나중에 지오코딩이 되면 중심점만 정확한 위치로 바꾼다
_UPSERT_COMPLEXES = text(
    """
    INSERT INTO complexes (id, property_type, name, name_key, source_seq, region_code, jibun, pnu, location,
                           location_source)
    SELECT u.id, u.property_type, u.name, u.name_key, u.source_seq, u.region_code, u.jibun, u.pnu,
           ST_SetSRID(ST_MakePoint(u.lng, u.lat), 4326), u.location_source
    FROM unnest(CAST(:id AS uuid[]), CAST(:property_type AS text[]), CAST(:name AS text[]),
                CAST(:name_key AS text[]), CAST(:source_seq AS text[]), CAST(:region_code AS text[]),
                CAST(:jibun AS text[]), CAST(:pnu AS text[]), CAST(:lat AS float8[]), CAST(:lng AS float8[]),
                CAST(:location_source AS text[]))
         AS u(id, property_type, name, name_key, source_seq, region_code, jibun, pnu, lat, lng, location_source)
    ON CONFLICT (id) DO UPDATE SET
        pnu = excluded.pnu,
        location = excluded.location,
        location_source = excluded.location_source
    WHERE complexes.location_source = 'centroid' AND excluded.location_source = 'geocode'
    """
)

_TX_COLUMNS = (
    ("id", "uuid"),
    ("source_key", "text"),
    ("source_kind", "text"),
    ("property_type", "text"),
    ("deal_type", "text"),
    ("complex_id", "uuid"),
    ("region_code", "text"),
    ("jibun", "text"),
    ("pnu", "text"),
    ("lat", "float8"),
    ("lng", "float8"),
    ("location_precision", "text"),
    ("price", "int8"),
    ("deposit", "int8"),
    ("monthly_rent", "int8"),
    ("exclusive_area_m2", "float8"),
    ("land_area_m2", "float8"),
    ("floor", "int4"),
    ("build_year", "int4"),
    ("contract_date", "date"),
    ("trade_method", "text"),
    ("is_cancelled", "bool"),
    ("cancelled_at", "date"),
    ("registered_date", "date"),
    ("is_share_deal", "bool"),
    ("jimok", "text"),
    ("land_use", "text"),
    ("house_type", "text"),
    ("contract_type", "text"),
    ("contract_term", "text"),
    ("use_renewal_right", "bool"),
)

# 다시 받았을 때 "바뀌었다"고 볼 값. 바뀐 행만 updated_at을 올려 실제 변경 건수를 센다
_MUTABLE = (
    "complex_id",
    "region_code",
    "jibun",
    "pnu",
    "location",
    "location_precision",
    "price",
    "deposit",
    "monthly_rent",
    "exclusive_area_m2",
    "land_area_m2",
    "floor",
    "build_year",
    "trade_method",
    "is_cancelled",
    "cancelled_at",
    "registered_date",
    "is_share_deal",
    "jimok",
    "land_use",
    "house_type",
    "contract_type",
    "contract_term",
    "use_renewal_right",
)
_STORED = [c for c, _ in _TX_COLUMNS if c not in ("lat", "lng")]
_SELECT = {
    "location": "ST_SetSRID(ST_MakePoint(u.lng, u.lat), 4326)",
    "exclusive_area_m2": "u.exclusive_area_m2::numeric(10, 4)",
    "land_area_m2": "u.land_area_m2::numeric(14, 4)",
    "floor": "u.floor::smallint",
    "build_year": "u.build_year::smallint",
}
_changed = " OR ".join(f"transactions.{c} IS DISTINCT FROM excluded.{c}" for c in _MUTABLE)

_UPSERT_TRANSACTIONS = text(
    f"""
    INSERT INTO transactions ({", ".join(_STORED[:8])}, location, {", ".join(_STORED[8:])},
                              first_seen_run_id, last_seen_run_id)
    SELECT {", ".join(_SELECT.get(c, f"u.{c}") for c in _STORED[:8])}, {_SELECT["location"]},
           {", ".join(_SELECT.get(c, f"u.{c}") for c in _STORED[8:])}, :run_id, :run_id
    FROM unnest({", ".join(f"CAST(:{c} AS {t}[])" for c, t in _TX_COLUMNS)})
         AS u({", ".join(c for c, _ in _TX_COLUMNS)})
    ON CONFLICT (source_key) DO UPDATE SET
        {", ".join(f"{c} = excluded.{c}" for c in _MUTABLE)},
        last_seen_run_id = excluded.last_seen_run_id,
        deleted_at = NULL,
        updated_at = CASE WHEN {_changed} OR transactions.deleted_at IS NOT NULL
                          THEN now() ELSE transactions.updated_at END
    RETURNING source_key, updated_at = now() AS touched
    """
)

_STORED_COMPLEXES = text(
    "SELECT id, ST_Y(location) AS lat, ST_X(location) AS lng, pnu, location_source FROM complexes "
    "WHERE id = ANY(CAST(:ids AS uuid[]))"
)

_EXISTING = text("SELECT source_key, is_cancelled FROM transactions WHERE source_key = ANY(CAST(:keys AS text[]))")

_MARK_DELETED = text(
    """
    UPDATE transactions SET deleted_at = now(), updated_at = now()
    WHERE source_kind = :kind AND left(region_code, 5) = :sgg
      AND contract_date >= :start AND contract_date < :end
      AND deleted_at IS NULL AND last_seen_run_id <> :run_id
    """
)

_LOG_WINDOW = text(
    """
    INSERT INTO source_fetch_log (source_kind, sgg_cd, deal_ym, run_id, total_count, fetched_at)
    VALUES (:kind, :sgg, :ym, :run_id, :total, :fetched_at)
    ON CONFLICT (source_kind, sgg_cd, deal_ym) DO UPDATE SET
        run_id = excluded.run_id, total_count = excluded.total_count, fetched_at = excluded.fetched_at
    """
)

_AREA_TYPES = (
    # 나중에 지오코딩된 단지(중심점 → 정확한 위치)는 이전 단위의 거래 위치도 맞춘다
    text(
        """
        UPDATE transactions t
        SET location = c.location, pnu = c.pnu, updated_at = now(),
            location_precision = CASE WHEN c.location_source = 'geocode' THEN 'parcel' ELSE 'dong' END
        FROM complexes c
        WHERE t.complex_id = c.id AND (t.location, t.pnu) IS DISTINCT FROM (c.location, c.pnu)
        """
    ),
    text(
        """
        INSERT INTO complex_area_types (complex_id, area_bucket, exclusive_area_m2)
        SELECT complex_id, floor(exclusive_area_m2 + 0.5)::smallint,
               round(round(percentile_cont(0.5) WITHIN GROUP (ORDER BY exclusive_area_m2::float8)::numeric, 6), 2)
        FROM transactions
        WHERE complex_id IS NOT NULL AND deleted_at IS NULL AND exclusive_area_m2 IS NOT NULL
        GROUP BY 1, 2
        ON CONFLICT (complex_id, area_bucket) DO UPDATE SET exclusive_area_m2 = excluded.exclusive_area_m2
        """
    ),
    text(
        """
        DELETE FROM complex_area_types a
        WHERE NOT EXISTS (
            SELECT 1 FROM transactions t
            WHERE t.complex_id = a.complex_id AND t.deleted_at IS NULL
              AND floor(t.exclusive_area_m2 + 0.5) = a.area_bucket
        )
        """
    ),
    text(
        """
        UPDATE complexes c SET build_year = s.build_year
        FROM (
            SELECT complex_id, mode() WITHIN GROUP (ORDER BY build_year) AS build_year
            FROM transactions
            WHERE complex_id IS NOT NULL AND deleted_at IS NULL AND build_year IS NOT NULL
            GROUP BY 1
        ) s
        WHERE c.id = s.complex_id AND c.build_year IS DISTINCT FROM s.build_year
        """
    ),
)


_START_RUN = text(
    "INSERT INTO etl_runs (job, status, params) VALUES (:job, 'running', CAST(:params AS jsonb)) RETURNING id"
)


# ── 적재 ─────────────────────────────────────────────────────


async def _start_run(engine: AsyncEngine, job: str, params: dict) -> int:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                _START_RUN,
                {"job": job, "params": json.dumps(params, ensure_ascii=False)},
            )
        ).scalar_one()


async def _regions(conn: AsyncConnection, geo: GeoIndex) -> _Regions:
    """법정동 마스터(regions)로 이름 → 코드 표를 만들고, 카카오 지오코딩 캐시로 중심점을 채운다."""
    found = (await conn.execute(text("SELECT code, level, parent_code, name FROM regions"))).all()
    by_name: dict[tuple[str, str], str] = {}
    centroid: dict[str, tuple[float, float]] = {}
    for s in SIGUNGU:
        hit = geo.region(s.code, "")
        centroid[s.code] = (hit.lat, hit.lng) if hit else (s.lat, s.lng)
    for code, level, parent, name in found:
        if level != "dong" or parent not in SIGUNGU_BY_CODE:
            continue
        by_name[(parent, name)] = code
        hit = geo.region(parent, name)
        if hit:
            centroid[code] = (hit.lat, hit.lng)
    known = sorted(centroid)
    await conn.execute(
        text(
            """
            UPDATE regions r SET centroid = ST_SetSRID(ST_MakePoint(u.lng, u.lat), 4326)
            FROM unnest(CAST(:code AS text[]), CAST(:lat AS float8[]), CAST(:lng AS float8[])) AS u(code, lat, lng)
            WHERE r.code = u.code
            """
        ),
        {"code": known, "lat": [centroid[c][0] for c in known], "lng": [centroid[c][1] for c in known]},
    )
    for code, level, parent, _ in found:
        if level == "dong" and code not in centroid and parent in centroid:
            centroid[code] = centroid[parent]
    return _Regions(by_name, {r[0] for r in found}, centroid, {r[0]: r[3] for r in found})


def _columns(rows: list[dict], names: Iterable[str]) -> dict[str, list]:
    return {n: [r[n] for r in rows] for n in names}


async def _load_window(
    conn: AsyncConnection,
    run_id: int,
    window: Window,
    trades: list[RawTrade],
    regions: _Regions,
    geo: GeoIndex,
    report: LoadReport,
) -> None:
    kind, sgg_cd, ym = window
    complexes: dict[UUID, dict] = {}
    tx_rows: list[dict] = []
    for t in trades:
        region_code = regions.code_for(t)
        if region_code is None:
            report.unmapped_regions.add((t.sgg_cd, t.umd_nm))
            report.unmapped_rows += 1
            continue
        lat, lng = regions.centroid.get(region_code) or regions.centroid[sgg_cd]
        pnu, precise, cid = None, False, None
        if t.property_type != PropertyType.land:
            key = complex_key(t)
            cid = complex_id(key)
            if cid not in complexes:
                hit = geo.address(t.sgg_cd, t.umd_nm, t.jibun) if t.jibun else None
                exact = bool(hit and hit.precise)
                dong = regions.names.get(region_code, t.umd_nm)
                address = f"부산 {SIGUNGU_BY_CODE[t.sgg_cd].name} {dong} {t.jibun}".strip()
                complexes[cid] = {
                    "id": cid,
                    "property_type": t.property_type.value,
                    "name": t.name or address,
                    "name_key": normalize(t.name or ""),
                    "source_seq": t.complex_seq,
                    "region_code": region_code,
                    "jibun": t.jibun,
                    "pnu": hit.pnu if exact else None,
                    "lat": hit.lat if exact else lat,
                    "lng": hit.lng if exact else lng,
                    "location_source": "geocode" if exact else "centroid",
                }
            c = complexes[cid]
            lat, lng, pnu, precise = c["lat"], c["lng"], c["pnu"], c["location_source"] == "geocode"
        elif "*" not in t.jibun and t.jibun:
            hit = geo.address(t.sgg_cd, t.umd_nm, t.jibun)
            if hit and hit.precise:
                lat, lng, pnu, precise = hit.lat, hit.lng, hit.pnu, True

        sale = t.deal_type == DealType.sale
        jeonse = t.deal_type == DealType.jeonse
        land = t.property_type == PropertyType.land
        tx_rows.append(
            {
                "id": transaction_id(t.source_key),
                "source_key": t.source_key,
                "source_kind": t.kind.value,
                "property_type": t.property_type.value,
                "deal_type": t.deal_type.value,
                "complex_id": cid,
                "region_code": region_code,
                "jibun": t.jibun or None,
                "pnu": pnu,
                "lat": lat,
                "lng": lng,
                "location_precision": "parcel" if precise else "dong",
                "price": (t.amount_man or 0) * 10_000 if sale else None,
                "deposit": None if sale else (t.deposit_man or 0) * 10_000,
                "monthly_rent": None if sale or jeonse else (t.monthly_rent_man or 0) * 10_000,
                "exclusive_area_m2": None if land else t.exclusive_area_m2,
                "land_area_m2": t.land_area_m2,
                "floor": t.floor,
                "build_year": t.build_year,
                "contract_date": t.contract_date,
                "trade_method": t.trade_method.value if t.trade_method else None,
                "is_cancelled": t.is_cancelled,
                "cancelled_at": t.cancelled_date,
                "registered_date": t.registered_date,
                "is_share_deal": t.is_share_deal,
                "jimok": t.jimok,
                "land_use": t.land_use,
                "house_type": t.house_type,
                "contract_type": t.contract_type,
                "contract_term": t.contract_term,
                "use_renewal_right": _renewal_right(t.use_renewal_right),
            }
        )

    if complexes:
        rows = list(complexes.values())
        await conn.execute(_UPSERT_COMPLEXES, _columns(rows, rows[0].keys()))
        # 거래 위치는 단지에 저장된 위치를 따른다. 단지마다 처음 본 거래 기준이라 이번 단위에서 계산한 값과 다를 수 있다
        stored = {r.id: r for r in (await conn.execute(_STORED_COMPLEXES, {"ids": list(complexes)})).all()}
        for row in tx_rows:
            c = stored.get(row["complex_id"]) if row["complex_id"] else None
            if c is not None:
                row |= {
                    "lat": c.lat,
                    "lng": c.lng,
                    "pnu": c.pnu,
                    "location_precision": "parcel" if c.location_source == "geocode" else "dong",
                }
    if tx_rows:
        keys = [r["source_key"] for r in tx_rows]
        existing = dict((await conn.execute(_EXISTING, {"keys": keys})).all())
        result = await conn.execute(
            _UPSERT_TRANSACTIONS, {"run_id": run_id, **_columns(tx_rows, (c for c, _ in _TX_COLUMNS))}
        )
        for source_key, touched in result.all():
            if source_key not in existing:
                report.rows_inserted += 1
            elif touched:
                report.rows_updated += 1
        report.rows_cancelled += sum(1 for r in tx_rows if r["is_cancelled"] and existing.get(r["source_key"]) is False)

    start, end = _month_range(ym)
    deleted = await conn.execute(
        _MARK_DELETED, {"kind": kind.value, "sgg": sgg_cd, "start": start, "end": end, "run_id": run_id}
    )
    report.rows_deleted += deleted.rowcount


async def load_into_db(
    engine: AsyncEngine,
    raw: RawCache,
    geo: GeoIndex,
    *,
    job: str = "collect",
    params: dict | None = None,
    calls_made: int = 0,
    force: bool = False,
    out: Callable[[str], None] = print,
) -> LoadReport:
    """캐시의 모든 단위 중 DB에 없거나 더 최근에 받은 단위만 적재한다(`force`면 전부).

    다른 적재가 돌고 있으면 기록을 남기지 않고 `LoadBusy`를 던진다.
    """
    async with engine.connect() as lock:
        if not (await lock.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOAD_LOCK_KEY})).scalar():
            raise LoadBusy("다른 적재가 이미 진행 중입니다. 끝난 뒤에 다시 실행하세요")
        # 세션 잠금이라 커밋해도 유지된다. 적재 내내 트랜잭션을 열어 두지 않는다
        await lock.commit()
        try:
            return await _load(engine, raw, geo, job=job, params=params, calls_made=calls_made, force=force, out=out)
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOAD_LOCK_KEY})
            await lock.commit()


async def _load(
    engine: AsyncEngine,
    raw: RawCache,
    geo: GeoIndex,
    *,
    job: str,
    params: dict | None,
    calls_made: int,
    force: bool,
    out: Callable[[str], None],
) -> LoadReport:
    run_id = await _start_run(engine, job, {"force": force, **(params or {})})
    report = LoadReport(run_id)
    async with engine.begin() as conn:
        regions = await _regions(conn, geo)
        logged = dict(
            ((k, s, y), f)
            for k, s, y, f in (
                await conn.execute(text("SELECT source_kind, sgg_cd, deal_ym, fetched_at FROM source_fetch_log"))
            ).all()
        )

    as_of: datetime | None = None
    for window in raw.windows():
        kind, sgg_cd, ym = window
        fetched_at = raw.fetched_at(*window)
        if fetched_at is None or sgg_cd not in SIGUNGU_BY_CODE:
            continue
        as_of = max(as_of, fetched_at) if as_of else fetched_at
        last = logged.get((kind.value, sgg_cd, ym))
        if not force and last is not None and last >= fetched_at:
            report.windows_skipped += 1
            continue
        trades = raw.load(*window)
        try:
            async with engine.begin() as conn:
                await _load_window(conn, run_id, window, trades, regions, geo, report)
                await conn.execute(
                    _LOG_WINDOW,
                    {
                        "kind": kind.value,
                        "sgg": sgg_cd,
                        "ym": ym,
                        "run_id": run_id,
                        "total": len(trades),
                        "fetched_at": fetched_at,
                    },
                )
        except Exception as e:  # noqa: BLE001 - 한 단위가 실패해도 나머지는 적재하고 결과에 남긴다
            out(f"  적재 실패 {kind.value} {sgg_cd} {ym}: {type(e).__name__}: {e}")
            report.windows_failed.append(window)
            continue
        report.windows_loaded += 1
        report.rows_fetched += len(trades)
        if report.windows_loaded % 100 == 0:
            out(f"  적재 {report.windows_loaded}개 단위")

    if report.windows_loaded:
        async with engine.begin() as conn:
            for statement in _AREA_TYPES:
                await conn.execute(statement)
            await conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_monthly_stats"))

    report.status = (
        "failed"
        if report.windows_failed and not report.windows_loaded
        else ("partial" if report.windows_failed else "success")
    )
    await _finish_run(engine, report, calls_made, as_of)
    return report


async def _finish_run(engine: AsyncEngine, report: LoadReport, calls_made: int, as_of: datetime | None) -> None:
    summary = {
        "windows_loaded": report.windows_loaded,
        "windows_skipped": report.windows_skipped,
        "windows_failed": [[k.value, s, y] for k, s, y in report.windows_failed],
        "unmapped_regions": sorted([list(r) for r in report.unmapped_regions]),
        "unmapped_rows": report.unmapped_rows,
    }
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                UPDATE etl_runs SET status = :status, finished_at = now(), calls_made = :calls,
                    rows_fetched = :fetched, rows_inserted = :inserted, rows_updated = :updated,
                    rows_cancelled = :cancelled, rows_deleted = :deleted, data_as_of = :as_of,
                    report = CAST(:report AS jsonb),
                    error = :error
                WHERE id = :id
                """
            ),
            {
                "id": report.run_id,
                "status": report.status,
                "calls": calls_made,
                "fetched": report.rows_fetched,
                "inserted": report.rows_inserted,
                "updated": report.rows_updated,
                "cancelled": report.rows_cancelled,
                "deleted": report.rows_deleted,
                "as_of": as_of,
                "report": json.dumps(summary, ensure_ascii=False),
                "error": f"적재 실패 단위 {len(report.windows_failed)}개" if report.windows_failed else None,
            },
        )

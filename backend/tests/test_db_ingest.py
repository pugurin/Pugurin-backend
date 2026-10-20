"""수집 캐시(.cache)를 실제 PostGIS에 적재하는 단계를 검증한다(#38 2단계). Docker가 없으면 건너뛴다."""

from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import text

from app.adapters.molit.types import SourceKind
from app.db import migrate
from app.db.engine import make_engine
from app.ingestion.build import build_dataset
from app.ingestion.collect import collect
from app.ingestion.load import load_into_db
from app.ingestion.rawcache import RawCache
from tests.conftest import FIXED_NOW, create_database
from tests.fakes.molit import FixtureTradeSource
from tests.test_ingestion import DISTRICTS, geo_for

pytestmark = pytest.mark.anyio

WINDOW = (SourceKind.apt_trade, "26350", "202509")


@pytest.fixture(scope="module")
def ingest_db(postgis: str) -> str:
    url = create_database(postgis, "ingest")
    migrate.upgrade(url)
    return url


@pytest.fixture
async def engine(ingest_db):
    engine = make_engine(ingest_db)
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE transactions, complex_area_types, complexes, source_fetch_log, etl_runs RESTART IDENTITY")
        )
        await conn.execute(text("UPDATE regions SET centroid = NULL"))
    yield engine
    await engine.dispose()


@pytest.fixture
async def cache(tmp_path):
    """해운대구·수영구 2025-09 실제 응답 샘플(7종)을 수집 캐시에 넣고, 가짜 지오코더로 좌표를 채운다."""
    raw = RawCache(tmp_path)
    await collect(
        FixtureTradeSource(),
        raw,
        months=["202509"],
        sgg_codes=DISTRICTS,
        kinds=list(SourceKind),
        now=FIXED_NOW,
    )
    trades = [t for w in raw.windows() for t in raw.load(*w)]
    return raw, await geo_for(trades, tmp_path), trades


async def load(engine, cache, **kw):
    raw, geo, _ = cache
    return await load_into_db(engine, raw, geo, out=lambda _: None, **kw)


async def scalar(engine, sql: str, **params):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def rows(engine, sql: str, **params):
    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(text(sql), params)).all()]


def edit_window(raw: RawCache, window, change, *, minutes: int = 10):
    """캐시의 한 단위를 고쳐 다시 받은 것처럼 저장한다(fetched_at을 뒤로 민다)."""
    trades = raw.load(*window)
    fields = change([dict(t.raw) for t in trades])
    from app.adapters.molit.parser import rows_from_fields

    raw.save(*window, rows_from_fields(window[0], fields), fetched_at=FIXED_NOW + timedelta(minutes=minutes))


# ── 적재 ─────────────────────────────────────────────────────


async def test_every_trade_is_stored_with_its_source_values(engine, cache):
    _, _, trades = cache
    report = await load(engine, cache)

    assert report.status == "success"
    assert report.unmapped_regions == set()
    assert report.rows_inserted == len(trades) == await scalar(engine, "SELECT count(*) FROM transactions")
    assert report.windows_loaded == len(list(SourceKind)) * len(DISTRICTS)
    assert await scalar(engine, "SELECT count(*) FROM source_fetch_log") == report.windows_loaded

    # 지분거래·도로도 저장한다. 통계에서 빼는 일은 조회 단계가 한다(ERD 원칙 6)
    shares = sum(t.is_share_deal for t in trades if t.kind == SourceKind.land_trade)
    roads = sum(t.jimok == "도로" for t in trades if t.kind == SourceKind.land_trade)
    assert shares and roads
    assert await scalar(engine, "SELECT count(*) FROM transactions WHERE is_share_deal") == shares
    assert await scalar(engine, "SELECT count(*) FROM transactions WHERE jimok = '도로'") == roads


async def test_ids_match_the_file_cache_build(engine, cache):
    # 조회를 DB로 옮겨도 앱이 들고 있는 단지·거래 id가 그대로여야 한다
    raw, geo, trades = cache
    dataset, _, _ = build_dataset(trades, geo)
    await load(engine, cache)

    tx_ids = {r[0] for r in await rows(engine, "SELECT id FROM transactions")}
    complex_ids = {r[0] for r in await rows(engine, "SELECT id FROM complexes")}
    assert {t.id for t in dataset.transactions} <= tx_ids
    assert {c.id for c in dataset.complexes} == complex_ids
    extra = len(tx_ids) - len(dataset.transactions)
    assert extra == await scalar(engine, "SELECT count(*) FROM transactions WHERE is_share_deal OR jimok = '도로'")


async def test_units_and_rent_semantics(engine, cache):
    await load(engine, cache)
    # 금액은 원, 전세는 월세 NULL, 월세는 둘 다
    assert (
        await scalar(engine, "SELECT count(*) FROM transactions WHERE deal_type = 'sale' AND price % 10000 <> 0") == 0
    )
    assert await scalar(engine, "SELECT count(*) FROM transactions WHERE deal_type = 'jeonse'") > 0
    assert await scalar(engine, "SELECT count(*) FROM transactions WHERE deal_type = 'monthly'") > 0
    # 갱신 계약과 갱신요구권 사용 여부를 원천 그대로 남긴다
    assert await scalar(engine, "SELECT count(*) FROM transactions WHERE contract_type = '갱신'") > 0
    assert await scalar(engine, "SELECT count(*) FROM transactions WHERE use_renewal_right") > 0


async def test_masked_land_sits_at_the_dong_centroid(engine, cache):
    await load(engine, cache)
    land = await rows(
        engine,
        """
        SELECT DISTINCT t.location_precision, t.pnu IS NULL, ST_Equals(t.location, r.centroid)
        FROM transactions t JOIN regions r ON r.code = t.region_code
        WHERE t.property_type = 'land'
        """,
    )
    assert land == [("dong", True, True)]


async def test_complexes_get_locations_area_types_and_build_year(engine, cache):
    await load(engine, cache)
    assert await scalar(engine, "SELECT count(*) FROM complexes WHERE location_source = 'geocode'") > 0
    assert await scalar(engine, "SELECT count(*) FROM complex_area_types") > 0
    assert await scalar(engine, "SELECT count(*) FROM complexes WHERE build_year IS NOT NULL") > 0
    # 84.842와 84.97은 같은 평형(85)으로 묶인다
    dupes = await scalar(
        engine,
        "SELECT count(*) FROM (SELECT 1 FROM complex_area_types GROUP BY complex_id, area_bucket HAVING count(*) > 1) d",
    )
    assert dupes == 0


async def test_region_centroids_come_from_geocoding(engine, cache):
    await load(engine, cache)
    assert await scalar(engine, "SELECT count(*) FROM regions WHERE level = 'sigungu' AND centroid IS NULL") == 0
    assert await scalar(engine, "SELECT count(*) FROM regions WHERE level = 'dong' AND centroid IS NOT NULL") > 0


async def test_monthly_stats_are_refreshed(engine, cache):
    await load(engine, cache)
    assert await scalar(engine, "SELECT count(*) FROM mv_monthly_stats") > 0
    # 갱신 계약을 빼면 표본이 줄어든다
    sql = """SELECT sum(n) FROM mv_monthly_stats
             WHERE scope_level = 'sigungu' AND deal_type = 'jeonse' AND include_direct AND include_renewals = :r"""
    with_renewals = await scalar(engine, sql, r=True)
    without = await scalar(engine, sql, r=False)
    assert with_renewals > without


async def test_run_is_recorded(engine, cache):
    report = await load(engine, cache, calls_made=14)
    run = (
        await rows(
            engine,
            "SELECT status, calls_made, rows_fetched, rows_inserted, finished_at IS NOT NULL, data_as_of FROM etl_runs",
        )
    )[0]
    assert run[:5] == ("success", 14, report.rows_fetched, report.rows_inserted, True)
    assert run[5] == FIXED_NOW


# ── 다시 적재 ────────────────────────────────────────────────


async def test_loading_again_skips_unchanged_windows(engine, cache):
    await load(engine, cache)
    before = await rows(engine, "SELECT id, updated_at FROM transactions ORDER BY id")
    again = await load(engine, cache)
    assert again.windows_loaded == 0 and again.windows_skipped > 0
    assert await rows(engine, "SELECT id, updated_at FROM transactions ORDER BY id") == before


async def test_forced_reload_changes_nothing_that_did_not_change(engine, cache):
    first = await load(engine, cache)
    again = await load(engine, cache, force=True)
    assert again.windows_loaded == first.windows_loaded
    assert (again.rows_inserted, again.rows_updated, again.rows_cancelled, again.rows_deleted) == (0, 0, 0, 0)


async def test_cancellation_updates_the_same_row(engine, cache):
    raw, _, _ = cache
    await load(engine, cache)
    target = raw.load(*WINDOW)[0]

    def cancel(fields):
        fields[0] |= {"cdealType": "O", "cdealDay": "25.10.02"}
        return fields

    edit_window(raw, WINDOW, cancel)
    report = await load(engine, cache)
    assert (report.windows_loaded, report.rows_inserted, report.rows_cancelled) == (1, 0, 1)
    tx_id = uuid5(NAMESPACE_URL, f"pugurin:tx:{target.source_key}")
    got = await rows(engine, "SELECT is_cancelled, cancelled_at::text FROM transactions WHERE id = :id", id=tx_id)
    assert got == [(True, "2025-10-02")]


async def test_rows_missing_from_a_refetch_are_marked_deleted_and_can_come_back(engine, cache):
    raw, _, _ = cache
    await load(engine, cache)
    target = raw.load(*WINDOW)[0]
    original = [dict(t.raw) for t in raw.load(*WINDOW)]

    edit_window(raw, WINDOW, lambda fields: fields[1:])
    removed = await load(engine, cache)
    assert removed.rows_deleted == 1
    deleted = await rows(engine, "SELECT source_key FROM transactions WHERE deleted_at IS NOT NULL")
    assert deleted == [(target.source_key,)]

    edit_window(raw, WINDOW, lambda _: original, minutes=20)
    back = await load(engine, cache)
    assert (back.rows_inserted, back.rows_updated) == (0, 1)
    assert await scalar(engine, "SELECT count(*) FROM transactions WHERE deleted_at IS NOT NULL") == 0


async def test_other_windows_are_untouched_by_a_refetch(engine, cache):
    raw, _, _ = cache
    await load(engine, cache)
    emptied = len(raw.load(*WINDOW))
    edit_window(raw, WINDOW, lambda fields: [])
    report = await load(engine, cache)
    assert report.rows_deleted == emptied
    left = await scalar(
        engine, "SELECT count(*) FROM transactions WHERE deleted_at IS NULL AND source_kind = 'apt_trade'"
    )
    assert left == len(raw.load(SourceKind.apt_trade, "26500", "202509"))


async def test_unknown_dong_names_are_skipped_and_reported(engine, cache):
    raw, _, _ = cache

    def rename(fields):
        fields[0] |= {"umdNm": "없는동", "umdCd": ""}
        return fields

    edit_window(raw, (SourceKind.offi_trade, "26350", "202509"), rename)
    report = await load(engine, cache)
    assert report.status == "success"
    assert report.unmapped_regions == {("26350", "없는동")}
    assert report.unmapped_rows == 1
    noted = await scalar(engine, "SELECT report -> 'unmapped_regions' FROM etl_runs")
    assert noted == [["26350", "없는동"]]


async def test_complexes_geocoded_later_move_with_all_their_trades(engine, cache, tmp_path):
    from app.ingestion.build import GeoIndex
    from app.ingestion.geocode import GeocodeCache

    raw, geo, _ = cache
    await load_into_db(engine, raw, GeoIndex(GeocodeCache(tmp_path / "none.json")), out=lambda _: None)
    assert await scalar(engine, "SELECT count(*) FROM complexes WHERE location_source = 'geocode'") == 0

    await load_into_db(engine, raw, geo, force=True, out=lambda _: None)
    assert await scalar(engine, "SELECT count(*) FROM complexes WHERE location_source = 'centroid'") < 5
    apart = await scalar(
        engine,
        """SELECT count(*) FROM transactions t JOIN complexes c ON c.id = t.complex_id
           WHERE NOT ST_Equals(t.location, c.location) OR t.pnu IS DISTINCT FROM c.pnu""",
    )
    assert apart == 0

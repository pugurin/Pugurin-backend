"""실제 PostGIS·Redis 컨테이너로 마이그레이션·시드·readiness를 검증한다. Docker가 없으면 건너뛴다."""

import asyncio
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.db import migrate
from app.db.legal_dong import load_legal_dongs
from app.db.seed import upsert_regions
from app.main import create_app
from tests.conftest import FIXED_NOW, create_database

INTERNAL = ("127.0.0.1", 50000)
READY = "/internal/health/ready"


@pytest.fixture(scope="module")
def migrated(postgis: str) -> str:
    migrate.upgrade(postgis)
    return postgis


def _scalar(url: str, sql: str):
    async def run():
        from app.db.engine import make_engine

        engine = make_engine(url)
        try:
            async with engine.connect() as conn:
                return (await conn.execute(text(sql))).scalar()
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _rows(url: str, sql: str, params: dict | None = None):
    async def run():
        from app.db.engine import make_engine

        engine = make_engine(url)
        try:
            async with engine.connect() as conn:
                return [tuple(r) for r in (await conn.execute(text(sql), params or {})).all()]
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _run_sync(url: str, fn):
    async def run():
        from app.db.engine import make_engine

        engine = make_engine(url)
        try:
            async with engine.begin() as conn:
                return await conn.run_sync(fn)
        finally:
            await engine.dispose()

    return asyncio.run(run())


# ── 마이그레이션 ─────────────────────────────────────────────


def test_upgrade_creates_the_erd_tables_and_view(migrated):
    tables = {
        r[0]
        for r in _rows(migrated, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        if r[0] != "alembic_version"
    }
    assert tables >= {
        "regions",
        "etl_runs",
        "source_fetch_log",
        "complexes",
        "complex_area_types",
        "transactions",
        "geocode_cache",
        "parcels_cache",
        "zoning_rules",
        "glossary_terms",
        "restriction_explanations",
    }
    assert _scalar(migrated, "SELECT count(*) FROM pg_matviews WHERE matviewname = 'mv_monthly_stats'") == 1
    assert _scalar(migrated, "SELECT extname FROM pg_extension WHERE extname = 'postgis'") == "postgis"


def test_database_is_at_the_latest_revision(migrated):
    assert _scalar(migrated, "SELECT version_num FROM alembic_version") == migrate.head_revision()


def test_monthly_stats_view_has_both_option_dimensions(migrated):
    cols = {
        r[0] for r in _rows(migrated, "SELECT attname FROM pg_attribute WHERE attrelid = 'mv_monthly_stats'::regclass")
    }
    assert {"include_direct", "include_renewals", "median_price_per_pyeong"} <= cols


# ── 법정동 시드 ──────────────────────────────────────────────


def test_regions_hold_every_busan_legal_dong(migrated):
    counts = dict(_rows(migrated, "SELECT level, count(*) FROM regions GROUP BY level"))
    assert counts == {"sigungu": 16, "dong": 254}


def test_ri_level_names_match_the_trade_source_spelling(migrated):
    # 실거래가 원천은 기장군을 '기장읍 교리'처럼 읍면 + 리로 준다
    rows = _rows(migrated, "SELECT code, parent_code FROM regions WHERE name = '기장읍 교리'")
    assert rows == [("2671025022", "26710")]
    assert _rows(migrated, "SELECT name FROM regions WHERE code = '26350'") == [("해운대구",)]
    assert _rows(migrated, "SELECT name, parent_code FROM regions WHERE code = '2635010500'") == [("우동", "26350")]


def test_seeding_again_changes_nothing(migrated):
    before = _rows(migrated, "SELECT code, level, parent_code, name FROM regions ORDER BY code")
    assert _run_sync(migrated, upsert_regions) == len(load_legal_dongs())
    assert _rows(migrated, "SELECT code, level, parent_code, name FROM regions ORDER BY code") == before


# ── 제약 조건 (ERD가 실제로 막는지) ──────────────────────────


def _insert_trade(conn, **overrides):
    run_id = conn.execute(
        text("INSERT INTO etl_runs (job, status) VALUES ('collect', 'running') RETURNING id")
    ).scalar()
    values = {
        "id": "00000000-0000-0000-0000-000000000001",
        "source_key": "a" * 40,
        "source_kind": "land_trade",
        "property_type": "land",
        "deal_type": "sale",
        "region_code": "2635010500",
        "location_precision": "dong",
        "price": 100_000_000,
        "deposit": None,
        "land_area_m2": 100,
        "contract_date": date(2026, 9, 1),
        "run": run_id,
    } | overrides
    conn.execute(
        text(
            """
            INSERT INTO transactions (id, source_key, source_kind, property_type, deal_type, region_code, location,
                location_precision, price, deposit, land_area_m2, contract_date, first_seen_run_id, last_seen_run_id)
            VALUES (:id, :source_key, :source_kind, :property_type, :deal_type, :region_code,
                ST_SetSRID(ST_MakePoint(129.16, 35.16), 4326), :location_precision, :price, :deposit, :land_area_m2,
                :contract_date, :run, :run)
            """
        ),
        values,
    )


def test_a_valid_land_sale_is_accepted(migrated):
    def run(conn):
        _insert_trade(conn)
        n = conn.execute(text("SELECT count(*) FROM transactions")).scalar()
        conn.execute(text("DELETE FROM transactions"))
        return n

    assert _run_sync(migrated, run) == 1


def test_land_cannot_be_rented(migrated):
    with pytest.raises(IntegrityError):
        _run_sync(migrated, lambda c: _insert_trade(c, deal_type="jeonse", price=None, deposit=100_000_000))


def test_unknown_region_is_rejected(migrated):
    with pytest.raises(IntegrityError):
        _run_sync(migrated, lambda c: _insert_trade(c, region_code="1111010100"))


# ── 되돌리기 ─────────────────────────────────────────────────


def test_downgrade_to_base_and_back(postgis):
    # 다른 테스트와 겹치지 않게 같은 컨테이너에 DB를 하나 더 만든다
    url = create_database(postgis, "roundtrip")
    migrate.upgrade(url)
    migrate.downgrade(url, "base")
    assert _scalar(url, "SELECT to_regclass('public.transactions')") is None
    migrate.upgrade(url)
    assert _scalar(url, "SELECT count(*) FROM regions") == 270


# ── readiness ────────────────────────────────────────────────


def _client(client_addr=INTERNAL, **settings) -> TestClient:
    return TestClient(create_app(settings=Settings(**settings), clock=lambda: FIXED_NOW), client=client_addr)


def test_ready_when_database_and_redis_are_up(migrated, redis_url):
    with _client(database_url=migrated, redis_url=redis_url) as c:
        r = c.get(READY)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["redis"] == {"status": "ok", "detail": None}
    assert r.headers["Cache-Control"] == "no-store"


def test_not_ready_when_schema_is_behind(postgis):
    url = create_database(postgis, "behind")
    migrate.upgrade(url, "0001")
    with _client(database_url=url) as c:
        r = c.get(READY)
    assert r.status_code == 503
    assert "마이그레이션 필요" in r.json()["checks"]["database"]["detail"]

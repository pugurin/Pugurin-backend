"""1차 스키마 (docs/ERD.md 4장 DDL 그대로)

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE regions (
    code        varchar(10) PRIMARY KEY,
    level       text NOT NULL CHECK (level IN ('sigungu', 'dong')),
    parent_code varchar(10) REFERENCES regions (code),
    name        text NOT NULL,
    centroid    geometry(Point, 4326),
    boundary    geometry(MultiPolygon, 4326),
    CHECK ((level = 'sigungu' AND length(code) = 5) OR (level = 'dong' AND length(code) = 10)),
    UNIQUE (parent_code, name)
);
CREATE INDEX regions_centroid ON regions USING gist (centroid);

CREATE TABLE etl_runs (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job           text NOT NULL CHECK (job IN ('collect', 'backfill', 'aggregate')),
    status        text NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed', 'quota_exceeded')),
    params        jsonb NOT NULL DEFAULT '{}',
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    calls_made    int NOT NULL DEFAULT 0,
    rows_fetched  int NOT NULL DEFAULT 0,
    rows_inserted int NOT NULL DEFAULT 0,
    rows_updated  int NOT NULL DEFAULT 0,
    rows_cancelled int NOT NULL DEFAULT 0,
    rows_deleted  int NOT NULL DEFAULT 0,
    error         text,
    data_as_of    timestamptz
);

CREATE TABLE source_fetch_log (
    source_kind text NOT NULL,
    sgg_cd      char(5) NOT NULL,
    deal_ym     char(6) NOT NULL,
    run_id      bigint NOT NULL REFERENCES etl_runs (id),
    total_count int NOT NULL,
    fetched_at  timestamptz NOT NULL,
    PRIMARY KEY (source_kind, sgg_cd, deal_ym)
);

CREATE TABLE complexes (
    id               uuid PRIMARY KEY,
    property_type    text NOT NULL CHECK (property_type IN ('apartment', 'officetel', 'villa')),
    name             text NOT NULL,
    name_key         text NOT NULL,
    source_seq       text UNIQUE,
    region_code      varchar(10) NOT NULL REFERENCES regions (code),
    jibun            text NOT NULL,
    pnu              char(19),
    location         geometry(Point, 4326) NOT NULL,
    location_source  text NOT NULL CHECK (location_source IN ('geocode', 'centroid')),
    build_year       smallint,
    household_count  int,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (source_seq IS NULL OR property_type = 'apartment')
);
CREATE UNIQUE INDEX complexes_natural_key ON complexes (property_type, region_code, jibun, name_key)
    WHERE source_seq IS NULL;
CREATE INDEX complexes_location ON complexes USING gist (location);
CREATE INDEX complexes_region ON complexes (region_code);

CREATE TABLE complex_area_types (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    complex_id         uuid NOT NULL REFERENCES complexes (id) ON DELETE CASCADE,
    area_bucket        smallint NOT NULL,
    exclusive_area_m2  numeric(9, 3) NOT NULL,
    supply_area_pyeong numeric(6, 1),
    supply_source      text,
    UNIQUE (complex_id, area_bucket)
);

CREATE TABLE transactions (
    id                 uuid PRIMARY KEY,
    source_key         char(40) NOT NULL UNIQUE,
    source_kind        text NOT NULL CHECK (source_kind IN
                         ('apt_trade', 'apt_rent', 'offi_trade', 'offi_rent', 'rh_trade', 'rh_rent', 'land_trade')),
    property_type      text NOT NULL CHECK (property_type IN ('apartment', 'officetel', 'villa', 'land')),
    deal_type          text NOT NULL CHECK (deal_type IN ('sale', 'jeonse', 'monthly')),
    complex_id         uuid REFERENCES complexes (id),
    region_code        varchar(10) NOT NULL REFERENCES regions (code),
    jibun              text,
    pnu                char(19),
    location           geometry(Point, 4326) NOT NULL,
    location_precision text NOT NULL CHECK (location_precision IN ('parcel', 'dong')),
    price              bigint,
    deposit            bigint,
    monthly_rent       bigint,
    exclusive_area_m2  numeric(9, 3),
    land_area_m2       numeric(12, 2),
    basis_area_m2      numeric GENERATED ALWAYS AS (coalesce(exclusive_area_m2, land_area_m2)) STORED,
    floor              smallint,
    build_year         smallint,
    contract_date      date NOT NULL,
    trade_method       text CHECK (trade_method IN ('broker', 'direct')),
    is_cancelled       boolean NOT NULL DEFAULT false,
    cancelled_at       date,
    registered_date    date,
    is_share_deal      boolean NOT NULL DEFAULT false,
    jimok              text,
    land_use           text,
    house_type         text,
    contract_type      text,
    contract_term      text,
    use_renewal_right  boolean,
    first_seen_run_id  bigint NOT NULL REFERENCES etl_runs (id),
    last_seen_run_id   bigint NOT NULL REFERENCES etl_runs (id),
    deleted_at         timestamptz,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    -- 전세는 원천의 월세 0을 NULL로 바꿔 넣는다
    CHECK ((deal_type = 'sale' AND price IS NOT NULL AND deposit IS NULL AND monthly_rent IS NULL)
        OR (deal_type = 'jeonse' AND price IS NULL AND deposit IS NOT NULL AND monthly_rent IS NULL)
        OR (deal_type = 'monthly' AND price IS NULL AND deposit IS NOT NULL AND monthly_rent IS NOT NULL)),
    CHECK (property_type <> 'land' OR deal_type = 'sale'),
    CHECK ((property_type = 'land') = (exclusive_area_m2 IS NULL)),
    CHECK (location_precision = 'parcel' OR pnu IS NULL)
);
CREATE INDEX transactions_map ON transactions (property_type, deal_type, contract_date DESC)
    WHERE deleted_at IS NULL AND NOT is_cancelled;
CREATE INDEX transactions_region ON transactions (region_code, property_type, deal_type, contract_date DESC)
    WHERE deleted_at IS NULL AND NOT is_cancelled;
CREATE INDEX transactions_complex ON transactions (complex_id, contract_date DESC)
    WHERE complex_id IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX transactions_parcel_location ON transactions USING gist (location)
    WHERE location_precision = 'parcel' AND deleted_at IS NULL;
CREATE INDEX transactions_window ON transactions (source_kind, left(region_code, 5), contract_date)
    WHERE deleted_at IS NULL;

CREATE TABLE geocode_cache (
    address_key text PRIMARY KEY,
    status      text NOT NULL CHECK (status IN ('ok', 'not_found', 'error')),
    provider    text NOT NULL,
    location    geometry(Point, 4326),
    pnu         char(19),
    attempts    smallint NOT NULL DEFAULT 1,
    last_error  text,
    retry_after timestamptz,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE parcels_cache (
    pnu                        char(19) PRIMARY KEY,
    jibun_address              text NOT NULL,
    land_category              text,
    land_area_m2               numeric(12, 2),
    official_land_price_per_m2 int,
    official_land_price_year   smallint,
    road_side                  text,
    shape                      text,
    geometry                   geometry(MultiPolygon, 4326) NOT NULL,
    zonings                    jsonb NOT NULL,
    restrictions               jsonb NOT NULL DEFAULT '[]',
    fetched_at                 timestamptz NOT NULL,
    expires_at                 timestamptz NOT NULL
);
CREATE INDEX parcels_cache_geometry ON parcels_cache USING gist (geometry);
CREATE INDEX parcels_cache_expiry ON parcels_cache (expires_at);

CREATE TABLE zoning_rules (
    zone_type                   text PRIMARY KEY,
    max_building_coverage_ratio smallint NOT NULL,
    max_floor_area_ratio        smallint NOT NULL,
    source                      text NOT NULL,
    effective_from              date NOT NULL,
    plain_explanation           text
);

CREATE TABLE glossary_terms (
    id               uuid PRIMARY KEY,
    term             text NOT NULL UNIQUE,
    category         text NOT NULL CHECK (category IN ('trade', 'land', 'building', 'tax')),
    is_popular       boolean NOT NULL DEFAULT false,
    display_order    int NOT NULL,
    short_definition text NOT NULL,
    long_definition  text NOT NULL,
    example          text NOT NULL,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE restriction_explanations (
    name              text PRIMARY KEY,
    plain_explanation text NOT NULL,
    glossary_term_id  uuid REFERENCES glossary_terms (id)
);

-- 월별 통계(추이 그래프용). 지도 마커의 "최근 N개월" 중위값은 기간 중위값이라 월별 값으로 합칠 수 없어 transactions에서 직접 계산한다.
CREATE MATERIALIZED VIEW mv_monthly_stats AS
WITH base AS (
    SELECT t.*, date_trunc('month', t.contract_date)::date AS month
    FROM transactions t
    WHERE t.deleted_at IS NULL AND NOT t.is_cancelled
      AND NOT t.is_share_deal AND coalesce(t.jimok, '') <> '도로'
), scoped AS (
    SELECT 'dong' AS scope_level, region_code::text AS scope_id, * FROM base
    UNION ALL
    SELECT 'sigungu', left(region_code, 5), * FROM base
    UNION ALL
    SELECT 'complex', complex_id::text, * FROM base WHERE complex_id IS NOT NULL
)
SELECT s.scope_level, s.scope_id, s.property_type, s.deal_type, s.month, d.include_direct, r.include_renewals,
       count(*) AS n,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (s.price / (s.basis_area_m2 / 3.3058))::double precision)
           FILTER (WHERE s.deal_type = 'sale') AS median_price_per_pyeong,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (s.deposit / (s.basis_area_m2 / 3.3058))::double precision)
           FILTER (WHERE s.deal_type = 'jeonse') AS median_deposit_per_pyeong,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY s.deposit::double precision)
           FILTER (WHERE s.deal_type = 'monthly') AS median_deposit,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY s.monthly_rent::double precision)
           FILTER (WHERE s.deal_type = 'monthly') AS median_monthly_rent
FROM scoped s
CROSS JOIN LATERAL (VALUES (true), (false)) AS d(include_direct)
CROSS JOIN LATERAL (VALUES (true), (false)) AS r(include_renewals)
WHERE (d.include_direct OR s.trade_method IS DISTINCT FROM 'direct')
  AND (r.include_renewals OR s.contract_type IS DISTINCT FROM '갱신')
GROUP BY s.scope_level, s.scope_id, s.property_type, s.deal_type, s.month, d.include_direct, r.include_renewals;
CREATE UNIQUE INDEX mv_monthly_stats_key
    ON mv_monthly_stats (scope_level, scope_id, property_type, deal_type, month, include_direct, include_renewals);
"""

DROP = """
DROP MATERIALIZED VIEW IF EXISTS mv_monthly_stats;
DROP TABLE IF EXISTS restriction_explanations, glossary_terms, zoning_rules, parcels_cache, geocode_cache,
    transactions, complex_area_types, complexes, source_fetch_log, etl_runs, regions;
"""


def _statements(sql: str) -> list[str]:
    """asyncpg는 한 번에 한 문장만 실행한다. 이 DDL에는 함수 본문이 없어 문장 끝(;줄바꿈)으로 나눠도 안전하다."""
    return [s.strip() for s in sql.split(";\n") if s.strip()]


def upgrade() -> None:
    for statement in _statements(SCHEMA):
        op.execute(statement)


def downgrade() -> None:
    # postgis 확장은 다른 스키마가 쓸 수 있어 남겨 둔다
    for statement in _statements(DROP):
        op.execute(statement)

"""redevelopments: 부산 정비사업(재개발·재건축 등) 구역과 사업추진단계 (부산광역시 정비사업 정보)

원천 목록에서 빠진 구역은 지우지 않고 `closed_at`을 채운다. 수집이 실패하거나 0건이면 아무것도 닫지 않는다.
설계: docs/REDEVELOPMENT.md

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

JOBS_BEFORE = "'collect', 'backfill', 'aggregate', 'auction', 'facility'"


def upgrade() -> None:
    op.execute("ALTER TABLE etl_runs DROP CONSTRAINT etl_runs_job_check")
    op.execute(
        f"ALTER TABLE etl_runs ADD CONSTRAINT etl_runs_job_check CHECK (job IN ({JOBS_BEFORE}, 'redevelopment'))"
    )
    op.execute(
        """
        CREATE TABLE redevelopments (
            id                      text PRIMARY KEY,          -- aCode(예: BARA_0000000000056). 없으면 NOCODE_ + 구역명 해시
            area_name               text NOT NULL,
            kind                    text NOT NULL,             -- redevelopment / reconstruction / small_reconstruction …
            step_raw                text,                      -- 사업추진단계 원문
            stage                   text NOT NULL DEFAULT 'unknown',
            stage_order             smallint,                  -- 0 해제 … 11 조합해산. 모르는 단계는 NULL
            location                text,                      -- 위치 원문(지번 또는 조합 사무실 주소)
            location_kind           text NOT NULL DEFAULT 'none' CHECK (location_kind IN ('lot', 'office', 'none')),
            lat                     double precision,
            lng                     double precision,
            location_precise        boolean NOT NULL DEFAULT false,  -- 지번까지 맞음. false면 법정동 중심점이거나 위치 없음
            geom                    geometry(Point, 4326),     -- lat/lng가 있을 때
            area_m2                 double precision,
            households              integer,
            buildings               integer,
            union_members           integer,
            floor_area_ratio        double precision,          -- 용적률(%)
            building_coverage_ratio double precision,          -- 건폐율(%)
            scale                   text,                      -- 예: 지하3층지상32층
            business_entity         text,
            contractor              text,
            engineer                text,
            architect               text,
            specialists             text,
            tel                     text,
            images                  jsonb NOT NULL DEFAULT '{}',  -- {view, location, panorama, layout}, 있는 것만
            first_seen_at           timestamptz NOT NULL,
            last_seen_at            timestamptz NOT NULL,
            closed_at               timestamptz,
            updated_at              timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX redevelopments_geom ON redevelopments USING gist (geom)")
    op.execute(
        "CREATE INDEX redevelopments_open_order ON redevelopments (stage_order DESC NULLS LAST, area_name) WHERE closed_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE redevelopments")
    op.execute("DELETE FROM etl_runs WHERE job = 'redevelopment'")
    op.execute("ALTER TABLE etl_runs DROP CONSTRAINT etl_runs_job_check")
    op.execute(f"ALTER TABLE etl_runs ADD CONSTRAINT etl_runs_job_check CHECK (job IN ({JOBS_BEFORE}))")

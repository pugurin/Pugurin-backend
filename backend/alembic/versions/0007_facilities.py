"""schools·hospitals: 단지 주변 생활 시설 (나이스 학교 기본정보, 건강보험심사평가원 병원정보)

원천 목록에서 빠진 시설은 지우지 않고 `closed_at`을 채운다(폐교·폐업).

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

JOBS_BEFORE = "'collect', 'backfill', 'aggregate', 'auction'"


def upgrade() -> None:
    op.execute("ALTER TABLE etl_runs DROP CONSTRAINT etl_runs_job_check")
    op.execute(f"ALTER TABLE etl_runs ADD CONSTRAINT etl_runs_job_check CHECK (job IN ({JOBS_BEFORE}, 'facility'))")
    op.execute(
        """
        CREATE TABLE schools (
            id               text PRIMARY KEY,           -- 표준 학교코드(아직 없는 (가칭) 학교는 TMP-…)
            name             text NOT NULL,
            kind             text,                       -- 초등학교 / 중학교 / 고등학교 / 특수학교 …
            foundation       text,                       -- 공립 / 사립 / 국립
            coed             text,                       -- 남여공학 / 남 / 여
            address          text NOT NULL,
            region_code      varchar(10),
            location         geometry(Point, 4326),      -- 주소를 카카오로 지오코딩
            tel              text,
            homepage         text,
            opened_on        date,                       -- 개교일. 미래면 개교 전
            last_seen_at     timestamptz NOT NULL,
            closed_at        timestamptz,
            updated_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX schools_open_location ON schools USING gist (location) WHERE closed_at IS NULL")
    op.execute(
        """
        CREATE TABLE hospitals (
            id               text PRIMARY KEY,           -- 요양기관기호(ykiho)
            name             text NOT NULL,
            class_name       text,                       -- 상급종합 / 종합병원 / 병원 / 의원 / 치과의원 / 한의원 …
            address          text NOT NULL,
            location         geometry(Point, 4326),      -- 원천 좌표(XPos·YPos)
            tel              text,
            homepage         text,
            doctors          integer,                    -- 의사 총수
            opened_on        date,
            last_seen_at     timestamptz NOT NULL,
            closed_at        timestamptz,
            updated_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX hospitals_open_location ON hospitals USING gist (location) WHERE closed_at IS NULL")


def downgrade() -> None:
    op.execute("DROP TABLE hospitals, schools")
    op.execute("DELETE FROM etl_runs WHERE job = 'facility'")
    op.execute("ALTER TABLE etl_runs DROP CONSTRAINT etl_runs_job_check")
    op.execute(f"ALTER TABLE etl_runs ADD CONSTRAINT etl_runs_job_check CHECK (job IN ({JOBS_BEFORE}))")

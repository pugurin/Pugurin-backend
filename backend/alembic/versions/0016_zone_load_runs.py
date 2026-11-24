"""etl_runs.job에 'redevelopment_zone'(정비구역 경계 적재 기록)을 더한다

경계를 걸러서 0건이 남아도 "실었다"를 알 수 있게 적재마다 etl_runs에 남긴다(GET /redevelopment-zones의 data_as_of).
0014는 이미 배포됐을 수 있어 고치지 않고 새로 더한다. 0016 전에 실은 경계는 기록이 없으니
그 적재 시각(loaded_at)으로 한 줄 채워 둔다(안 그러면 다시 실을 때까지 503).
설계: docs/REDEVELOPMENT_ZONES.md

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

JOBS_BEFORE = "'collect', 'backfill', 'aggregate', 'auction', 'facility', 'building', 'redevelopment'"


def upgrade() -> None:
    op.execute("ALTER TABLE etl_runs DROP CONSTRAINT etl_runs_job_check")
    op.execute(
        f"ALTER TABLE etl_runs ADD CONSTRAINT etl_runs_job_check CHECK (job IN ({JOBS_BEFORE}, 'redevelopment_zone'))"
    )
    op.execute(
        """
        INSERT INTO etl_runs (job, status, started_at, finished_at, rows_fetched, rows_inserted, report)
        SELECT 'redevelopment_zone', 'success', max(loaded_at), max(loaded_at), count(*), count(*),
               '{"backfilled": true}'::jsonb
        FROM redevelopment_zones
        HAVING count(*) > 0
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM etl_runs WHERE job = 'redevelopment_zone'")
    op.execute("ALTER TABLE etl_runs DROP CONSTRAINT etl_runs_job_check")
    op.execute(f"ALTER TABLE etl_runs ADD CONSTRAINT etl_runs_job_check CHECK (job IN ({JOBS_BEFORE}))")

"""건축물대장으로 아파트 평형별 공급면적 채우기

- `complex_area_types.supply_area_pyeong`·`supply_source`는 0001부터 있었다(지금까지 비어 있었다).
- `complexes.supply_checked_at`: 건축물대장을 마지막으로 본 시각. 하루 호출 한도 안에서 안 본 단지부터 나눠 받는다.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

JOBS_BEFORE = "'collect', 'backfill', 'aggregate', 'auction', 'facility'"


def upgrade() -> None:
    op.execute("ALTER TABLE etl_runs DROP CONSTRAINT etl_runs_job_check")
    op.execute(f"ALTER TABLE etl_runs ADD CONSTRAINT etl_runs_job_check CHECK (job IN ({JOBS_BEFORE}, 'building'))")
    op.execute("ALTER TABLE complexes ADD COLUMN supply_checked_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE complexes DROP COLUMN supply_checked_at")
    op.execute("UPDATE complex_area_types SET supply_area_pyeong = NULL, supply_source = NULL")
    op.execute("DELETE FROM etl_runs WHERE job = 'building'")
    op.execute("ALTER TABLE etl_runs DROP CONSTRAINT etl_runs_job_check")
    op.execute(f"ALTER TABLE etl_runs ADD CONSTRAINT etl_runs_job_check CHECK (job IN ({JOBS_BEFORE}))")

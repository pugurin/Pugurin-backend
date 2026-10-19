"""etl_runs.report: 적재 결과 요약(법정동을 못 찾은 이름, 실패한 단위 등)

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE etl_runs ADD COLUMN report jsonb NOT NULL DEFAULT '{}'")


def downgrade() -> None:
    op.execute("ALTER TABLE etl_runs DROP COLUMN report")

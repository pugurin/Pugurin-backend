"""부산 법정동 코드 마스터를 regions에 넣는다 (docs/ERD.md 3.4)

Revision ID: 0002
Revises: 0001
"""

from alembic import op

from app.db.seed import upsert_regions

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    upsert_regions(op.get_bind())


def downgrade() -> None:
    op.execute("DELETE FROM regions WHERE level = 'dong'")
    op.execute("DELETE FROM regions")

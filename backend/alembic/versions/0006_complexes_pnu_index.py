"""complexes.pnu 인덱스: 경매 물건을 같은 필지의 단지와 잇는 조회(/auctions)가 행마다 쓴다

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX complexes_pnu ON complexes (pnu) WHERE pnu IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX complexes_pnu")

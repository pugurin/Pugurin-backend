"""알림 created_at 인덱스: 주 1회 오래된 알림 지우기(created_at < cutoff)가 표 전체를 훑지 않게

0017은 이미 머지·배포되었을 수 있어 고치지 않고 새 리비전으로 더한다.

Revision ID: 0018
Revises: 0017
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX notifications_created_idx ON notifications (created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX notifications_created_idx")

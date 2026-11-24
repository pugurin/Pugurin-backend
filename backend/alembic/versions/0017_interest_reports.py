"""관심 단지 주간 리포트: 정비사업 단계 변화와 알림 테이블

이 마이그레이션에 리포트·알림 테이블이 차례로 더해진다. 각 변경은 `_`로 나눈 함수 하나씩이고
downgrade는 upgrade의 역순으로 부른다.

Revision ID: 0017
Revises: 0016
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def _redevelopment_stage_change_up() -> None:
    # 단계가 바뀐 시각(리포트의 "이번 주 단계 변화")
    op.execute("ALTER TABLE redevelopments ADD COLUMN previous_stage text")
    op.execute("ALTER TABLE redevelopments ADD COLUMN stage_changed_at timestamptz")


def _redevelopment_stage_change_down() -> None:
    op.execute("ALTER TABLE redevelopments DROP COLUMN stage_changed_at")
    op.execute("ALTER TABLE redevelopments DROP COLUMN previous_stage")


def _notifications_up() -> None:
    # 탈퇴 때 사용자 행은 익명화만 하고 남기므로 CASCADE가 안 걸린다. 탈퇴 트랜잭션에서 직접 지운다.
    # 같은 주 리포트는 한 통만(다시 돌려도 덮지 않는다)
    op.execute(
        """
        CREATE TABLE notifications (
            id            uuid PRIMARY KEY,
            user_id       uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            kind          text NOT NULL CHECK (kind IN ('interest_report')),
            period_start  date NOT NULL,
            title         text NOT NULL,
            summary       text NOT NULL,
            payload       jsonb NOT NULL,
            created_at    timestamptz NOT NULL DEFAULT now(),
            read_at       timestamptz,
            UNIQUE (user_id, kind, period_start)
        )
        """
    )
    op.execute("CREATE INDEX notifications_user_created_idx ON notifications (user_id, created_at DESC)")


def _notifications_down() -> None:
    op.execute("DROP TABLE notifications")


def upgrade() -> None:
    _redevelopment_stage_change_up()
    _notifications_up()


def downgrade() -> None:
    _notifications_down()
    _redevelopment_stage_change_down()

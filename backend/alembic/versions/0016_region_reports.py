"""관심 지역 리포트: 관심 목록에 지역(법정동)을 더한다

이 마이그레이션에 리포트·알림 테이블이 차례로 더해진다. 각 변경은 `_`로 나눈 함수 하나씩이고
downgrade는 upgrade의 역순으로 부른다.

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

# 0015에서 CREATE TABLE 안에 이름 없이 만든 CHECK의 기본 이름
KIND_CHECK = "user_interests_kind_check"


def _interest_kind_region_up() -> None:
    op.execute(f"ALTER TABLE user_interests DROP CONSTRAINT {KIND_CHECK}")
    op.execute(f"ALTER TABLE user_interests ADD CONSTRAINT {KIND_CHECK} CHECK (kind IN ('complex', 'auction', 'region'))")


def _interest_kind_region_down() -> None:
    op.execute("DELETE FROM user_interests WHERE kind = 'region'")
    op.execute(f"ALTER TABLE user_interests DROP CONSTRAINT {KIND_CHECK}")
    op.execute(f"ALTER TABLE user_interests ADD CONSTRAINT {KIND_CHECK} CHECK (kind IN ('complex', 'auction'))")


def _redevelopment_stage_change_up() -> None:
    # 단계가 바뀐 시각(리포트의 "이번 주 단계 변화")과 구역의 법정동 코드(지역 리포트 묶음용)
    op.execute("ALTER TABLE redevelopments ADD COLUMN previous_stage text")
    op.execute("ALTER TABLE redevelopments ADD COLUMN stage_changed_at timestamptz")
    op.execute("ALTER TABLE redevelopments ADD COLUMN region_code varchar(10)")


def _redevelopment_stage_change_down() -> None:
    op.execute("ALTER TABLE redevelopments DROP COLUMN region_code")
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
            kind          text NOT NULL CHECK (kind IN ('region_report')),
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
    _interest_kind_region_up()
    _redevelopment_stage_change_up()
    _notifications_up()


def downgrade() -> None:
    _notifications_down()
    _redevelopment_stage_change_down()
    _interest_kind_region_down()

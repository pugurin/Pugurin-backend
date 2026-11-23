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


def upgrade() -> None:
    _interest_kind_region_up()


def downgrade() -> None:
    _interest_kind_region_down()

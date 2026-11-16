"""device_interests: 관심 단지·관심 경매 물건(앱 설치 단위)

로그인(2차)이 아직 없어서 관심 목록은 `X-Device-Id`(앱 설치 UUID)에 묶는다.
로그인이 생기면 이 표의 기기 행을 사용자 계정으로 옮긴다.
대상은 지워지거나 닫혀도 행을 남긴다(목록에서 '종료'로 보이거나 빠진다). 그래서 외래키를 걸지 않는다.

Revision ID: 0012
Revises: 0011
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE device_interests (
            device_id   uuid NOT NULL,
            kind        text NOT NULL CHECK (kind IN ('complex', 'auction')),
            target_id   text NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (device_id, kind, target_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE device_interests")
